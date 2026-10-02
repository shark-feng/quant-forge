"""四个 provider 共用的契约测试（M4-9）。

设计要点：

1. **同一套断言跑四个实现**：检查项定义在本 mixin 里，子类只提供夹具（继承，不重写）。
   故运行器必须能收集继承来的 ``test_*`` 方法 —— 见 `tests/run_tests.py::_iter_tests`
   与 `tests/test_defect_10_test_hygiene.py` 的守护；
2. **不用 ``setUp``**：零依赖运行器只调用 ``test_*`` 方法本体，不调用 ``setUp``；
   夹具统一走 ``classmethod`` + 类级缓存，保证两套运行器结果一致；
3. **断言从 provider 自己的输出推导**（例如窗口区间），不硬编码各源的数据细节，
   这样四个实现共用一套逻辑而不需要为每个源写特例；
4. **能力声明与实际行为必须一致**：声称有某能力就要真的拿得到数据；
   声明为不可用时必须给出 warning 说明降级/近似，**不允许静默**。

八项检查（C1~C8）：

| 编号 | 检查 | 对应风险 |
|---|---|---|
| C1 | 能力声明 ↔ 实际行为一致（含「请求不存在 → 空表 + warning，不抛异常」） | 虚假能力 → 上层误判可用性 |
| C2 | `missing_required` 边界（不缺时不报、缺时不抛异常只返回缺失项） | 闸门误判 |
| C3 | `degradation_notes` 与 `capabilities()` 双向自洽 | 披露与实际不符 |
| C4 | 指数成分**区间相交**过滤（跨窗口生效必须保留 / 窗口早于生效日必须为空） | **幸存者偏差** |
| C5 | 字段规范（canonical 列名） | 下游解析错位 |
| C6 | 主键唯一 | 重复行放大权重 |
| C7 | PIT 列存在（`list_date` / `announce_date`）、行情落在请求区间内 | 上市日兜底 / 财务未来函数 |
| C8 | `Provenance` 契约（行数、时区、symbols 一致） | 报告数字无法溯源 |
"""

from __future__ import annotations

import atexit
import shutil
import uuid
from pathlib import Path
from typing import Any, ClassVar, Sequence

import pandas as pd

from tests.compat import skip
from tests.tools import PROJECT_ROOT

from aqs.data.provider import (
    BACKTEST_REQUIRED,
    CONDITIONAL_REQUIRED,
    Provenance,
    degradation_notes,
)

#: 契约测试使用的统一区间（足够长，便于从中派生子窗口）
START = "2022-01-04"
END = "2022-06-30"

#: 行情必须至少包含的 canonical 列
BARS_REQUIRED_COLUMNS = ("date", "symbol", "open", "high", "low", "close", "volume", "amount")
META_REQUIRED_COLUMNS = ("symbol", "list_date")
MEMBER_COLUMNS = ("index_code", "symbol", "effective_from", "effective_to")
FUNDAMENTAL_KEYS = ("symbol", "report_period", "announce_date")


def temp_root(tag: str) -> Path:
    """契约夹具目录（进程退出时自动清理）。

    不能用 `workspace_tmp`（它是 with 上下文，退出即删），也不能用 `setUp/tearDown`
    （运行器不调用它们）。
    """
    root = PROJECT_ROOT / ".tmp_tests" / f"contract_{tag}_{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    return root


def write_dataset(root: Path, *, fmt: str = "csv", symbols: Sequence[str] = ("600000.SH", "000001.SZ")) -> Path:
    """写一套**能力齐全**的本地数据集（CSV 或 Parquet）。

    刻意**不含** ``delist_date`` 列：能力由列推断，若写了空列会让 provider 声称
    ``delistings=True`` 却没有退市数据（虚假能力）。这里保持「没有就如实说没有」。
    """
    root.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range(START, periods=12)
    frames = []
    for i, symbol in enumerate(symbols):
        price = 10.0 + i
        frame = pd.DataFrame(
            {
                "date": dates,
                "symbol": symbol,
                "name": f"样例{i}",
                "board": "main",
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": 1_000_000.0,
                "amount": 1_000_000.0 * price,
                "adj_factor": 1.0,
                "is_suspended": False,
                "limit_up": round(price * 1.1, 2),
                "limit_down": round(price * 0.9, 2),
                "is_st": False,
                "list_date": pd.Timestamp("2010-01-01"),
                "industry": "银行" if i == 0 else "半导体",
                "total_mv": 1.0e10 + i,
            }
        )
        frames.append(frame)
    bars = pd.concat(frames, ignore_index=True)
    members = pd.DataFrame(
        {
            "index_code": ["000300.SH"] * len(symbols),
            "symbol": list(symbols),
            "effective_from": [pd.Timestamp("2022-01-01")] * len(symbols),  # 早于 START
            "effective_to": [pd.NaT] * len(symbols),
        }
    )
    fundamentals = pd.DataFrame(
        {
            "symbol": list(symbols),
            "report_period": [pd.Timestamp("2021-12-31")] * len(symbols),
            "announce_date": [pd.Timestamp("2022-03-15"), pd.Timestamp("2022-03-20")][: len(symbols)],
            "roe": [0.1] * len(symbols),
        }
    )
    index_dir = root / "index"
    index_dir.mkdir(exist_ok=True)
    if fmt == "csv":
        bars.to_csv(root / "bars.csv", index=False, encoding="utf-8-sig")
        members.to_csv(index_dir / "index_members.csv", index=False, encoding="utf-8-sig")
        fundamentals.to_csv(root / "fundamentals.csv", index=False, encoding="utf-8-sig")
        return root / "index" / "index_members.csv"
    bars.to_parquet(root / "bars.parquet", index=False)
    members.to_parquet(index_dir / "index_members.parquet", index=False)
    fundamentals.to_parquet(root / "fundamentals.parquet", index=False)
    return root / "index" / "index_members.parquet"


def overlapping(frame: pd.DataFrame, start: Any, end: Any) -> pd.DataFrame:
    """按**区间相交**语义筛选（契约测试自己实现一份，用于交叉验证 provider）。

    注意：这**不是** ``effective_from >= start``（那会丢掉窗口前就生效的成分）。
    """
    if frame is None or len(frame) == 0:
        return pd.DataFrame()
    ef = pd.to_datetime(frame["effective_from"], errors="coerce")
    et = pd.to_datetime(frame["effective_to"], errors="coerce")
    mask = (ef <= pd.Timestamp(end)) & (et.isna() | (et >= pd.Timestamp(start)))
    return frame.loc[mask]


class ProviderContractMixin:
    """四个 provider 共用的契约（子类只提供夹具，不重写检查项）。"""

    #: 展示用标签（失败信息里能看出是哪个实现）
    tag: ClassVar[str] = "provider"
    #: 指数代码（必须带交易所后缀）
    index_code: ClassVar[str] = "000300.SH"

    _provider_cache: ClassVar[dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # 夹具钩子（子类实现）
    # ------------------------------------------------------------------ #
    @classmethod
    def build_provider(cls):  # pragma: no cover - 由子类实现
        raise NotImplementedError

    @classmethod
    def provider(cls):
        """类级缓存的 provider 实例（`setUp` 在零依赖运行器里不会被调用）。"""
        key = cls.__name__
        if key not in cls._provider_cache:
            cls._provider_cache[key] = cls.build_provider()
        return cls._provider_cache[key]

    @classmethod
    def known_symbols(cls) -> list[str]:
        """至少一个**确定存在**的标的（供 C5~C8 使用）。"""
        meta, _ = cls.provider().fetch_symbol_meta([])
        if len(meta) == 0:
            raise AssertionError("契约要求 provider 至少能提供一个标的的元信息")
        return sorted(meta["symbol"].astype(str).tolist())[:2]

    @classmethod
    def unknown_symbol(cls) -> str:
        return "999999.SZ"

    # ------------------------------------------------------------------ #
    # C1：能力声明 ↔ 实际行为
    # ------------------------------------------------------------------ #
    def test_c01_declared_capabilities_match_actual_behavior(self):
        provider = self.provider()
        caps = provider.capabilities()
        symbols = self.known_symbols()

        # 必需能力：声明为真就必须真的拿得到数据
        assert caps.daily_bars is True, f"[{self.tag}] 契约要求行情能力"
        bars, _ = provider.fetch_bars(symbols, START, END)
        assert len(bars) > 0, f"[{self.tag}] 声明 daily_bars=True 却取不到行情"

        # 方法型能力：声明 True → 非空；声明 False → 必须有 warning（返回数据或空表都可以，
        # 但不允许**无声地**返回）
        checks: list[tuple[str, Any]] = [
            ("listing_dates", lambda: provider.fetch_symbol_meta(symbols)),
            ("index_members", lambda: provider.fetch_index_members(self.index_code, START, END)),
            ("fundamentals", lambda: provider.fetch_fundamentals(symbols, START, END)),
            ("industry", lambda: provider.fetch_industry(symbols)),
        ]
        for capability, fetch in checks:
            frame, prov = fetch()
            if getattr(caps, capability):
                assert len(frame) > 0, f"[{self.tag}] 声明 {capability}=True 却返回空表"
            else:
                assert prov.warnings, (
                    f"[{self.tag}] 未声明 {capability} 时返回了数据/空表却没有 warning（静默降级）"
                )

        # 反例：请求不存在的标的 → 空表 + warning，**不抛异常**（多标的取数约定第 1 条）
        empty, empty_prov = provider.fetch_bars([self.unknown_symbol()], START, END)
        assert len(empty) == 0
        assert empty_prov.warnings, f"[{self.tag}] 未知标的必须给出 warning"

        # 反例：不存在的指数 → 空表 + warning，不抛异常
        no_index, no_index_prov = provider.fetch_index_members("000905.SH", START, END)
        assert isinstance(no_index, pd.DataFrame)
        if len(no_index) == 0:
            assert no_index_prov.warnings, f"[{self.tag}] 空成分必须给出 warning"

    # ------------------------------------------------------------------ #
    # C2：missing_required 边界
    # ------------------------------------------------------------------ #
    def test_c02_missing_required_boundary(self):
        provider = self.provider()
        caps = provider.capabilities()

        # 需求全开时：返回的缺失项 = 声明为 False 的项，且**不抛异常**
        missing = caps.missing_required(
            need_index_members=True,
            need_fundamentals=True,
            need_adjustment_factors=True,
            need_price_limits=True,
        )
        declared_false = {
            name
            for name in (
                "daily_bars",
                "listing_dates",
                "index_members",
                "fundamentals",
                "adjustment_factors",
                "price_limits",
            )
            if not getattr(caps, name)
        }
        assert set(missing) == declared_false, f"[{self.tag}] 缺失清单与声明不一致"

        # 必需能力不缺失时：只查必需项必须为空（正例）
        if caps.daily_bars and caps.listing_dates:
            assert caps.missing_required() == ()
        # 顺序稳定（便于断言与展示）
        assert list(missing) == sorted(missing) or set(missing) <= set(BACKTEST_REQUIRED) | {
            "index_members",
            "fundamentals",
            "adjustment_factors",
            "price_limits",
        }

        # 边界（provider 无关）：声明 5 项、只缺 1 项时必须只返回那一项，且不抛异常
        from aqs.data.provider import ProviderCapabilities

        partial = ProviderCapabilities(
            daily_bars=True,
            listing_dates=True,
            index_members=False,
            fundamentals=True,
            adjustment_factors=True,
        )
        assert partial.missing_required(need_index_members=True, need_fundamentals=True) == (
            "index_members",
        )
        # 反例：不需要时不得把它算作缺失
        assert partial.missing_required(need_fundamentals=True) == ()

    # ------------------------------------------------------------------ #
    # C3：degradation_notes 与 capabilities() 自洽
    # ------------------------------------------------------------------ #
    def test_c03_degradation_notes_match_capabilities(self):
        caps = self.provider().capabilities()
        notes = degradation_notes(
            caps,
            need_index_members=True,
            need_fundamentals=True,
            need_adjustment_factors=True,
            need_price_limits=True,
        )
        text = " | ".join(notes)
        missing = caps.missing_required(
            need_index_members=True,
            need_fundamentals=True,
            need_adjustment_factors=True,
            need_price_limits=True,
        )

        # 正向：每个缺失能力都必须有对应披露，且文本取自条件必需能力的说明
        for name in missing:
            assert name in text, f"[{self.tag}] 缺失能力 {name} 未在降级披露中出现"
            if name in CONDITIONAL_REQUIRED:
                assert CONDITIONAL_REQUIRED[name][:12] in text, (
                    f"[{self.tag}] {name} 的披露文本与 CONDITIONAL_REQUIRED 不一致"
                )
        # 反向：没缺的能力不得被说成缺失
        for name, description in CONDITIONAL_REQUIRED.items():
            if getattr(caps, name):
                assert name not in text, f"[{self.tag}] {name} 声明为可用却被披露为缺失"

        # 退市信息缺失是**独立**风险点：即使没开 need 也要提示幸存者偏差
        if not caps.delistings:
            assert "幸存者偏差" in text
        else:
            assert "退市" not in text or "幸存者偏差" not in text

        # 能力说明（provider 自己的 notes）不得与声明冲突
        for note in caps.notes:
            assert isinstance(note, str) and note.strip(), f"[{self.tag}] notes 含空条目"

    # ------------------------------------------------------------------ #
    # C4：指数成分窗口过滤（区间相交）
    # ------------------------------------------------------------------ #
    def test_c04_index_members_use_interval_overlap_filtering(self):
        provider = self.provider()
        reference, ref_prov = provider.fetch_index_members(self.index_code, START, END)
        if len(reference) == 0:
            # 声明不可用或该源没有成分数据：必须给出 warning，且不得抛异常
            assert ref_prov.warnings, f"[{self.tag}] 空成分必须有 warning"
            return

        earliest = pd.to_datetime(reference["effective_from"], errors="coerce").min()
        assert pd.notna(earliest)

        # 正例：子窗口起点**晚于**最早生效日 → 那些「窗口开始前就已生效」的成分必须保留
        sub_start = earliest + pd.Timedelta(days=30)
        assert sub_start <= pd.Timestamp(END), "契约区间太短，无法派生子窗口"
        sub, _ = provider.fetch_index_members(
            self.index_code, sub_start.date(), pd.Timestamp(END).date()
        )
        expected = overlapping(reference, sub_start, END)
        got = set(map(tuple, sub[["index_code", "symbol"]].astype(str).to_numpy()))
        want = set(map(tuple, expected[["index_code", "symbol"]].astype(str).to_numpy()))
        assert want <= got, (
            f"[{self.tag}] 跨窗口生效的成分被丢掉（effective_from >= start 的错误语义）："
            f"缺少 {sorted(want - got)}"
        )
        # 反向：返回的行必须真的与窗口相交（不得有假阳性）
        assert len(overlapping(sub, sub_start, END)) == len(sub), (
            f"[{self.tag}] 返回了与请求窗口不相交的成分"
        )

        # 反例：窗口完全早于最早生效日 → 必须为空
        pre_start = earliest - pd.Timedelta(days=60)
        pre_end = earliest - pd.Timedelta(days=1)
        pre, pre_prov = provider.fetch_index_members(
            self.index_code, pre_start.date(), pre_end.date()
        )
        assert len(pre) == 0, f"[{self.tag}] 窗口早于生效日却返回了 {len(pre)} 行"
        assert pre_prov.warnings, f"[{self.tag}] 空结果必须给出 warning"
        # 反向自检：这两个窗口确实不相交（否则上面的断言没有意义）
        assert pre_end < earliest <= sub_start

    # ------------------------------------------------------------------ #
    # C5~C7：字段规范 / 主键 / PIT
    # ------------------------------------------------------------------ #
    def test_c05_frames_follow_canonical_field_spec(self):
        provider = self.provider()
        symbols = self.known_symbols()
        caps = provider.capabilities()

        bars, _ = provider.fetch_bars(symbols, START, END)
        missing = [c for c in BARS_REQUIRED_COLUMNS if c not in bars.columns]
        assert not missing, f"[{self.tag}] 行情缺少 canonical 列：{missing}"

        meta, _ = provider.fetch_symbol_meta(symbols)
        if len(meta):
            assert set(META_REQUIRED_COLUMNS) <= set(meta.columns), (
                f"[{self.tag}] 元信息缺少 {META_REQUIRED_COLUMNS}"
            )

        members, _ = provider.fetch_index_members(self.index_code, START, END)
        if len(members):
            assert set(MEMBER_COLUMNS) <= set(members.columns), (
                f"[{self.tag}] 指数成分缺少 {MEMBER_COLUMNS}"
            )

        if caps.fundamentals:
            fundamentals, _ = provider.fetch_fundamentals(symbols, START, END)
            if len(fundamentals):
                assert set(FUNDAMENTAL_KEYS) <= set(fundamentals.columns), (
                    f"[{self.tag}] 财务缺少 {FUNDAMENTAL_KEYS}"
                )

        if caps.industry:
            industry, _ = provider.fetch_industry(symbols)
            if len(industry):
                assert {"symbol", "industry"} <= set(industry.columns), (
                    f"[{self.tag}] 行业缺少 symbol/industry"
                )

    def test_c06_primary_keys_are_unique(self):
        provider = self.provider()
        symbols = self.known_symbols()

        bars, _ = provider.fetch_bars(symbols, START, END)
        assert not bars.duplicated(subset=["date", "symbol"]).any(), (
            f"[{self.tag}] 行情主键 (date, symbol) 重复"
        )

        meta, _ = provider.fetch_symbol_meta(symbols)
        if len(meta):
            assert not meta.duplicated(subset=["symbol"]).any(), (
                f"[{self.tag}] 元信息主键 symbol 重复"
            )

        members, _ = provider.fetch_index_members(self.index_code, START, END)
        if len(members):
            assert not members.duplicated(
                subset=["index_code", "symbol", "effective_from"]
            ).any(), f"[{self.tag}] 指数成分主键重复"

        if provider.capabilities().fundamentals:
            fundamentals, _ = provider.fetch_fundamentals(symbols, START, END)
            if len(fundamentals):
                assert not fundamentals.duplicated(subset=list(FUNDAMENTAL_KEYS)).any(), (
                    f"[{self.tag}] 财务主键 (symbol, report_period, announce_date) 重复"
                )

    def test_c07_pit_columns_present_and_rows_within_range(self):
        provider = self.provider()
        symbols = self.known_symbols()
        caps = provider.capabilities()

        bars, _ = provider.fetch_bars(symbols, START, END)
        dates = pd.to_datetime(bars["date"], errors="coerce")
        assert dates.notna().all(), f"[{self.tag}] 行情日期存在无法解析的值"
        assert dates.min() >= pd.Timestamp(START), f"[{self.tag}] 行情早于请求区间（PIT 越界）"
        assert dates.max() <= pd.Timestamp(END), f"[{self.tag}] 行情晚于请求区间"

        if caps.listing_dates:
            meta, _ = provider.fetch_symbol_meta(symbols)
            assert len(meta) > 0
            assert pd.to_datetime(meta["list_date"], errors="coerce").notna().all(), (
                f"[{self.tag}] 声明 listing_dates=True 但 list_date 有空值（不得静默兜底）"
            )

        if caps.fundamentals:
            fundamentals, _ = provider.fetch_fundamentals(symbols, START, END)
            if len(fundamentals):
                announce = pd.to_datetime(fundamentals["announce_date"], errors="coerce")
                assert announce.notna().all(), f"[{self.tag}] 财务缺少公告日（未来函数风险）"
                assert (announce >= pd.Timestamp(START)).all(), (
                    f"[{self.tag}] 财务公告日早于请求区间"
                )
                assert (announce <= pd.Timestamp(END)).all(), (
                    f"[{self.tag}] 财务公告日晚于请求区间"
                )
                period = pd.to_datetime(fundamentals["report_period"], errors="coerce")
                assert (announce > period).all(), (
                    f"[{self.tag}] 公告日不得早于报告期（否则是财务未来函数）"
                )

        members, _ = provider.fetch_index_members(self.index_code, START, END)
        if len(members):
            assert (
                pd.to_datetime(members["effective_from"], errors="coerce") <= pd.Timestamp(END)
            ).all(), f"[{self.tag}] 成分生效日晚于请求区间"

    # ------------------------------------------------------------------ #
    # C8：Provenance 契约
    # ------------------------------------------------------------------ #
    def test_c08_provenance_contract(self):
        provider = self.provider()
        symbols = self.known_symbols()

        samples: list[tuple[str, pd.DataFrame, Provenance, int]] = []
        bars, bars_prov = provider.fetch_bars(symbols, START, END)
        samples.append(("bars", bars, bars_prov, len(bars)))

        meta, meta_prov = provider.fetch_symbol_meta(symbols)
        samples.append(("symbol_meta", meta, meta_prov, len(meta)))

        members, members_prov = provider.fetch_index_members(self.index_code, START, END)
        samples.append(("index_members", members, members_prov, len(members)))

        fundamentals, fund_prov = provider.fetch_fundamentals(symbols, START, END)
        samples.append(("fundamentals", fundamentals, fund_prov, len(fundamentals)))

        industry, industry_prov = provider.fetch_industry(symbols)
        samples.append(("industry", industry, industry_prov, len(industry)))

        days, cal_prov = provider.fetch_trading_calendar(START, END)
        samples.append(("calendar", pd.DataFrame({"date": pd.to_datetime(days)}), cal_prov, len(days)))
        # 交易日历不含标的：`symbols` 必须是 0（不得沿用行情的标的数）
        assert cal_prov.symbols == 0, (
            f"[{self.tag}] 日历的 Provenance.symbols 应为 0，实际 {cal_prov.symbols}"
        )

        for name, frame, prov, expected_rows in samples:
            assert isinstance(prov, Provenance), f"[{self.tag}] {name} 未返回 Provenance"
            assert prov.rows == expected_rows, (
                f"[{self.tag}] {name} 的 Provenance.rows={prov.rows} 与实际 {expected_rows} 不符"
            )
            assert prov.fetched_at.tzinfo is not None, (
                f"[{self.tag}] {name} 的时间戳必须带时区（数据层统一 UTC）"
            )
            assert prov.source == provider.name, f"[{self.tag}] {name} 的 source 应为 {provider.name}"
            if "symbol" in frame.columns and len(frame):
                assert prov.symbols == frame["symbol"].nunique(), (
                    f"[{self.tag}] {name} 的 symbols 计数与数据不一致"
                )

        assert days == sorted(set(days)), f"[{self.tag}] 交易日历必须升序去重"
        assert all(pd.Timestamp(d) >= pd.Timestamp(START) for d in days)
        assert all(pd.Timestamp(d) <= pd.Timestamp(END) for d in days)
