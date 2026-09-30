"""M3 策略层：指标、三种内置策略、配置驱动注册表。"""

from __future__ import annotations

from .base import BaseStrategy, Strategy, StrategyContext
from .breakout import BreakoutStrategy
from .indicators import (
    cross_over,
    cross_under,
    rolling_max,
    rolling_min,
    sma,
    tail_max,
    tail_mean,
    tail_min,
    tail_window,
    volume_ratio,
)
from .ma_cross import MACrossStrategy
from .registry import (
    STRATEGY_REGISTRY,
    StrategySpec,
    available_strategies,
    build_strategy,
    filters_to_data_overrides,
    get_strategy_class,
    load_spec,
    load_strategy,
    register_strategy,
)
from .volume import VolumeStrategy

__all__ = [
    "BaseStrategy",
    "Strategy",
    "StrategyContext",
    "MACrossStrategy",
    "BreakoutStrategy",
    "VolumeStrategy",
    "STRATEGY_REGISTRY",
    "StrategySpec",
    "available_strategies",
    "build_strategy",
    "filters_to_data_overrides",
    "get_strategy_class",
    "load_spec",
    "load_strategy",
    "register_strategy",
    "cross_over",
    "cross_under",
    "rolling_max",
    "rolling_min",
    "sma",
    "tail_max",
    "tail_mean",
    "tail_min",
    "tail_window",
    "volume_ratio",
]
