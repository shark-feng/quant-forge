"""M4-2：数据源抽象（能力声明 / 溯源 / 健康检查 / 协议）。

对应 `docs/18_m4_dataprovider.md` §3.1 与 §4.1。
契约测试基类在 `tests/contracts/provider_contract.py`（M4-9），本模块只测抽象本身。
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from tests.compat import approx

from aqs.data.provider import (
    BACKTEST_REQUIRED,
    DataProvider,
    ProviderCapabilities,
    ProviderHealth,
    Provenance,
    degradation_notes,
)


# --------------------------------------------------------------------------- #
# 1. 能力声明
# --------------------------------------------------------------------------- #
def test_full_capabilities_declares_everything_except_intraday():
    """`full()` 声明全部日频能力，但**不得**虚报 intraday（日频引擎不消费日内数据）。"""
    caps = ProviderCapabilities.full()
    assert caps.daily_bars is True
    assert caps.adjustment_factors is True
    assert caps.index_members is True
    assert caps.fundamentals is True
    assert caps.listing_dates is True
    assert caps.intraday is False, "日频数据源不应声明 intraday 能力"
    assert caps.default_adjust == "hfq"


def test_missing_required_is_conditional_on_needs():
    """必需能力随上层需求变化：默认只要日线+上市日，需要指数/财务时才追加。"""
    assert BACKTEST_REQUIRED == ("daily_bars", "listing_dates")

    empty = ProviderCapabilities()
    assert empty.missing_required() == ("daily_bars", "listing_dates")
    assert empty.missing_required(need_index_members=True) == (
        "daily_bars",
        "listing_dates",
        "index_members",
    )
    assert empty.missing_required(need_fundamentals=True, need_index_members=True) == (
        "daily_bars",
        "listing_dates",
        "index_members",
        "fundamentals",
    )

    full = ProviderCapabilities.full()
    assert full.missing_required(need_index_members=True, need_fundamentals=True) == ()

    # 顺序固定，便于断言与报告展示
    partial = ProviderCapabilities(daily_bars=True, listing_dates=True, index_members=False)
    assert partial.missing_required(need_index_members=True) == ("index_members",)
    assert partial.missing_required() == ()


def test_capabilities_as_dict_round_trips_all_fields():
    caps = ProviderCapabilities.full(notes=("synthetic",))
    d = caps.as_dict()
    for name in (
        "daily_bars",
        "adjustment_factors",
        "suspensions",
        "price_limits",
        "st_flags",
        "listing_dates",
        "delistings",
        "index_members",
        "fundamentals",
        "industry",
        "market_cap",
        "intraday",
        "default_adjust",
        "notes",
    ):
        assert name in d, f"as_dict 缺少字段：{name}"
    assert d["notes"] == ["synthetic"]
    assert d["intraday"] is False


# --------------------------------------------------------------------------- #
# 2. 降级披露（不得静默兜底）
# --------------------------------------------------------------------------- #
def test_degradation_notes_disclose_missing_capabilities():
    # 核心能力齐全 + 有退市信息 → 没有任何降级，也不应有噪音
    clean = ProviderCapabilities(daily_bars=True, listing_dates=True, delistings=True)
    assert degradation_notes(clean) == (), "能力齐全时不应产生降级披露"

    # 缺指数成分：只有在上层确实需要时才披露
    assert degradation_notes(clean, need_index_members=False) == ()
    notes = degradation_notes(clean, need_index_members=True)
    assert len(notes) == 1
    assert "index_members" in notes[0]
    assert "幸存者偏差" in notes[0]

    notes_all = degradation_notes(
        clean, need_index_members=True, need_fundamentals=True, need_adjustment_factors=True
    )
    joined = " | ".join(notes_all)
    assert "index_members" in joined and "fundamentals" in joined and "adjustment_factors" in joined
    assert len(notes_all) == 3

    # 退市信息缺失是**独立**风险点：与是否使用指数股票池无关，默认就要披露
    no_delist = ProviderCapabilities(daily_bars=True, listing_dates=True)
    base_notes = degradation_notes(no_delist)
    assert len(base_notes) == 1
    assert "幸存者偏差" in base_notes[0]
    assert "退市" in base_notes[0]


def test_degradation_notes_flags_missing_required_capabilities():
    """缺 `daily_bars`/`listing_dates` 属于致命缺失，必须显式点名。"""
    caps = ProviderCapabilities()  # 什么都没有
    notes = degradation_notes(caps)
    assert any("daily_bars" in n and "必需" in n for n in notes)
    assert any("listing_dates" in n and "必需" in n for n in notes)
    # 也缺退市信息 → 同时提示幸存者偏差
    assert any("幸存者偏差" in n for n in notes)

    full = ProviderCapabilities.full()
    assert full.delistings is True
    assert degradation_notes(full, need_index_members=True, need_fundamentals=True) == ()


# --------------------------------------------------------------------------- #
# 3. 溯源与健康检查
# --------------------------------------------------------------------------- #
def test_provenance_serialises_timestamps_and_warnings():
    p = Provenance(
        source="fallback:parquet",
        fetched_at=datetime(2026, 9, 30, 12, 34, 56),
        cache_hit=True,
        rows=1200,
        symbols=3,
        params_hash="abc123",
        warnings=("空结果不重试",),
    )
    d = p.as_dict()
    assert d["source"] == "fallback:parquet"
    assert d["fetched_at"] == "2026-09-30T12:34:56"
    assert d["cache_hit"] is True
    assert d["rows"] == 1200
    assert d["params_hash"] == "abc123"
    assert d["warnings"] == ["空结果不重试"]


def test_provider_health_serialises_and_rounds_latency():
    h = ProviderHealth(
        ok=False,
        checked_at=datetime(2026, 9, 30, 9, 0, 0),
        latency_ms=12.3456,
        details={"endpoints": 4},
        errors=("bars 接口超时",),
    )
    d = h.as_dict()
    assert d["ok"] is False
    assert d["checked_at"] == "2026-09-30T09:00:00"
    assert d["latency_ms"] == approx(12.346)
    assert d["details"] == {"endpoints": 4}
    assert d["errors"] == ["bars 接口超时"]


# --------------------------------------------------------------------------- #
# 4. 协议可检查性
# --------------------------------------------------------------------------- #
class _CompleteStub:
    """完整实现协议的最小对象。"""

    name = "stub"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities.full()

    def health_check(self) -> ProviderHealth:
        return ProviderHealth(ok=True, checked_at=datetime.now(), latency_ms=0.0)

    def fetch_bars(self, symbols, start, end, *, adjust="none"):
        return pd.DataFrame(), Provenance("stub", datetime.now(), True, 0, 0)

    def fetch_symbol_meta(self, symbols):
        return pd.DataFrame(), Provenance("stub", datetime.now(), True, 0, 0)

    def fetch_index_members(self, index_code, start, end):
        return pd.DataFrame(), Provenance("stub", datetime.now(), True, 0, 0)

    def fetch_fundamentals(self, symbols, start, end):
        return pd.DataFrame(), Provenance("stub", datetime.now(), True, 0, 0)

    def fetch_trading_calendar(self, start, end):
        return [], Provenance("stub", datetime.now(), True, 0, 0)

    def fetch_industry(self, symbols):
        return pd.DataFrame(), Provenance("stub", datetime.now(), True, 0, 0)


class _IncompleteStub:
    """缺 `fetch_fundamentals` 的对象，不应通过协议检查。"""

    name = "incomplete"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def health_check(self) -> ProviderHealth:
        return ProviderHealth(ok=True, checked_at=datetime.now(), latency_ms=0.0)

    def fetch_bars(self, symbols, start, end, *, adjust="none"):
        return pd.DataFrame(), Provenance("x", datetime.now(), True, 0, 0)


def test_data_provider_protocol_is_runtime_checkable():
    assert isinstance(_CompleteStub(), DataProvider), "完整实现应通过协议检查"
    assert not isinstance(_IncompleteStub(), DataProvider), "缺方法的对象不应通过协议检查"
