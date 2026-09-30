"""价格突破策略测试（含「不含 T 日」的未来函数防线）。"""

from __future__ import annotations

from tests.compat import approx, raises
from tests.tools import dates, make_bars, make_context, make_store, price_series

from aqs.core.enums import SignalDirection
from aqs.core.exceptions import ConfigError
from aqs.strategy.breakout import BreakoutStrategy

SYM = "600000.SH"


def oracle_breakout(closes, highs, lows, window: int) -> dict[int, str]:
    """独立 oracle：{日索引: 信号原因}。"""
    events: dict[int, str] = {}
    for t in range(len(closes)):
        if t < window:
            continue
        prior_high = max(highs[t - window : t])  # 不含 T 日
        prior_low = min(lows[t - window : t])
        if closes[t] > prior_high:
            events[t] = "breakout_high"
        elif closes[t] < prior_low:
            events[t] = "breakdown_low"
    return events


def staircase_series(window: int = 20) -> tuple[list[float], list[float], list[float]]:
    """构造含突破/跌破/边界（等于前高）的序列。"""
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    for _ in range(window + 1):  # 0..20
        closes.append(10.0)
        highs.append(10.2)
        lows.append(9.8)
    tail = [
        (10.5, 10.6, 10.0),   # 21 突破前高 10.2 → ENTRY
        (10.4, 10.5, 10.2),   # 22 无
        (9.5, 10.4, 9.4),     # 23 跌破前低 9.8 → EXIT
        (9.6, 9.7, 9.5),      # 24 无
        (10.6, 10.7, 9.6),    # 25 等于前高 10.6 → 不触发（严格大于）
        (10.7, 10.8, 10.5),   # 26 等于前高 → 不触发
        (10.8, 10.9, 10.6),   # 27 等于前高 → 不触发
        (11.0, 11.1, 10.9),   # 28 高于前高 10.9 → ENTRY
    ]
    for c, h, low in tail:
        closes.append(c)
        highs.append(h)
        lows.append(low)
    return closes, highs, lows


def run_over_days(closes, highs, lows, params, *, universe=None):
    rows = price_series(SYM, closes, highs=highs, lows=lows)
    store = make_store(make_bars(rows))
    strategy = BreakoutStrategy(params)
    days = dates(len(closes))
    actual: dict[int, str] = {}
    for t, day in enumerate(days):
        ctx = make_context(store, day, universe=universe or [SYM])
        for sig in strategy.on_bar(ctx):
            actual[t] = sig.reason
    return actual, store, strategy, days


# --------------------------------------------------------------------------- #
# 与独立 oracle 全量比对
# --------------------------------------------------------------------------- #
def test_signals_match_oracle_every_day():
    closes, highs, lows = staircase_series(20)
    actual, _, _, _ = run_over_days(closes, highs, lows, {"window": 20})
    assert actual == oracle_breakout(closes, highs, lows, 20)


def test_expected_key_days():
    closes, highs, lows = staircase_series(20)
    actual, _, _, _ = run_over_days(closes, highs, lows, {"window": 20})
    assert actual.get(21) == "breakout_high"
    assert actual.get(23) == "breakdown_low"
    assert actual.get(28) == "breakout_high"
    # 等于前高的三日一律不触发（严格大于）
    assert 25 not in actual and 26 not in actual and 27 not in actual


def test_excludes_today_high_proving_no_lookahead():
    """当日盘中冲高、收盘仍高于**此前**最高价 → 必须触发；
    若错误地把当日最高价计入基准，则不会触发。"""
    window = 5
    closes = [10.0] * (window + 1) + [10.3]
    highs = [10.2] * (window + 1) + [10.9]  # 当日最高 10.9
    lows = [9.8] * (window + 2)
    actual, _, _, _ = run_over_days(closes, highs, lows, {"window": window})
    assert actual.get(window + 1) == "breakout_high"
    assert oracle_breakout(closes, highs, lows, window) == actual


def test_equal_to_prior_high_does_not_trigger():
    window = 5
    closes = [10.0] * (window + 1) + [10.2]
    highs = [10.2] * (window + 2)
    lows = [9.8] * (window + 2)
    actual, _, _, _ = run_over_days(closes, highs, lows, {"window": window})
    assert actual == {}
    assert oracle_breakout(closes, highs, lows, window) == {}


def test_breakdown_triggers_exit():
    window = 5
    closes = [10.0] * (window + 1) + [9.0]
    highs = [10.2] * (window + 2)
    lows = [9.8] * (window + 1) + [8.9]
    actual, _, _, _ = run_over_days(closes, highs, lows, {"window": window})
    assert actual.get(window + 1) == "breakdown_low"


def test_include_exits_false():
    window = 5
    closes = [10.0] * (window + 1) + [9.0]
    highs = [10.2] * (window + 2)
    lows = [9.8] * (window + 1) + [8.9]
    actual, _, _, _ = run_over_days(closes, highs, lows, {"window": window, "include_exits": False})
    assert actual == {}


def test_score_reflects_breakout_size():
    window = 5
    closes = [10.0] * (window + 1) + [11.0]
    highs = [10.2] * (window + 2)
    lows = [9.8] * (window + 2)
    rows = price_series(SYM, closes, highs=highs, lows=lows)
    store = make_store(make_bars(rows))
    strategy = BreakoutStrategy({"window": window})
    day = dates(len(closes))[-1]
    signals = strategy.on_bar(make_context(store, day, universe=[SYM]))
    assert len(signals) == 1
    assert signals[0].direction is SignalDirection.ENTRY
    assert signals[0].score == approx(11.0 / 10.2 - 1.0)
    assert signals[0].meta["prior_high"] == approx(10.2)


# --------------------------------------------------------------------------- #
# 参数与边界
# --------------------------------------------------------------------------- #
def test_exclude_today_false_is_rejected():
    with raises(ConfigError) as exc:
        BreakoutStrategy({"window": 20, "exclude_today": False})
    assert "未来函数" in str(exc.value)


def test_unknown_param_rejected():
    with raises(ConfigError):
        BreakoutStrategy({"windows": 20})


def test_invalid_window_rejected():
    with raises(ConfigError):
        BreakoutStrategy({"window": 1})


def test_history_insufficient_returns_empty():
    window = 20
    closes, highs, lows = staircase_series(window)
    rows = price_series(SYM, closes, highs=highs, lows=lows)
    store = make_store(make_bars(rows))
    strategy = BreakoutStrategy({"window": window})
    days = dates(len(closes))
    for t in range(window):
        assert strategy.on_bar(make_context(store, days[t], universe=[SYM])) == []


def test_confirm_volume_requires_expansion():
    window = 5
    closes = [10.0] * (window + 1) + [10.5, 10.6]
    highs = [10.2] * (window + 2) + [10.7]
    lows = [9.8] * (window + 3)
    volumes = [1_000_000.0] * (window + 1) + [1_000_000.0, 3_000_000.0]

    params = {"window": window, "confirm_volume": True, "volume_window": 5, "volume_ratio": 1.5}
    rows = price_series(SYM, closes, highs=highs, lows=lows, volumes=volumes)
    store = make_store(make_bars(rows))
    strategy = BreakoutStrategy(params)
    days = dates(len(closes))

    # 突破日未放量 → 无信号
    assert strategy.on_bar(make_context(store, days[window + 1], universe=[SYM])) == []
    # 放量突破日 → 有信号
    signals = strategy.on_bar(make_context(store, days[window + 2], universe=[SYM]))
    assert len(signals) == 1


def test_describe_min_history_includes_volume_window():
    assert BreakoutStrategy({"window": 20}).min_history == 21
    assert BreakoutStrategy({"window": 20, "confirm_volume": True, "volume_window": 10}).min_history == 21
