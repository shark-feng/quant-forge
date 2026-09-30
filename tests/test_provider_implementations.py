"""M4-6：provider 实现（synthetic / csv / parquet）。

对应 `docs/18_m4_dataprovider.md` §4.1 的返回约定。
这里只测「实现本身」；四实现的**同一套契约**在 M4-9 的 `tests/contracts/` 中跑。

Parquet 相关用例在 pyarrow 缺失时抛 ``unittest.SkipTest``（runner 与 pytest 都识别），
**跳过不计入通过** —— 避免「环境缺依赖」被读成「验证通过」。
"""

from __future__ import annotations

import pandas as pd

from tests.compat import raises, skip
from tests.tools import workspace_tmp

from aqs.config.schema import DataConfig
from aqs.core.exceptions import DataError
from aqs.data.cache import parquet_available
from aqs.data.file_provider import CsvProvider, ParquetProvider
from aqs.data.provider import DataProvider
from aqs.data.synthetic_provider import SyntheticProvider

START = "2022-01-04"
END = "2022-03-31"


def make_synthetic(**kwargs) -> SyntheticProvider:
    return SyntheticProvider(
        DataConfig(), n_symbols=6, seed=20240101, index_size=4, start=START, end=END, **kwargs
    )


def write_csv_dataset(root, *, symbols=("600000.SH", "600001.SH"), days=6, with_optional=True):
    """落一套 CSV 数据集（含/不含可选列），返回目录。"""
    root.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range("2022-03-01", periods=days)
    frames = []
    for i, symbol in enumerate(symbols):
        price = 10.0 + i
        frame = pd.DataFrame(
            {
                "date": dates,
                "symbol": symbol,
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": 1000.0,
                "amount": 1000.0 * price,
            }
        )
        if with_optional:
            frame["adj_factor"] = 1.0
            frame["is_suspended"] = False
            frame["limit_up"] = price * 1.1
            frame["limit_down"] = price * 0.9
            frame["is_st"] = False
            frame["list_date"] = pd.Timestamp("2010-01-01")
            frame["industry"] = "银行"
        frames.append(frame)
    pd.concat(frames, ignore_index=True).to_csv(
        root / "bars.csv", index=False, encoding="utf-8-sig"
    )
    # 一标的一文件的布局也要能读
    (root / "index").mkdir(exist_ok=True)
    pd.DataFrame(
        {
            "index_code": ["000300.SH"],
            "symbol": ["600000.SH"],
            "effective_from": [pd.Timestamp("2022-01-01")],
            "effective_to": [pd.NaT],
        }
    ).to_csv(root / "index" / "index_members.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(
        {
            "symbol": ["600000.SH"],
            "report_period": [pd.Timestamp("2021-12-31")],
            "announce_date": [pd.Timestamp("2022-03-15")],
            "net_assets": [1000.0],
        }
    ).to_csv(root / "fundamentals.csv", index=False, encoding="utf-8-sig")
    return root


# --------------------------------------------------------------------------- #
# SyntheticProvider
# --------------------------------------------------------------------------- #
def test_synthetic_provider_satisfies_protocol_and_declares_full_capabilities():
    provider = make_synthetic()

    assert isinstance(provider, DataProvider), "应满足统一取数协议"
    caps = provider.capabilities()
    assert caps.daily_bars and caps.index_members and caps.fundamentals
    assert caps.listing_dates and caps.delistings and caps.adjustment_factors
    assert caps.intraday is False
    assert caps.missing_required(need_index_members=True, need_fundamentals=True) == ()

    health = provider.health_check()
    assert health.ok is True
    assert health.details["deterministic"] is True
    assert health.details["rows"] > 0


def all_symbols(provider) -> list[str]:
    """取 provider 的全部标的（`symbols=[]` 视为「不过滤」）。

    不能硬编码 600000.SH：合成数据的代码是确定性分配出来的，
    小规模 universe 未必包含某个特定代码（本身也是一处常见测试陷阱）。
    """
    meta, _ = provider.fetch_symbol_meta([])
    return sorted(meta["symbol"].astype(str).tolist())


def test_synthetic_provider_returns_canonical_frames_with_provenance():
    provider = make_synthetic()
    symbols = all_symbols(provider)
    assert len(symbols) >= 2, f"合成数据标的过少：{symbols}"
    target = symbols[0]

    bars, prov = provider.fetch_bars([target], START, END)

    assert prov.source == "synthetic"
    assert prov.rows == len(bars) > 0
    assert prov.cache_hit is False
    for column in ("date", "symbol", "open", "high", "low", "close", "volume", "amount", "adj_factor"):
        assert column in bars.columns, f"canonical 行情缺少列：{column}"
    assert set(bars["symbol"]) == {target}
    assert bars["date"].min() >= pd.Timestamp(START)
    assert bars["date"].max() <= pd.Timestamp(END)

    # 未知标的：返回空表 + warning，而不是抛异常
    empty, prov2 = provider.fetch_bars(["999999.SZ"], START, END)
    assert len(empty) == 0
    assert prov2.warnings and "不存在" in prov2.warnings[0]

    cal, _ = provider.fetch_trading_calendar(START, END)
    assert cal == sorted(set(cal)), "交易日历必须升序去重"
    assert len(cal) > 10

    meta, _ = provider.fetch_symbol_meta([target])
    assert "list_date" in meta.columns and len(meta) == 1

    members, _ = provider.fetch_index_members("000300.SH", START, END)
    assert {"index_code", "symbol", "effective_from", "effective_to"}.issubset(members.columns)

    fund, _ = provider.fetch_fundamentals([target], START, END)
    assert "announce_date" in fund.columns

    industry, _ = provider.fetch_industry([target])
    assert set(industry.columns) == {"symbol", "industry"}


def test_synthetic_provider_is_deterministic_and_caches_generation():
    symbols = all_symbols(make_synthetic())
    a, _ = make_synthetic().fetch_bars(symbols[:1], START, END)
    b, _ = make_synthetic().fetch_bars(symbols[:1], START, END)
    pd.testing.assert_frame_equal(a, b)

    provider = make_synthetic()
    first, _ = provider.fetch_bars(symbols[:1], START, END)
    second, _ = provider.fetch_bars(symbols[:1], START, END)
    assert provider._cache.bundle is not None
    pd.testing.assert_frame_equal(first, second)


# --------------------------------------------------------------------------- #
# CsvProvider：能力由实际列推断
# --------------------------------------------------------------------------- #
def test_csv_provider_infers_capabilities_from_columns():
    with workspace_tmp("provider_csv") as root:
        write_csv_dataset(root, with_optional=True)
        provider = CsvProvider(
            root,
            index_members_path=root / "index" / "index_members.csv",
            fundamentals_path=root / "fundamentals.csv",
            config=DataConfig(provider="csv"),
        )
        caps = provider.capabilities()
        assert caps.daily_bars is True
        assert caps.adjustment_factors is True
        assert caps.price_limits is True
        assert caps.st_flags is True
        assert caps.suspensions is True
        assert caps.listing_dates is True
        assert caps.industry is True
        assert caps.index_members is True
        assert caps.fundamentals is True
        # 没有的列必须如实说没有
        assert caps.delistings is False
        assert caps.market_cap is False

        bars, prov = provider.fetch_bars(["600000.SH", "600001.SH"], START, END)
        assert len(bars) == 12  # 2 标的 × 6 天
        assert prov.source == "csv"
        assert set(bars["symbol"]) == {"600000.SH", "600001.SH"}

        cal, _ = provider.fetch_trading_calendar(START, END)
        assert len(cal) == 6

        members, _ = provider.fetch_index_members("000300.SH", START, END)
        assert len(members) == 1
        # 区间相交语义：成分自 2022-01-01 生效且 effective_to 为空，
        # 查询窗口从 2022-01-04 开始 —— 它仍然生效，**必须保留**。
        # （若按 effective_from >= start 过滤，这里会变成空表 → 股票池退化为全市场 → 幸存者偏差）
        assert members["symbol"].tolist() == ["600000.SH"]
        assert pd.isna(members["effective_to"].iloc[0]), "未结束的成分 effective_to 应为空"

        # 反例：窗口完全早于生效日 → 不应返回该成分
        early, _ = provider.fetch_index_members("000300.SH", "2021-01-01", "2021-12-31")
        assert len(early) == 0, "窗口与生效区间无交集时必须返回空"

        fund, _ = provider.fetch_fundamentals(["600000.SH"], START, END)
        assert len(fund) == 1

        industry, _ = provider.fetch_industry(["600000.SH"])
        assert industry["industry"].tolist() == ["银行"]


def test_csv_provider_missing_optional_columns_are_disclosed():
    """只有必需列时：可选能力全部为 False，且给出可读的 notes。"""
    with workspace_tmp("provider_csv_min") as root:
        write_csv_dataset(root, with_optional=False)
        provider = CsvProvider(root, config=DataConfig(provider="csv"))
        caps = provider.capabilities()

        assert caps.daily_bars is True
        assert caps.adjustment_factors is False
        assert caps.price_limits is False
        assert caps.listing_dates is False
        assert caps.index_members is False
        assert caps.fundamentals is False
        assert caps.missing_required() == ("listing_dates",), "上市日缺失应体现在必需能力里"

        notes = " | ".join(caps.notes)
        assert "list_date" in notes
        assert "指数成分" in notes

        # 缺能力的方法返回空表 + warning，而不是抛异常
        empty, prov = provider.fetch_index_members("000300.SH", START, END)
        assert len(empty) == 0
        assert prov.warnings
        empty2, prov2 = provider.fetch_fundamentals(["600000.SH"], START, END)
        assert len(empty2) == 0 and prov2.warnings


def test_csv_provider_health_check_reports_missing_directory():
    with workspace_tmp("provider_csv_missing") as root:
        provider = CsvProvider(root / "nope", config=DataConfig(provider="csv"))
        health = provider.health_check()
        assert health.ok is False
        assert health.errors
        caps = provider.capabilities()
        assert caps.daily_bars is False, "探测失败时能力必须按最低估计"


# --------------------------------------------------------------------------- #
# ParquetProvider：环境不具备时必须显式跳过
# --------------------------------------------------------------------------- #
def test_parquet_provider_requires_pyarrow_with_actionable_error():
    if parquet_available():
        skip("本机已有 pyarrow，本用例仅验证缺失时的报错路径")
    with workspace_tmp("provider_pq_missing") as root:
        with raises(DataError) as ctx:
            ParquetProvider(root, config=DataConfig(provider="parquet"))
        assert "pyarrow" in str(ctx.value)


def test_parquet_provider_contract_round_trip():
    if not parquet_available():
        skip("未安装 pyarrow，无法验证 Parquet provider（CI 环境按需安装）")
    with workspace_tmp("provider_pq") as root:
        frame = pd.DataFrame(
            {
                "date": pd.bdate_range("2022-03-01", periods=4),
                "symbol": "600000.SH",
                "open": 10.0,
                "high": 10.5,
                "low": 9.5,
                "close": 10.0,
                "volume": 1000.0,
                "amount": 10_000.0,
                "adj_factor": 1.0,
            }
        )
        frame.to_parquet(root / "bars.parquet", index=False)
        provider = ParquetProvider(root, config=DataConfig(provider="parquet"))
        assert provider.capabilities().daily_bars is True
        bars, prov = provider.fetch_bars(["600000.SH"], START, END)
        assert len(bars) == 4
        assert prov.source == "parquet"
