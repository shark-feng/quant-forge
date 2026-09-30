"""数据仓库测试：PIT 访问、复权、停牌/退市、未来函数防护。"""

from __future__ import annotations

import pandas as pd

from tests.compat import approx, raises
from tests.tools import bar_row, dates, make_bars, make_store

from aqs.config.schema import DataConfig, UniverseConfig, construct
from aqs.core.exceptions import DataError, LookaheadError

D = dates(30)
SYM = "600000.SH"
SYM2 = "000001.SZ"


def _market() -> tuple[pd.DataFrame, pd.DataFrame]:
    """两个标的、30 个交易日；SYM 在 D[5] 停牌，SYM2 在 D[20] 退市。"""
    rows = []
    for i, d in enumerate(D):
        rows.append(
            bar_row(
                d,
                SYM,
                close=10.0 + i * 0.1,
                volume=0.0 if i == 5 else 1_000_000.0,
                is_suspended=(i == 5),
                adj_factor=1.0 if i < 10 else 2.0,
            )
        )
        if i <= 20:
            rows.append(
                bar_row(
                    d,
                    SYM2,
                    close=20.0 + i * 0.2,
                    amount=2_000_000.0,
                    delist_date=D[20],
                    list_date=D[0],
                )
            )
    bars = make_bars(rows)
    fundamentals = pd.DataFrame(
        [
            # 2021 年报：在回测区间开始前已公告 → 始终可见
            {"symbol": SYM, "report_period": "2021-12-31", "announce_date": "2022-01-20", "roe": 0.10},
            # 2022 一季报：报告期 2022-03-31，公告日 D[22] = 2022-03-31
            {"symbol": SYM, "report_period": "2022-03-31", "announce_date": D[22], "roe": 0.15},
        ]
    )
    return bars, fundamentals


def _store(**kwargs):
    bars, fundamentals = _market()
    return make_store(bars, fundamentals=fundamentals, **kwargs)


# --------------------------------------------------------------------------- #
# 基础查询
# --------------------------------------------------------------------------- #
def test_describe_and_symbols():
    store = _store()
    info = store.describe()
    assert info["symbols"] == 2
    assert info["trading_days"] == 30
    assert info["has_fundamentals"] is True
    assert info["has_index_history"] is False
    assert store.symbols() == sorted([SYM, SYM2])


def test_bars_on_returns_only_that_day():
    store = _store()
    bars = store.bars_on(D[0])
    assert set(bars) == {SYM, SYM2}
    bars30 = store.bars_on(D[25])
    assert set(bars30) == {SYM}  # SYM2 已退市（无行情）


def test_history_window_and_end_bound():
    store = _store()
    hist = store.history(SYM, D[10], 5)
    assert len(hist) == 5
    assert pd.Timestamp(hist["date"].max()).date() == D[10]
    assert pd.Timestamp(hist["date"].min()).date() == D[6]


def test_history_shorter_than_window():
    store = _store()
    hist = store.history(SYM, D[2], 10)
    assert len(hist) == 3


def test_adjusted_prices_available_from_store():
    store = _store()
    bar = store.bar(SYM, D[15])
    assert bar is not None
    # 复权因子在第 10 天由 1.0 变为 2.0，基准为首日 1.0 → 后复权价 = 2 × 原始价
    assert bar.close_adj == approx(bar.close * 2.0)
    assert bar.adj_factor == approx(2.0)
    assert bar.base_adj_factor == approx(1.0)


def test_adv_uses_only_past_data():
    store = _store()
    hist = store.history(SYM2, D[10], 5, fields=["amount"])
    assert store.adv(SYM2, D[10], 5) == approx(float(hist["amount"].mean()))
    # SYM2 的 amount 恒为 200 万、volume 恒为 100 万股
    assert store.adv(SYM2, D[10], 5) == approx(2_000_000.0)
    assert store.adv(SYM2, D[10], 5, field_name="volume") == approx(1_000_000.0)


def test_adv_returns_zero_when_insufficient_history():
    store = _store()
    assert store.adv(SYM, D[2], 20) == 0.0


def test_rolling_value_matches_manual_mean():
    store = _store()
    # D[5] 停牌（amount = 0），D[6..9] 的 amount = close × 100 万股
    expected = (0.0 + sum((10.0 + i * 0.1) * 1_000_000.0 for i in range(6, 10))) / 5.0
    assert store.rolling_value(SYM, "amount", D[9], 5) == approx(expected)


# --------------------------------------------------------------------------- #
# 可交易性 / 停牌 / 退市
# --------------------------------------------------------------------------- #
def test_suspension_flags():
    store = _store()
    assert store.is_tradable(SYM, D[4]) is True
    assert store.is_tradable(SYM, D[5]) is False
    assert store.is_suspended(SYM, D[5]) is True
    assert store.bar(SYM, D[5]).is_suspended is True
    assert store.listing_status(SYM, D[5]) == "suspended"
    assert store.listing_status(SYM, D[6]) == "listed"


def test_delisted_symbol_absent_after_delist_date():
    store = _store()
    assert SYM2 in store.all_listed(D[15])
    assert SYM2 not in store.all_listed(D[21])
    assert store.listing_status(SYM2, D[21]) == "delisted"
    assert store.is_tradable(SYM2, D[21]) is False


def test_next_tradable_day_uses_store():
    store = _store()
    assert store.calendar.next_tradable_day(SYM, D[4]) == D[6]
    assert store.calendar.next_tradable_day(SYM2, D[20]) is None  # 退市后无交易日


def test_unknown_symbol_raises():
    store = _store()
    with raises(DataError):
        store.symbol_meta("999999.SH")


# --------------------------------------------------------------------------- #
# 未来函数防护（核心验收项）
# --------------------------------------------------------------------------- #
def test_history_never_returns_future_rows():
    store = _store()
    end = D[8]
    hist = store.history(SYM, end, 30)
    assert pd.Timestamp(hist["date"].max()).date() <= end


def test_as_of_view_blocks_future_request():
    store = _store()
    view = store.as_of(D[8])
    assert view.history(SYM, D[8], 5).shape[0] == 5
    with raises(LookaheadError):
        view.history(SYM, D[9], 5)
    assert len(view.violations) == 1
    assert view.violations[0]["requested"] == str(D[9])


def test_as_of_view_blocks_future_bars_on_and_universe():
    store = _store()
    view = store.as_of(D[8])
    view.bars_on(D[8])
    for call in (lambda: view.bars_on(D[9]), lambda: view.bar(SYM, D[20]), lambda: view.universe(D[15])):
        with raises(LookaheadError):
            call()
    assert len(view.violations) == 3


def test_store_level_as_of_guard():
    store = _store()
    with raises(LookaheadError):
        store.history(SYM, D[9], 5, as_of=D[8])
    with raises(LookaheadError):
        store.bars_on(D[9], as_of=D[8])
    with raises(LookaheadError):
        store.adv(SYM, D[9], 5, as_of=D[8])
    # 合法调用不受影响
    assert store.bars_on(D[8], as_of=D[8])


def test_as_of_view_trading_days_truncated():
    store = _store()
    view = store.as_of(D[8])
    days = view.trading_days()
    assert days[-1] == D[8]
    with raises(LookaheadError):
        view.trading_days(end=D[9])


def test_adv_view_guard():
    store = _store()
    view = store.as_of(D[10])
    assert view.adv(SYM2, D[10], 5) == approx(2_000_000.0)
    with raises(LookaheadError):
        view.adv(SYM2, D[11], 5)


# --------------------------------------------------------------------------- #
# 财务数据（公告日口径）
# --------------------------------------------------------------------------- #
def test_fundamentals_follow_announce_date():
    store = _store()
    # 一季报在 D[22] 才公告
    before = store.fundamentals(D[21], symbols=[SYM])
    assert float(before.loc[SYM, "roe"]) == approx(0.10)
    assert pd.Timestamp(before.loc[SYM, "report_period"]).date().isoformat() == "2021-12-31"
    after = store.fundamentals(D[22], symbols=[SYM])
    assert float(after.loc[SYM, "roe"]) == approx(0.15)
    assert pd.Timestamp(after.loc[SYM, "report_period"]).date().isoformat() == "2022-03-31"


def test_fundamentals_via_pit_view_uses_as_of():
    store = _store()
    view = store.as_of(D[21])
    assert float(view.fundamentals(symbols=[SYM]).loc[SYM, "roe"]) == approx(0.10)
    view2 = store.as_of(D[25])
    assert float(view2.fundamentals(symbols=[SYM]).loc[SYM, "roe"]) == approx(0.15)


def test_fundamentals_missing_returns_empty():
    bars, _ = _market()
    store = make_store(bars)
    assert store.fundamentals(D[0]).empty


# --------------------------------------------------------------------------- #
# 股票池（默认测试配置不做过滤）
# --------------------------------------------------------------------------- #
def test_universe_default_all_mode():
    store = _store()
    uni = store.universe(D[2])
    assert set(uni) == {SYM, SYM2}
    assert uni == sorted(uni)


def test_universe_filters_suspended_and_delisted():
    store = _store()
    assert SYM not in store.universe(D[5])          # 停牌
    assert SYM2 not in store.universe(D[25])        # 已退市


def test_universe_stats_available():
    store = _store()
    store.universe(D[5])
    stats = store.universe_builder.last_stats
    assert stats is not None
    assert stats.date == D[5]
    assert stats.candidates == 2
    assert stats.final == 1


def test_quality_report_attached():
    store = _store()
    assert store.quality is not None
    assert store.quality.ok


def test_strict_quality_raises():
    bad = make_bars([bar_row(D[0], SYM, close=-1.0)])
    with raises(Exception) as exc:
        make_store(bad, data_config=construct(DataConfig, {"quality": {"strict": True}}))
    assert "数据质量" in str(exc.value) or "非正值" in str(exc.value)


def test_listed_days_from_list_date():
    bars = make_bars([bar_row(d, SYM, close=10.0, list_date=D[0]) for d in D[:10]])
    store = make_store(bars)
    assert store.listed_days(SYM, D[0]) == 1
    assert store.listed_days(SYM, D[9]) == 10
    assert store.bar(SYM, D[9]).listed_days == 10


def test_index_members_query():
    bars, _ = _market()
    members = pd.DataFrame(
        [
            {"index_code": "000300.SH", "symbol": SYM, "effective_from": D[0], "effective_to": D[10]},
            {"index_code": "000300.SH", "symbol": SYM2, "effective_from": D[11], "effective_to": None},
        ]
    )
    store = make_store(bars, index_members=members, universe_config=UniverseConfig(mode="index"))
    assert store.index_members("000300.SH", D[5]) == [SYM]
    assert store.index_members("000300.SH", D[15]) == [SYM2]
    assert store.index_members("000905.SH", D[15]) == []
    assert store.universe(D[4]) == [SYM]
    assert store.universe(D[5]) == []  # 指数成分内但当日停牌 → 剔除
    assert store.universe(D[15]) == [SYM2]
