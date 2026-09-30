"""回归缺陷 #9：量比基准过小导致量比爆炸。

原缺陷：``volume_ratio`` 只用 ``base.replace(0, np.nan)`` 挡住严格 0。
长期停牌复牌、新股上市初期等场景下 20 日均量可能只有几百股，
量比被放大成几十倍 → 产生假信号。

修复后：
- 指标层 ``volume_ratio(..., min_base=)``：基准 < min_base → NaN（视为无信号）；
- 策略层 ``volume_min_base``（默认 10000 股）从配置读取，低于阈值直接跳过该标的。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from tests.compat import approx, raises
from tests.tools import dates, make_bars, make_context, make_store, price_series

from aqs.core.exceptions import ConfigError
from aqs.strategy.indicators import volume_ratio
from aqs.strategy.volume import VolumeStrategy

D = dates(30)
SYM = "600000.SH"


# --------------------------------------------------------------------------- #
# 指标层
# --------------------------------------------------------------------------- #
def test_volume_ratio_normal_case():
    volume = pd.DataFrame({"A": [1_000_000.0] * 21 + [2_000_000.0]})
    ratio = volume_ratio(volume, 20)
    assert ratio["A"].iloc[-1] == approx(2.0)


def test_volume_ratio_below_min_base_is_nan():
    volume = pd.DataFrame({"A": [100.0] * 21 + [10_000.0]})
    # 不设阈值时量比会被放大到 100 倍（这正是原缺陷的表现）
    assert volume_ratio(volume, 20)["A"].iloc[-1] == approx(100.0)
    # 设阈值后：基准 100 < 1000 → NaN（视为无信号）
    assert np.isnan(volume_ratio(volume, 20, min_base=1_000.0)["A"].iloc[-1])
    # 阈值放宽到基准之下 → 恢复计算
    assert volume_ratio(volume, 20, min_base=50.0)["A"].iloc[-1] == approx(100.0)


def test_volume_ratio_boundary_is_inclusive():
    volume = pd.DataFrame({"A": [10_000.0] * 21 + [20_000.0]})
    assert volume_ratio(volume, 20, min_base=10_000.0)["A"].iloc[-1] == approx(2.0)
    assert np.isnan(volume_ratio(volume, 20, min_base=10_001.0)["A"].iloc[-1])


def test_volume_ratio_negative_min_base_rejected():
    volume = pd.DataFrame({"A": [1.0] * 5})
    with raises(ValueError):
        volume_ratio(volume, 3, min_base=-1.0)


def test_volume_ratio_zero_base_still_nan():
    volume = pd.DataFrame({"A": [0.0] * 21 + [1000.0]})
    assert np.isnan(volume_ratio(volume, 20, min_base=0.0)["A"].iloc[-1])


# --------------------------------------------------------------------------- #
# 策略层
# --------------------------------------------------------------------------- #
def build_store(closes, volumes):
    rows = price_series(SYM, closes, volumes=volumes)
    return make_store(make_bars(rows))


def test_strategy_skips_symbol_with_tiny_base():
    """20 日均量只有 100 股 → 放量 100 倍也不出信号。"""
    closes = [10.0] * 25 + [10.2]
    volumes = [100.0] * 25 + [10_000.0]
    store = build_store(closes, volumes)
    strategy = VolumeStrategy({"volume_window": 5, "volume_ratio": 1.5})
    signals = strategy.on_bar(make_context(store, D[len(closes) - 1], universe=[SYM]))
    assert signals == []


def test_strategy_with_relaxed_threshold_does_signal():
    closes = [10.0] * 25 + [10.2]
    volumes = [100.0] * 25 + [10_000.0]
    store = build_store(closes, volumes)
    strategy = VolumeStrategy({"volume_window": 5, "volume_ratio": 1.5, "volume_min_base": 10.0})
    signals = strategy.on_bar(make_context(store, D[len(closes) - 1], universe=[SYM]))
    assert len(signals) == 1


def test_strategy_default_threshold_is_configured():
    strategy = VolumeStrategy({})
    assert strategy.volume_min_base == approx(10_000.0)


def test_config_yaml_carries_min_base():
    from aqs.strategy.registry import load_spec, build_strategy

    spec = load_spec("configs/strategies/volume.yaml")
    assert spec.params["volume_min_base"] == 10_000
    assert build_strategy(spec).volume_min_base == approx(10_000.0)


def test_negative_min_base_rejected_by_config():
    with raises(ConfigError):
        VolumeStrategy({"volume_min_base": -1.0})


def test_unknown_param_still_rejected():
    with raises(ConfigError):
        VolumeStrategy({"volume_min_bases": 1.0})


def test_normal_liquidity_still_signals():
    closes = [10.0] * 25 + [10.2]
    volumes = [1_000_000.0] * 25 + [2_000_000.0]
    store = build_store(closes, volumes)
    strategy = VolumeStrategy({"volume_window": 5, "volume_ratio": 1.5})
    signals = strategy.on_bar(make_context(store, D[len(closes) - 1], universe=[SYM]))
    assert len(signals) == 1
    assert signals[0].meta["volume_ratio"] == approx(2.0)


def test_suspension_then_resume_does_not_false_signal():
    """长期停牌（成交量 0）后复牌：均量基准被停牌期拉低，必须有阈值兜底。"""
    closes = [10.0] * 20 + [10.0] * 5 + [10.3]
    volumes = [1_000_000.0] * 20 + [0.0] * 5 + [2_000_000.0]
    store = build_store(closes, volumes)
    strategy = VolumeStrategy({"volume_window": 20, "volume_ratio": 1.5})
    # 基准 = 含 5 个 0 的 20 日均量 = 75 万 → 仍高于阈值，属于正常放量（信号有效）
    signals = strategy.on_bar(make_context(store, D[len(closes) - 1], universe=[SYM]))
    assert len(signals) == 1
    # 若基准被拉到阈值以下，则不出信号
    strict = VolumeStrategy({"volume_window": 20, "volume_ratio": 1.5, "volume_min_base": 800_000.0})
    assert strict.on_bar(make_context(store, D[len(closes) - 1], universe=[SYM])) == []
