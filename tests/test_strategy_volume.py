"""成交量配合策略测试（放量定义与「基准不含当日」口径）。"""

from __future__ import annotations

from tests.compat import approx, raises
from tests.tools import dates, make_bars, make_context, make_store, price_series

from aqs.core.enums import SignalDirection
from aqs.core.exceptions import ConfigError
from aqs.strategy.volume import VolumeStrategy

SYM = "600000.SH"


def oracle_volume(closes, volumes, window: int, ratio: float, *, exclude_today: bool = True):
    """独立 oracle：{日索引: 信号原因}。"""
    events: dict[int, str] = {}
    for t in range(len(closes)):
        if t < 1:
            continue
        base_slice = volumes[t - window : t] if exclude_today else volumes[t - window + 1 : t + 1]
        if len(base_slice) < window:
            continue
        base = sum(base_slice) / window
        if base <= 0 or volumes[t] <= base * ratio:
            continue
        if closes[t] > closes[t - 1]:
            events[t] = "volume_up"
        elif closes[t] < closes[t - 1]:
            events[t] = "volume_down"
    return events


def scenario():
    """21 日平盘 → 放量上涨(2.0 倍) → 放量下跌(1.67 倍) → 缩量上涨 → 温和放量上涨。"""
    window = 5
    closes = [10.0] * 21
    volumes = [1_000_000.0] * 21
    closes += [10.2, 10.1, 10.3, 10.4]
    volumes += [2_000_000.0, 2_000_000.0, 1_000_000.0, 1_500_000.0]
    return window, closes, volumes


def run_over_days(closes, volumes, params, *, universe=None):
    rows = price_series(SYM, closes, volumes=volumes)
    store = make_store(make_bars(rows))
    strategy = VolumeStrategy(params)
    days = dates(len(closes))
    actual: dict[int, str] = {}
    for t, day in enumerate(days):
        ctx = make_context(store, day, universe=universe or [SYM])
        for sig in strategy.on_bar(ctx):
            actual[t] = sig.reason
    return actual, store, strategy, days


# --------------------------------------------------------------------------- #
# 信号判定
# --------------------------------------------------------------------------- #
def test_signals_match_oracle_every_day():
    window, closes, volumes = scenario()
    actual, _, _, _ = run_over_days(
        closes, volumes, {"volume_window": window, "volume_ratio": 1.5}
    )
    assert actual == oracle_volume(closes, volumes, window, 1.5)


def test_volume_up_triggers_entry():
    window, closes, volumes = scenario()
    actual, _, _, _ = run_over_days(closes, volumes, {"volume_window": window})
    assert actual.get(21) == "volume_up"


def test_volume_down_triggers_exit():
    window, closes, volumes = scenario()
    actual, _, _, _ = run_over_days(closes, volumes, {"volume_window": window})
    assert actual.get(22) == "volume_down"


def test_low_volume_up_does_not_trigger():
    window, closes, volumes = scenario()
    actual, _, _, _ = run_over_days(closes, volumes, {"volume_window": window})
    assert 23 not in actual  # 缩量上涨


def test_ratio_boundary_is_strict():
    """成交量恰好等于阈值倍数 → 不触发（要求严格大于）。"""
    window = 5
    closes = [10.0] * 21 + [10.2]
    volumes = [1_000_000.0] * 21 + [1_500_000.0]  # 基准 = 100 万 → 恰为 1.5 倍
    actual, _, _, _ = run_over_days(closes, volumes, {"volume_window": window, "volume_ratio": 1.5})
    assert actual == {}
    # 略微超过阈值即触发
    volumes2 = volumes[:-1] + [1_500_001.0]
    actual2, _, _, _ = run_over_days(closes, volumes2, {"volume_window": window, "volume_ratio": 1.5})
    assert actual2.get(21) == "volume_up"


def test_volume_ma_excludes_current_day():
    """当日巨量不应抬高自身基准；含当日口径会显著降低量比。"""
    window = 5
    closes = [10.0] * 21 + [10.2]
    volumes = [1_000_000.0] * 21 + [1_600_000.0]
    params = {"volume_window": window, "volume_ratio": 1.5}
    rows = price_series(SYM, closes, volumes=volumes)
    store = make_store(make_bars(rows))
    day = dates(len(closes))[-1]

    excl = VolumeStrategy({**params, "volume_ma_exclude_today": True})
    incl = VolumeStrategy({**params, "volume_ma_exclude_today": False})
    sig_excl = excl.on_bar(make_context(store, day, universe=[SYM]))
    sig_incl = incl.on_bar(make_context(store, day, universe=[SYM]))

    assert len(sig_excl) == 1
    assert sig_excl[0].meta["volume_ma"] == approx(1_000_000.0)
    # 含当日基准 = (4×100万 + 160万)/5 = 112 万 → 量比 1.43 < 1.5 → 不出信号
    assert len(sig_incl) == 0


def test_score_is_volume_ratio():
    window = 5
    closes = [10.0] * 21 + [10.2]
    volumes = [1_000_000.0] * 21 + [2_000_000.0]
    rows = price_series(SYM, closes, volumes=volumes)
    store = make_store(make_bars(rows))
    strategy = VolumeStrategy({"volume_window": window, "volume_ratio": 1.5})
    signals = strategy.on_bar(make_context(store, dates(len(closes))[-1], universe=[SYM]))
    assert len(signals) == 1
    assert signals[0].direction is SignalDirection.ENTRY
    assert signals[0].score == approx(2.0)
    assert signals[0].meta["volume_ratio"] == approx(2.0)


def test_include_exits_false():
    window, closes, volumes = scenario()
    actual, _, _, _ = run_over_days(
        closes, volumes, {"volume_window": window, "include_exits": False}
    )
    assert "volume_down" not in actual.values()
    assert "volume_up" in actual.values()


# --------------------------------------------------------------------------- #
# 参数与边界
# --------------------------------------------------------------------------- #
def test_history_insufficient_returns_empty():
    window = 5
    closes = [10.0, 10.1, 10.2, 10.3, 10.4]
    volumes = [1_000_000.0] * 5
    rows = price_series(SYM, closes, volumes=volumes)
    store = make_store(make_bars(rows))
    strategy = VolumeStrategy({"volume_window": window})
    days = dates(len(closes))
    for t in range(len(closes)):  # min_history = 5 + 1 + 1 = 7
        assert strategy.on_bar(make_context(store, days[t], universe=[SYM])) == []


def test_unknown_param_rejected():
    with raises(ConfigError):
        VolumeStrategy({"volume_windows": 20})


def test_invalid_ratio_rejected():
    with raises(ConfigError):
        VolumeStrategy({"volume_ratio": -1.0})
    with raises(ConfigError):
        VolumeStrategy({"volume_ratio": "1.5"})


def test_min_history_depends_on_ma_convention():
    assert VolumeStrategy({"volume_window": 20}).min_history == 22
    assert VolumeStrategy({"volume_window": 20, "volume_ma_exclude_today": False}).min_history == 21


def test_flat_price_with_volume_produces_no_signal():
    window = 5
    closes = [10.0] * 25
    volumes = [1_000_000.0] * 21 + [5_000_000.0] * 4
    actual, _, _, _ = run_over_days(closes, volumes, {"volume_window": window})
    assert actual == {}  # 放量但价格不动 → 不出信号
