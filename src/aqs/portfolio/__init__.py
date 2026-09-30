"""M4 组合层：仓位计算、目标权重组合、配置驱动注册表。"""

from __future__ import annotations

from .base import BasePortfolio, OrderPlan, Portfolio, TargetWeight
from .registry import (
    PORTFOLIO_REGISTRY,
    available_portfolios,
    build_portfolio,
    get_portfolio_class,
    load_portfolio,
    register_portfolio,
    sizing_from_params,
)
from .sizing import (
    KellyResult,
    KellySizing,
    KellyStats,
    SizingConfig,
    cap_weights,
    equal_weights,
    kelly_binary,
    kelly_continuous,
    kelly_from_trades,
    kelly_stats_from_trades,
    score_weights,
)
from .target_weight import TargetWeightPortfolio

__all__ = [
    "BasePortfolio",
    "OrderPlan",
    "Portfolio",
    "TargetWeight",
    "TargetWeightPortfolio",
    "PORTFOLIO_REGISTRY",
    "available_portfolios",
    "build_portfolio",
    "get_portfolio_class",
    "load_portfolio",
    "register_portfolio",
    "sizing_from_params",
    "KellyResult",
    "KellySizing",
    "KellyStats",
    "SizingConfig",
    "cap_weights",
    "equal_weights",
    "kelly_binary",
    "kelly_continuous",
    "kelly_from_trades",
    "kelly_stats_from_trades",
    "score_weights",
]
