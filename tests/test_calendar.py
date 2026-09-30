"""交易日历测试（顺延、区间、时间戳）。"""

from __future__ import annotations

import pandas as pd

from tests.compat import raises
from tests.tools import bar_row, dates, make_bars

from aqs.core.calendar import TradingCalendar
from aqs.core.enums import SessionPhase
from aqs.core.exceptions import DataError

D = dates(10)


def test_next_and_prev_trading_day():
    cal = TradingCalendar(D)
    assert cal.next_trading_day(D[0]) == D[1]
    assert cal.next_trading_day(D[5], 3) == D[8]
    assert cal.next_trading_day(D[9]) is None
    assert cal.prev_trading_day(D[5]) == D[4]
    assert cal.prev_trading_day(D[5], 3) == D[2]
    assert cal.prev_trading_day(D[0]) is None


def test_include_self_semantics():
    cal = TradingCalendar(D)
    assert cal.next_trading_day(D[3], include_self=True) == D[3]
    assert cal.next_trading_day(D[3], include_self=False) == D[4]
    assert cal.prev_trading_day(D[3], include_self=True) == D[3]
    assert cal.prev_trading_day(D[3], include_self=False) == D[2]
    # 非交易日：向后顺延
    holiday = D[3] + pd.Timedelta(days=1)
    assert cal.is_trading_day(holiday) is False
    assert cal.shift(holiday, 0) == D[4]


def test_sessions_and_count():
    cal = TradingCalendar(D)
    assert cal.sessions(D[2], D[5]) == D[2:6]
    assert cal.sessions() == D
    assert cal.count_between(D[0], D[9]) == 10
    assert len(cal) == 10
    assert cal.first_day == D[0]
    assert cal.last_day == D[9]


def test_invalid_n_rejected():
    cal = TradingCalendar(D)
    with raises(ValueError):
        cal.next_trading_day(D[0], 0)
    with raises(ValueError):
        cal.prev_trading_day(D[0], -1)


def test_index_of_strict():
    cal = TradingCalendar(D)
    assert cal.index_of(D[4]) == 4
    assert cal.index_of(pd.Timestamp("2000-01-03"), strict=False) is None
    with raises(DataError):
        cal.index_of(pd.Timestamp("2000-01-03"))


def test_from_bars_keeps_suspended_days_as_trading_days():
    rows = [bar_row(D[0], close=10.0), bar_row(D[1], close=10.0, is_suspended=True), bar_row(D[2], close=10.0)]
    cal = TradingCalendar.from_bars(make_bars(rows))
    assert len(cal) == 3  # 停牌日仍然是交易日
    assert D[1] in cal


def test_next_tradable_day_skips_suspension():
    suspended = {D[2], D[3], D[4]}
    cal = TradingCalendar(D)
    cal.bind_tradable(lambda sym, day: day not in suspended)
    assert cal.next_tradable_day("600000.SH", D[1]) == D[5]
    assert cal.next_tradable_day("600000.SH", D[2]) == D[5]
    assert cal.next_tradable_day("600000.SH", D[2], include_self=True) == D[5]
    assert cal.next_tradable_day("600000.SH", D[5], include_self=True) == D[5]
    assert cal.tradable_sessions("600000.SH", D[0], D[6]) == [D[0], D[1], D[5], D[6]]


def test_next_tradable_day_returns_none_when_never_tradable():
    cal = TradingCalendar(D)
    cal.bind_tradable(lambda sym, day: False)
    assert cal.next_tradable_day("600000.SH", D[0], max_lookahead=5) is None


def test_next_tradable_day_requires_binding():
    cal = TradingCalendar(D)
    with raises(DataError):
        cal.next_tradable_day("600000.SH", D[0])


def test_phase_timestamps_are_ordered():
    cal = TradingCalendar(D)
    ts = [cal.timestamp(D[0], phase) for phase in (SessionPhase.PRE_OPEN, SessionPhase.OPEN, SessionPhase.CLOSE, SessionPhase.POST_CLOSE)]
    assert ts == sorted(ts)
    assert ts[0].hour == 9 and ts[0].minute == 0
    assert ts[1].hour == 9 and ts[1].minute == 30
    assert ts[2].hour == 15 and ts[2].minute == 0
    assert ts[3].hour == 15 and ts[3].minute == 10


def test_timestamp_on_non_trading_day_rejected():
    cal = TradingCalendar(D)
    with raises(DataError):
        cal.timestamp(pd.Timestamp("2000-01-03"), SessionPhase.CLOSE)


def test_validate_span_and_from_file_missing():
    cal = TradingCalendar(D)
    cal.validate_span(D[0], D[9], min_days=10)
    with raises(DataError):
        cal.validate_span(D[0], D[9], min_days=11)
    with raises(DataError):
        TradingCalendar.from_file("data/not_exists.csv")
