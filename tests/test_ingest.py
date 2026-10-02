"""M4-10：`ingest_from_provider` 适配层（provider → DataStore）。

对应 `docs/18_m4_dataprovider.md` §4.6 与使用者确认的 D1~D4 + 4 个补充点：

- **D1**：`quality` 用 `ProviderQualityReport`（Q1~Q12）；`validate_bars` 的
  `DataQualityReport` 放 `diagnostics["validate"]`，且其 `errors` 必须影响
  `step_status["validate"]`（strict→failed / non-strict→degraded）；
- **D2**：`manifest` 为空时必须在 `degradation_notes` 里说明原因；
- **D3**：`step_status` 取值域 `ok|degraded|failed|skipped`，
  **`skipped` 只表示用户显式没请求**，源不支持属于 `degraded`；
- **D4**：`degradation_notes` 逐条写明被触发的既有口径（停牌/涨跌停/ST/复权）；
- **补充 1/2/4**：`overall_status`、失败原子性（写进 docstring）、
  `capabilities` 是「理论上具备」的语义。

全部离线：synthetic / CSV / Parquet / AKShare+FakeClient。
"""

from __future__ import annotations

import pandas as pd

from tests.compat import raises
from tests.contracts.provider_contract import write_dataset
from tests.fake_akshare import FakeAKShareClient
from tests.tools import workspace_tmp

from aqs.config.schema import DataConfig, SyntheticDataConfig, UniverseConfig
from aqs.core.exceptions import DataError, DataQualityError
from aqs.data.akshare_provider import AKShareProvider
from aqs.data.cache import DataCache
from aqs.data.file_provider import CsvProvider
from aqs.data.loader import (
    INGEST_STEPS,
    STEP_STATUSES,
    ingest_from_provider,
    load_market_data,
)
from aqs.data.provider import filter_effective_window
from aqs.data.ratelimit import RateLimiter
from aqs.data.synthetic_provider import SyntheticProvider

START = "2022-01-04"
END = "2022-06-30"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def synthetic_provider(*, n_symbols: int = 8, seed: int = 4242, index_size: int = 4) -> SyntheticProvider:
    return SyntheticProvider(
        DataConfig(),
        n_symbols=n_symbols,
        seed=seed,
        index_size=index_size,
        start=START,
        end=END,
    )


def csv_provider(root, *, with_fundamentals: bool = True, with_index: bool = True) -> CsvProvider:
    """写一套 CSV 数据集；再用「是否传路径」控制 provider 的能力声明。"""
    members_path = write_dataset(root, fmt="csv")
    return CsvProvider(
        root,
        index_members_path=members_path if with_index else None,
        fundamentals_path=(root / "fundamentals.csv") if with_fundamentals else None,
        config=DataConfig(provider="csv"),
    )


def write_min_csv(root):
    """只含必需列的最小数据集（能力推断应显示「除行情外都没有」）。"""
    root.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range(START, periods=6)
    pd.DataFrame(
        {
            "date": dates,
            "symbol": "600000.SH",
            "open": 10.0,
            "high": 10.5,
            "low": 9.5,
            "close": 10.0,
            "volume": 1_000_000.0,
            "amount": 10_000_000.0,
        }
    ).to_csv(root / "bars.csv", index=False, encoding="utf-8-sig")
    return root


class WrappedProvider:
    """把真实 provider 包一层：注入失败、篡改返回（测失败路径与校验失败）。

    只代理协议方法；其余属性（如 `cache_manifest`）经 `__getattr__` 透传，
    因此「有无缓存能力」的判定仍然按真实 provider 走。
    """

    def __init__(self, base, *, fail=(), patch_bars=None) -> None:
        self.base = base
        self.fail = set(fail)
        self.patch_bars = patch_bars

    @property
    def name(self) -> str:
        return self.base.name

    def _guard(self, method: str) -> None:
        if method in self.fail:
            raise DataError(f"注入的 {method} 故障")

    def capabilities(self):
        return self.base.capabilities()

    def health_check(self):
        return self.base.health_check()

    def fetch_trading_calendar(self, *args, **kwargs):
        self._guard("fetch_trading_calendar")
        return self.base.fetch_trading_calendar(*args, **kwargs)

    def fetch_index_members(self, *args, **kwargs):
        self._guard("fetch_index_members")
        return self.base.fetch_index_members(*args, **kwargs)

    def fetch_symbol_meta(self, *args, **kwargs):
        self._guard("fetch_symbol_meta")
        return self.base.fetch_symbol_meta(*args, **kwargs)

    def fetch_fundamentals(self, *args, **kwargs):
        self._guard("fetch_fundamentals")
        return self.base.fetch_fundamentals(*args, **kwargs)

    def fetch_industry(self, *args, **kwargs):
        self._guard("fetch_industry")
        return self.base.fetch_industry(*args, **kwargs)

    def fetch_bars(self, symbols, start, end, *, adjust="none"):
        self._guard("fetch_bars")
        frame, prov = self.base.fetch_bars(symbols, start, end, adjust=adjust)
        if self.patch_bars is not None:
            frame = self.patch_bars(frame)
        return frame, prov

    def __getattr__(self, item):
        return getattr(self.base, item)


def ingest(provider, *, config=None, universe=None, **kwargs):
    return ingest_from_provider(
        provider,
        config=config if config is not None else DataConfig(),
        universe_config=universe if universe is not None else UniverseConfig(mode="all"),
        start=START,
        end=END,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #
def test_synthetic_ingest_builds_usable_store_and_full_report():
    report = ingest(synthetic_provider())

    # store 真的可用（不是「构造成功但查不了」）
    days = report.store.trading_days()
    assert len(days) > 100
    assert len(report.store.bars_on(days[0])) > 0
    assert len(report.store.universe(days[-1])) > 0

    assert report.overall_status in ("ok", "degraded")
    assert set(report.step_status) == set(INGEST_STEPS)
    assert set(report.step_status.values()) <= set(STEP_STATUSES)
    assert report.step_status["bars"] == "ok"
    assert report.step_status["store"] == "ok"
    assert report.step_status["symbol_meta"] == "ok"
    assert report.provenance["bars"].rows > 0
    assert report.diagnostics["provider"] == "synthetic"


def test_csv_ingest_reports_each_step():
    with workspace_tmp("ingest_csv") as root:
        report = ingest(csv_provider(root), universe=UniverseConfig(mode="index"))

    assert report.step_status["calendar"] == "ok"
    assert report.step_status["index_members"] == "ok"
    assert report.step_status["symbol_meta"] == "ok"
    assert report.step_status["bars"] == "ok"
    assert report.step_status["fundamentals"] == "ok"
    assert report.step_status["normalize"] == "ok"
    assert report.step_status["validate"] == "ok"
    assert report.step_status["store"] == "ok"
    # 索引与财务都来自本地文件 → 都应记为 ok（不是 skipped）
    assert "skipped" not in report.step_status.values()


def test_symbols_none_or_empty_means_derive_from_index_members():
    p = synthetic_provider()
    by_default = ingest(p)
    explicit = ingest(p, symbols=[])
    assert by_default.overall_status == explicit.overall_status
    assert len(by_default.store.trading_days()) == len(explicit.store.trading_days())


# --------------------------------------------------------------------------- #
# D3：skipped 只表示「用户显式没请求」
# --------------------------------------------------------------------------- #
def test_fundamentals_not_requested_is_skipped():
    report = ingest(synthetic_provider(), fundamentals=False, industry=False)
    assert report.step_status["fundamentals"] == "skipped"
    assert report.step_status["industry"] == "skipped"
    # skipped 之外不应出现其它 skipped
    assert list(report.step_status.values()).count("skipped") == 2


def test_unsupported_capability_is_degraded_not_skipped():
    """源不支持 ≠ 用户没请求：必须是 degraded（D3）。"""
    with workspace_tmp("ingest_min") as root:
        write_min_csv(root)  # 只有必需列：财务/行业/上市日都不支持
        cfg = DataConfig(provider="csv")
        cfg.listing_date.policy = "proxy"
        provider = CsvProvider(root, config=cfg)
        caps = provider.capabilities()
        assert caps.fundamentals is False and caps.industry is False
        report = ingest(provider, config=cfg, universe=UniverseConfig(mode="all"))

    assert report.step_status["fundamentals"] == "degraded"
    assert report.step_status["industry"] == "degraded"
    assert "skipped" not in (
        report.step_status["fundamentals"],
        report.step_status["industry"],
    )
    assert any("不支持" in n for n in report.degradation_notes)


# --------------------------------------------------------------------------- #
# 失败路径：必需步骤 vs 可选数据
# --------------------------------------------------------------------------- #
def test_fundamentals_fetch_failure_never_aborts():
    """可选数据失败一律不中止；`failure_policy` 只决定记 degraded 还是 failed。"""
    with workspace_tmp("ingest_fund_fail") as root:
        base = csv_provider(root)
        fallback_cfg = DataConfig(provider="csv")
        report = ingest(WrappedProvider(base, fail={"fetch_fundamentals"}), config=fallback_cfg)
        assert report.step_status["fundamentals"] == "degraded"
        assert report.overall_status == "degraded"
        assert any("已跳过" in n for n in report.degradation_notes)

        fail_cfg = DataConfig(provider="csv", failure_policy="fail")
        report2 = ingest(WrappedProvider(base, fail={"fetch_fundamentals"}), config=fail_cfg)
        assert report2.step_status["fundamentals"] == "failed"
        assert report2.overall_status == "failed"
        assert any("本次无该数据" in n for n in report2.degradation_notes)
        # 仍然构建出了 store（可选数据缺失不阻断）
        assert len(report2.store.trading_days()) > 0


def test_required_step_failure_aborts_with_reason():
    with workspace_tmp("ingest_required_fail") as root:
        base = csv_provider(root)
        with raises(DataError) as ctx:
            ingest(WrappedProvider(base, fail={"fetch_bars"}))
        assert "行情取数失败" in str(ctx.value)
        assert "step_status" in str(ctx.value)


def test_symbol_meta_unsupported_follows_listing_date_policy():
    """源无上市日：不中止，由 data.listing_date.policy 决定（strict 报错 / proxy 降级）。"""
    with workspace_tmp("ingest_nometa") as root:
        write_min_csv(root)
        base = CsvProvider(root, config=DataConfig(provider="csv"))
        assert base.capabilities().listing_dates is False

        # strict（默认）：store 拒绝缺 list_date 的数据
        with raises(DataQualityError) as ctx:
            ingest(base)
        assert "list_date" in str(ctx.value)

        # proxy：继续运行，但步态为 degraded 且逐条披露
        cfg = DataConfig(provider="csv")
        cfg.listing_date.policy = "proxy"
        report = ingest(base, config=cfg)
        assert report.step_status["symbol_meta"] == "degraded"
        assert report.overall_status == "degraded"
        assert any("listing_dates=False" in n for n in report.degradation_notes)
        assert report.store._proxy_symbols, "代理口径标的必须被登记（可用于披露）"


def test_symbol_meta_fetch_failure_respects_failure_policy():
    with workspace_tmp("ingest_meta_fail") as root:
        base = csv_provider(root)

        # fallback：降级为无 meta 模式（此时靠 proxy 口径才能继续）
        cfg = DataConfig(provider="csv")
        cfg.listing_date.policy = "proxy"
        report = ingest(WrappedProvider(base, fail={"fetch_symbol_meta"}), config=cfg)
        assert report.step_status["symbol_meta"] == "degraded"
        assert any("进入无 meta 模式" in n for n in report.degradation_notes)

        # fail：直接中止
        fail_cfg = DataConfig(provider="csv", failure_policy="fail")
        with raises(DataError) as ctx:
            ingest(WrappedProvider(base, fail={"fetch_symbol_meta"}), config=fail_cfg)
        assert "failure_policy=fail" in str(ctx.value)


def test_index_members_failure_or_fallback():
    with workspace_tmp("ingest_index_fail") as root:
        base = csv_provider(root)
        broken = WrappedProvider(base, fail={"fetch_index_members"})

        # fallback_to_all=True（默认）：退化为全市场并披露
        report = ingest(broken, universe=UniverseConfig(mode="index", fallback_to_all=True))
        assert report.step_status["index_members"] == "degraded"
        assert any("退化为全市场" in n for n in report.degradation_notes)
        assert len(report.store.trading_days()) > 0

        # fallback_to_all=False：必须报错（不得静默退化）
        with raises(DataError) as ctx:
            ingest(broken, universe=UniverseConfig(mode="index", fallback_to_all=False))
        assert "fallback_to_all=False" in str(ctx.value)


# --------------------------------------------------------------------------- #
# D1：校验失败影响 step_status；has_errors / overall_status
# --------------------------------------------------------------------------- #
def test_validate_errors_toggle_step_status_and_has_errors():
    with workspace_tmp("ingest_validate") as root:
        base = csv_provider(root)
        # 制造重复主键（(date, symbol) 重复）
        dup = WrappedProvider(base, patch_bars=lambda f: pd.concat([f, f.iloc[[0]]], ignore_index=True))

        # strict：抛错并附报告，step_status 里 validate=failed
        with raises(DataQualityError) as ctx:
            ingest(dup)
        assert "step_status" in str(ctx.value) and "failed" in str(ctx.value)

        # non-strict：继续运行，但步态降级、诊断带原始报告、has_errors 为真
        cfg = DataConfig(provider="csv")
        cfg.quality.strict = False
        report = ingest(dup, config=cfg)
        assert report.step_status["validate"] == "degraded"
        validate_report = report.diagnostics["validate"]
        assert validate_report.errors, "必须保留原始校验报告"
        assert any("主键" in e for e in validate_report.errors)
        assert report.has_errors is True
        assert report.quality.ok is False


def test_index_members_approximated_is_disclosed_in_diagnostics_and_quality():
    """B1 附加要求：近似必须同时进 diagnostics 与**报告结构**（不只是日志）。"""
    with workspace_tmp("ingest_ak") as root:
        provider = AKShareProvider(
            DataConfig(provider="akshare", max_missing_ratio=1.0),
            client=FakeAKShareClient(
                index_snapshots={"000300": ["index_stock_cons_csindex_000300_20220301.csv"]}
            ),
            cache=DataCache(root / "cache", provider="akshare"),
            limiter=RateLimiter(requests_per_minute=0),
            sleeper=lambda _s: None,
            # 固定时钟：快照日落在请求区间内，否则成分会被窗口过滤掉
            now=lambda: pd.Timestamp("2022-03-05 08:00", tz="UTC").to_pydatetime(),
        )
        assert provider.capabilities().index_members is False
        # AKShare 逐标的抓取：必须给出明确标的（夹具里只有 600000）
        report = ingest(
            provider,
            symbols=["600000.SH"],
            universe=UniverseConfig(mode="index", fallback_to_all=True),
        )

    assert report.diagnostics["index_members_approximated"] is True
    assert any("幸存者偏差" in n for n in report.degradation_notes)
    quality_text = " | ".join(f.message for f in report.quality.findings)
    assert "index_members=False" in quality_text, "近似披露必须写进 quality 报告结构"


def test_fallback_notes_list_the_triggered_existing_semantics():
    """D4：披露要写清「用了什么既有口径」，而不是只写「降级」。"""
    with workspace_tmp("ingest_notes") as root:
        write_min_csv(root)
        cfg = DataConfig(provider="csv")
        cfg.listing_date.policy = "proxy"
        provider = CsvProvider(root, config=cfg)
        report = ingest(provider, config=cfg, fundamentals=False, industry=False)

    text = " | ".join(report.degradation_notes)
    assert "volume<=0" in text, "停牌口径必须写明"
    assert "板块规则推算" in text and "10%" in text, "涨跌停口径必须写明并给出比例"
    assert "is_st 缺失 → 默认 False" in text
    assert "复权因子缺失" in text


def test_manifest_absence_is_explained():
    with workspace_tmp("ingest_manifest_csv") as root:
        report = ingest(csv_provider(root))
    assert report.manifest == []
    assert any("未启用缓存，无 manifest" in n for n in report.degradation_notes)

    with workspace_tmp("ingest_manifest_ak") as root:
        provider = AKShareProvider(
            DataConfig(provider="akshare", max_missing_ratio=1.0),
            client=FakeAKShareClient(),
            cache=DataCache(root / "cache", provider="akshare"),
            limiter=RateLimiter(requests_per_minute=0),
            sleeper=lambda _s: None,
        )
        report_ak = ingest(provider, symbols=["600000.SH"])
    assert isinstance(report_ak.manifest, list)
    assert not any("未启用缓存" in n for n in report_ak.degradation_notes), (
        "有缓存能力的 provider 不得被说成「未启用缓存」"
    )
    if report_ak.manifest:
        assert {m.provider for m in report_ak.manifest} == {"akshare"}


# --------------------------------------------------------------------------- #
# 补充 3：与旧入口（load_market_data）的一致性
# --------------------------------------------------------------------------- #
def test_legacy_load_market_data_matches_ingest_store():
    """两个入口必须产生相同内部状态。

    比较对象与方法（使用者指定）：

    - 相同 ``DataConfig`` + 相同 ``project.seed``（此处用 ``cfg.synthetic.seed``）下
      ``load_market_data(cfg)`` 的 :class:`MarketDataBundle`
      ↔ ``build_provider(cfg) → ingest_from_provider(...)`` 的 ``IngestReport.store``；
    - ``bars``：两侧都按 ``(date, symbol)`` 排序后
      ``pd.testing.assert_frame_equal`` **严格逐行逐列比较（含 dtype）**；
    - ``index_members`` / ``fundamentals``：先按 ingest 的**窗口口径**裁剪旧口径数据
      （成分按区间相交、财务按公告日落在区间内），再断言长度相等，
      并抽样若干 ``(date, code)`` 组合比较**查询结果相等**；
    - 私有属性（``store._df`` / ``_members`` / ``_fundamentals``）直接访问 ——
      本用例测的就是「两个入口产生相同的内部状态」。

    **必须先对齐生成调用**：`generate_market_data` 的「位置 → 代码」分配取决于是否显式传入
    ``symbols``（实测传 8 个代码与不传，会让 600001/600003 的上市窗口对调，行数相同但
    区间不同）。所以两侧都不能各自传一份 ``symbols``：旧入口不传（用内部生成），
    新入口只把 provider **已经生成好**的那一份标的清单交给 `fetch_bars` 做过滤。
    """
    syn = SyntheticDataConfig(n_symbols=8, seed=4242, index_size=4, index_code="000300.SH")
    cfg = DataConfig(synthetic=syn)
    provider = SyntheticProvider(cfg, start=START, end=END)

    # provider 内部生成一次；两侧共用这一份标的清单（见 docstring 的口径说明）
    meta_frame, _ = provider.fetch_symbol_meta([])
    universe_symbols = sorted(set(meta_frame["symbol"].astype(str)))
    assert len(universe_symbols) == syn.n_symbols

    legacy = load_market_data(
        cfg,
        start=START,
        end=END,
        n_symbols=syn.n_symbols,
        seed=syn.seed,
        index_size=syn.index_size,
        index_code=syn.index_code,
    )
    report = ingest(provider, config=cfg, universe=UniverseConfig(mode="all"), symbols=universe_symbols)

    # ---- bars：严格逐行逐列（含 dtype）----
    left = legacy.bars.sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)
    right = report.store._df.sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)
    assert list(left.columns) == list(right.columns), (
        f"列集不同：仅旧入口 {sorted(set(left.columns) - set(right.columns))} / "
        f"仅新入口 {sorted(set(right.columns) - set(left.columns))}"
    )
    pd.testing.assert_frame_equal(left, right, check_dtype=True)

    # ---- index_members：按窗口口径裁剪后长度相等 + 抽样查询相等 ----
    legacy_members = filter_effective_window(legacy.index_members, START, END)
    assert report.store._members is not None
    assert len(report.store._members) == len(legacy_members)
    days = report.store.trading_days()
    for day in (days[0], days[len(days) // 2], days[-1]):
        expected = sorted(
            set(
                legacy_members.loc[
                    (
                        pd.to_datetime(legacy_members["effective_from"]) <= pd.Timestamp(day)
                    )
                    & (
                        pd.to_datetime(legacy_members["effective_to"]).isna()
                        | (pd.to_datetime(legacy_members["effective_to"]) >= pd.Timestamp(day))
                    ),
                    "symbol",
                ].astype(str)
            )
        )
        assert report.store.index_members("000300.SH", day) == expected, f"{day} 成分不一致"

    # ---- fundamentals：同样裁剪后长度相等 + 抽样 (date, code) 查询相等 ----
    legacy_fund = legacy.fundamentals
    announce = pd.to_datetime(legacy_fund["announce_date"], errors="coerce")
    legacy_fund = legacy_fund.loc[
        (announce >= pd.Timestamp(START)) & (announce <= pd.Timestamp(END))
    ].reset_index(drop=True)
    assert report.store._fundamentals is not None
    assert len(report.store._fundamentals) == len(legacy_fund)
    sample_rows = legacy_fund.head(3)
    for _, row in sample_rows.iterrows():
        got = report.store.fundamentals(
            pd.Timestamp(row["announce_date"]).date(), symbols=[str(row["symbol"])]
        )
        assert len(got) >= 1, f"{row['symbol']} @ {row['announce_date']} 在 store 中查不到"
        assert float(got.iloc[0]["report_period"].year) == float(row["report_period"].year)
