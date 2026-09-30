"""风控规则注册表：规则名 → 规则类，配置驱动构建。

扩展新规则只需两步：
1. 实现 :class:`~aqs.risk.base.RiskRule` 子类；
2. ``register_rule(MyRule)``，然后在 ``configs/risk.yaml`` 的 ``rules:`` 中写规则名。

未注册的规则名会直接报错（不允许静默忽略风控规则）。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..config.schema import RiskRuleConfig
from ..core.exceptions import ConfigError
from .base import RiskRule
from .rules import (
    CancelRatioRule,
    CashSufficiencyRule,
    DailyLossLimitRule,
    GrossExposureRule,
    IndustryExposureRule,
    MaxDrawdownRule,
    MaxOrderNotionalRule,
    MaxOrderQuantityRule,
    MaxPositionPerSymbolRule,
    MaxTradeVolumeDailyRule,
    OrderFrequencyRule,
    PortfolioVaRLimitRule,
    PriceDeviationRule,
)

__all__ = [
    "RULE_REGISTRY",
    "register_rule",
    "available_rules",
    "get_rule_class",
    "build_rule",
    "build_rules",
]

RULE_REGISTRY: dict[str, type[RiskRule]] = {
    MaxOrderQuantityRule.name: MaxOrderQuantityRule,
    MaxOrderNotionalRule.name: MaxOrderNotionalRule,
    PriceDeviationRule.name: PriceDeviationRule,
    OrderFrequencyRule.name: OrderFrequencyRule,
    CancelRatioRule.name: CancelRatioRule,
    MaxTradeVolumeDailyRule.name: MaxTradeVolumeDailyRule,
    MaxPositionPerSymbolRule.name: MaxPositionPerSymbolRule,
    IndustryExposureRule.name: IndustryExposureRule,
    GrossExposureRule.name: GrossExposureRule,
    CashSufficiencyRule.name: CashSufficiencyRule,
    DailyLossLimitRule.name: DailyLossLimitRule,
    MaxDrawdownRule.name: MaxDrawdownRule,
    PortfolioVaRLimitRule.name: PortfolioVaRLimitRule,
}


def register_rule(cls: type[RiskRule], *, name: str | None = None) -> type[RiskRule]:
    key = name or cls.name
    if not key:
        raise ConfigError("风控规则必须有 name")
    RULE_REGISTRY[key] = cls
    return cls


def available_rules() -> list[str]:
    return sorted(RULE_REGISTRY)


def get_rule_class(name: str) -> type[RiskRule]:
    try:
        return RULE_REGISTRY[name]
    except KeyError as exc:
        raise ConfigError(
            f"未知风控规则：{name}；可用规则：{available_rules()}", path="risk.rules"
        ) from exc


def build_rule(config: RiskRuleConfig | Mapping[str, Any]) -> RiskRule:
    """按配置构建单条规则。"""
    if not isinstance(config, RiskRuleConfig):
        config = RiskRuleConfig(**dict(config))
    cls = get_rule_class(config.name)
    rule = cls(config.params, action=config.action)
    rule.priority = config.priority
    return rule


def build_rules(configs: Sequence[RiskRuleConfig | Mapping[str, Any]]) -> list[RiskRule]:
    """按配置构建规则集（按 priority 升序）。"""
    rules = [build_rule(c) for c in configs]
    return sorted(rules, key=lambda r: r.priority)
