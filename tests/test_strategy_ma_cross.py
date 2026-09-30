"""均线交叉策略测试。

核心方法：用**独立实现的纯 Python 循环**（不依赖被测代码）作为 oracle，
逐日比对策略在每一个交易日产生的信号 —— 既验证金叉/死叉的判定，
也验证「历史不足不出信号」「池外标的无信号」等边界。
"""

from __future__ import annotations

from tests.compat import approx, raises
from tests.tools import dates, make_bars, make_context, make_store, price_series

from aqs.core.enums import SignalDirection
from aqs.core.exceptions import ConfigError
from aqs.strategy.ma_cross import MACrossStrategy

SYM_A = "600000.SH"
SYM_B = "000001.SZ"


def v_series(n_down: int = 20, n_up: int = 16) -> list[float]:
    """先跌后涨的价格序列（必然出现一次金叉）。"""
    down = [20.0 - 0.5 * i for i in range(n_down)]
    up = [down[-1] + 0.8 * (i + 1) for i in range(n_up)]
    return [round(x, 2) for x in down + up]


def v_then_fall(n_down: int = 20, n_up: int = 16, n_fall: int = 18) -> list[float]:
    """先跌、再涨、最后回落（同时包含金叉与死叉）。"""
    base = v_series(n_down, n_up)
    tail = [base[-1] - 0.8 * (i + 1) for i in range(n_fall)]
    return [round(x, 2) for x in base + tail]


def oracle_ma(closes: list[float], fast: int, slow: int) -> dict[int, str]:
    """独立 oracle：返回 {日索引: 信号原因}。"""
    events: dict[int, str] = {}
    for t in range(len(closes)):
        if t < slow:
            continue
        f_now = sum(closes[t - fast + 1 : t + 1]) / fast
        s_now = sum(closes[t - slow + 1 : t + 1]) / slow
        f_prev = sum(closes[t - fast : t]) / fast
        s_prev = sum(closes[t - slow : t]) / slow
        if f_now > s_now and f_prev <= s_prev:
            events[t] = "golden_cross"
        elif f_now < s_now and f_prev >= s_prev:
            events[t] = "dead_cross"
    return events


def run_strategy_over_days(closes: list[float], params: dict, *, symbols=(SYM_A,), universe=None):
    """逐日运行策略，返回 {日索引: 信号原因}。"""
    rows = []
    for sym in symbols:
        rows.extend(price_series(sym, closes))
    store = make_store(make_bars(rows))
    strategy = MACrossStrategy(params)
    days = dates(len(closes))
    actual: dict[int, str] = {}
    for t, day in enumerate(days):
        ctx = make_context(store, day, universe=list(universe) if universe is not None else list(symbols))
        for sig in strategy.on_bar(ctx):
            actual[t] = sig.reason
    return actual, store, strategy, days


# --------------------------------------------------------------------------- #
# 与独立 oracle 全量比对
# --------------------------------------------------------------------------- #
def test_signals_match_oracle_every_day():
    closes = v_series()
    actual, _, _, _ = run_strategy_over_days(
        closes, {"fast_window": 5, "slow_window": 20, "price_field": "close_adj"}
    )
    assert actual == oracle_ma(closes, 5, 20)
    assert "golden_cross" in actual.values()  # 该序列确实存在金叉


def test_signals_match_oracle_with_custom_windows():
    closes = v_series(n_down=25, n_up=20)
    actual, _, _, _ = run_strategy_over_days(
        closes, {"fast_window": 3, "slow_window": 10, "price_field": "close_adj"}
    )
    assert actual == oracle_ma(closes, 3, 10)


def test_signals_match_oracle_with_rising_then_falling():
    closes = [10.0 + 0.3 * i for i in range(25)] + [17.5 - 0.4 * i for i in range(1, 20)]
    closes = [round(x, 2) for x in closes]
    actual, _, _, _ = run_strategy_over_days(closes, {"fast_window": 5, "slow_window": 20})
    assert actual == oracle_ma(closes, 5, 20)
    assert "dead_cross" in actual.values()


# --------------------------------------------------------------------------- #
# 逐条验收
# --------------------------------------------------------------------------- #
def test_entry_only_on_golden_cross_day():
    closes = v_series()
    events = oracle_ma(closes, 5, 20)
    golden_days = [t for t, r in events.items() if r == "golden_cross"]
    assert golden_days, "构造的序列应至少出现一次金叉"
    first = golden_days[0]

    rows = price_series(SYM_A, closes)
    store = make_store(make_bars(rows))
    strategy = MACrossStrategy({"fast_window": 5, "slow_window": 20})
    days = dates(len(closes))

    before = strategy.on_bar(make_context(store, days[first - 1], universe=[SYM_A]))
    assert [s for s in before if s.direction is SignalDirection.ENTRY] == []

    on_day = strategy.on_bar(make_context(store, days[first], universe=[SYM_A]))
    entries = [s for s in on_day if s.direction is SignalDirection.ENTRY]
    assert len(entries) == 1
    assert entries[0].symbol == SYM_A
    assert entries[0].signal_date == days[first]
    assert entries[0].reason == "golden_cross"

    after = strategy.on_bar(make_context(store, days[first + 1], universe=[SYM_A]))
    assert [s for s in after if s.direction is SignalDirection.ENTRY] == []


def test_history_insufficient_returns_empty():
    closes = v_series()
    rows = price_series(SYM_A, closes)
    store = make_store(make_bars(rows))
    strategy = MACrossStrategy({"fast_window": 5, "slow_window": 20})
    days = dates(len(closes))
    for t in range(20):  # 需要 21 根 K 线才有 MA20 与前一日的 MA20
        assert strategy.on_bar(make_context(store, days[t], universe=[SYM_A])) == []


def test_unknown_param_rejected():
    with raises(ConfigError) as exc:
        MACrossStrategy({"fast_windows": 5, "slow_window": 20})
    assert "fast_windows" in str(exc.value)


def test_invalid_windows_rejected():
    with raises(ConfigError):
        MACrossStrategy({"fast_window": 20, "slow_window": 5})
    with raises(ConfigError):
        MACrossStrategy({"fast_window": 20, "slow_window": 20})
    with raises(ConfigError):
        MACrossStrategy({"fast_window": 1, "slow_window": 20})
    with raises(ConfigError):
        MACrossStrategy({"fast_window": 5.5, "slow_window": 20})


def test_invalid_price_field_rejected():
    with raises(ConfigError):
        MACrossStrategy({"price_field": "pe_ttm"})


def test_include_exits_false():
    closes = v_then_fall()
    full, _, _, _ = run_strategy_over_days(closes, {"fast_window": 5, "slow_window": 20})
    assert "golden_cross" in full.values() and "dead_cross" in full.values()

    actual, _, _, _ = run_strategy_over_days(
        closes, {"fast_window": 5, "slow_window": 20, "include_exits": False}
    )
    assert "dead_cross" not in actual.values()
    assert "golden_cross" in actual.values()
    # 只有卖出信号被过滤，买入信号与完整口径完全一致
    assert {t for t, r in actual.items()} == {t for t, r in full.items() if r == "golden_cross"}


def test_only_universe_symbols_get_signals():
    closes = v_series()
    rows = price_series(SYM_A, closes) + price_series(SYM_B, closes)
    store = make_store(make_bars(rows))
    strategy = MACrossStrategy({"fast_window": 5, "slow_window": 20})
    day = dates(len(closes))[-1]

    # 两个标的形态一致，但只有 A 在股票池内
    signals = strategy.on_bar(make_context(store, day, universe=[SYM_A]))
    assert {s.symbol for s in signals} <= {SYM_A}

    # 两个都在池内 → B 也会产生信号（若有交叉）
    all_signals = strategy.on_bar(make_context(store, day, universe=[SYM_A, SYM_B]))
    assert {s.symbol for s in all_signals} <= {SYM_A, SYM_B}


def test_empty_universe_returns_empty():
    closes = v_series()
    store = make_store(make_bars(price_series(SYM_A, closes)))
    strategy = MACrossStrategy({})
    assert strategy.on_bar(make_context(store, dates(len(closes))[-1], universe=[])) == []


def test_score_is_ma_spread():
    closes = v_series()
    rows = price_series(SYM_A, closes)
    store = make_store(make_bars(rows))
    strategy = MACrossStrategy({"fast_window": 5, "slow_window": 20})
    days = dates(len(closes))

    for t in range(20, len(closes)):
        for sig in strategy.on_bar(make_context(store, days[t], universe=[SYM_A])):
            fast = sum(closes[t - 4 : t + 1]) / 5.0
            slow = sum(closes[t - 19 : t + 1]) / 20.0
            assert sig.score == approx((fast - slow) / slow)
            assert sig.meta["ma_fast"] == approx(fast)
            assert sig.meta["ma_slow"] == approx(slow)


def test_delisted_symbol_produces_no_signal():
    """停牌/退市导致无行情的标的不会出现在面板中，因而不会有信号。"""
    closes = v_series()
    rows = price_series(SYM_A, closes) + price_series(SYM_B, closes[:25])
    store = make_store(make_bars(rows))
    strategy = MACrossStrategy({"fast_window": 5, "slow_window": 20})
    day = dates(len(closes))[-1]
    signals = strategy.on_bar(make_context(store, day, universe=[SYM_A, SYM_B]))
    assert all(s.symbol == SYM_A for s in signals)


def test_describe_reports_params():
    info = MACrossStrategy({"fast_window": 10, "slow_window": 30}).describe()
    assert info["name"] == "ma_cross"
    assert info["params"]["fast_window"] == 10
    assert info["min_history"] == 31
