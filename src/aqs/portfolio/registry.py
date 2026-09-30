"""组合层注册表与配置驱动构建。

支持两种配置来源（后者覆盖前者）：
1. ``configs/base.yaml`` 的 ``portfolio:`` 段（全局默认）；
2. 策略文件 ``configs/strategies/*.yaml`` 的 ``portfolio:`` 段（策略专属覆盖）。

未知键直接报错 —— 与策略层保持同一套严格校验口径。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..config.loader import load_strategy_config
from ..config.schema import PortfolioConfig, as_config
from ..core.exceptions import ConfigError
from .base import BasePortfolio
from .sizing import KellySizing, SizingConfig
from .target_weight import TargetWeightPortfolio

__all__ = [
    "PORTFOLIO_REGISTRY",
    "register_portfolio",
    "available_portfolios",
    "get_portfolio_class",
    "sizing_from_params",
    "build_portfolio",
    "load_portfolio",
]

PORTFOLIO_REGISTRY: dict[str, type[BasePortfolio]] = {
    TargetWeightPortfolio.name: TargetWeightPortfolio,
    "equal": TargetWeightPortfolio,      # 别名：等权（默认行为）
}

_ALLOWED_KEYS = (
    "name",
    "class",
    "weighting",
    "max_positions",
    "max_weight_per_symbol",
    "cash_buffer",
    "allow_reentry",
    "rebalance",
    "lot_size",
    "kelly",
)
_KELLY_KEYS = ("enabled", "mode", "fraction", "cap", "min_trades", "initial_exposure")


def register_portfolio(cls: type[BasePortfolio], *, name: str | None = None) -> type[BasePortfolio]:
    key = name or cls.name
    if not key:
        raise ConfigError("组合类必须有 name")
    PORTFOLIO_REGISTRY[key] = cls
    return cls


def available_portfolios() -> list[str]:
    return sorted(PORTFOLIO_REGISTRY)


def get_portfolio_class(name: str) -> type[BasePortfolio]:
    try:
        return PORTFOLIO_REGISTRY[name]
    except KeyError as exc:
        raise ConfigError(f"未知组合类：{name}；可用：{available_portfolios()}") from exc


def sizing_from_params(
    params: Mapping[str, Any] | None = None,
    *,
    defaults: PortfolioConfig | Mapping[str, Any] | None = None,
    lot_size: int | None = None,
) -> SizingConfig:
    """把配置段转换为 :class:`SizingConfig`。

    Args:
        params: 策略文件中的 ``portfolio:`` 段（可只写需要覆盖的键）。
        defaults: 全局默认（``base.yaml`` 的 ``portfolio:`` 段）。
        lot_size: 每手股数（来自引擎配置，优先级最高）。
    """
    data = dict(params or {})
    unknown = sorted(set(data) - set(_ALLOWED_KEYS))
    if unknown:
        raise ConfigError(
            f"组合配置存在未知键 {unknown}；可用键：{sorted(_ALLOWED_KEYS)}", path="portfolio"
        )

    base: PortfolioConfig = as_config(defaults, PortfolioConfig) if defaults is not None else PortfolioConfig()
    kelly_params = dict(data.get("kelly") or {})
    unknown_kelly = sorted(set(kelly_params) - set(_KELLY_KEYS))
    if unknown_kelly:
        raise ConfigError(
            f"kelly 配置存在未知键 {unknown_kelly}；可用键：{sorted(_KELLY_KEYS)}", path="portfolio.kelly"
        )
    kelly = KellySizing(
        enabled=bool(kelly_params.get("enabled", base.kelly.enabled)),
        mode=str(kelly_params.get("mode", base.kelly.mode)),
        fraction=float(kelly_params.get("fraction", base.kelly.fraction)),
        cap=float(kelly_params.get("cap", base.kelly.cap)),
        min_trades=int(kelly_params.get("min_trades", base.kelly.min_trades)),
        initial_exposure=float(kelly_params.get("initial_exposure", base.kelly.initial_exposure)),
    )

    return SizingConfig(
        weighting=str(data.get("weighting", base.weighting)),
        max_positions=int(data.get("max_positions", base.max_positions)),
        max_weight_per_symbol=float(data.get("max_weight_per_symbol", base.max_weight_per_symbol)),
        cash_buffer=float(data.get("cash_buffer", base.cash_buffer)),
        lot_size=int(lot_size if lot_size is not None else data.get("lot_size", 100)),
        allow_reentry=bool(data.get("allow_reentry", base.allow_reentry)),
        rebalance=bool(data.get("rebalance", base.rebalance)),
        kelly=kelly,
    )


def build_portfolio(
    params: Mapping[str, Any] | None = None,
    *,
    name: str = "target_weight",
    defaults: PortfolioConfig | Mapping[str, Any] | None = None,
    lot_size: int | None = None,
) -> BasePortfolio:
    """构建组合实例。"""
    key = name
    if params and "name" in params:
        key = str(params["name"])
    cls = get_portfolio_class(key)
    sizing = sizing_from_params(params, defaults=defaults, lot_size=lot_size)
    return cls(sizing)


def load_portfolio(
    path: str,
    *,
    defaults: PortfolioConfig | Mapping[str, Any] | None = None,
    lot_size: int | None = None,
) -> BasePortfolio:
    """从策略文件读取 ``portfolio:`` 段并构建组合。"""
    section = load_strategy_config(path).get("portfolio") or {}
    if not isinstance(section, Mapping):
        raise ConfigError(f"策略文件 {path} 的 portfolio 段必须是映射")
    return build_portfolio(section, defaults=defaults, lot_size=lot_size)
