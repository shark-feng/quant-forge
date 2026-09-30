"""策略注册表与配置驱动构建。

配置文件（``configs/strategies/*.yaml``）的语义：

```yaml
strategy:
  name: ma_cross                     # → 注册表键
  class: aqs.strategy.ma_cross.MACrossStrategy   # 仅文档用途（以注册表为准）
  params: {fast_window: 5, slow_window: 20}      # → 策略构造参数（未知键报错）
  filters: {exclude_st: true, ...}               # → 数据层股票池口径（filters_to_data_overrides）
  execution: {price: open, max_defer_days: 5}    # → 引擎撮合口径
  portfolio: {max_positions: 10, ...}            # → 组合层口径（M4）
  signal: {buy: "...", sell: "..."}              # 人类可读的信号表达式（不参与计算）
```
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..config.loader import load_strategy_config
from ..core.exceptions import ConfigError
from .base import BaseStrategy
from .breakout import BreakoutStrategy
from .ma_cross import MACrossStrategy
from .volume import VolumeStrategy

__all__ = [
    "STRATEGY_REGISTRY",
    "StrategySpec",
    "register_strategy",
    "available_strategies",
    "get_strategy_class",
    "build_strategy",
    "load_spec",
    "load_strategy",
    "filters_to_data_overrides",
]

#: 策略名 → 策略类
STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    MACrossStrategy.name: MACrossStrategy,
    BreakoutStrategy.name: BreakoutStrategy,
    VolumeStrategy.name: VolumeStrategy,
}

_KNOWN_SECTIONS = ("name", "class", "params", "signal", "filters", "execution", "portfolio")


def register_strategy(cls: type[BaseStrategy], *, name: str | None = None) -> type[BaseStrategy]:
    """注册策略类（也支持自定义策略扩展）。"""
    key = name or cls.name
    if not key:
        raise ConfigError("策略必须有 name")
    STRATEGY_REGISTRY[key] = cls
    return cls


def available_strategies() -> list[str]:
    return sorted(STRATEGY_REGISTRY)


def get_strategy_class(name: str) -> type[BaseStrategy]:
    try:
        return STRATEGY_REGISTRY[name]
    except KeyError as exc:
        raise ConfigError(f"未知策略：{name}；可用策略：{available_strategies()}") from exc


# --------------------------------------------------------------------------- #
# 配置解析
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class StrategySpec:
    """一个策略配置文件的完整语义（供引擎/组合层/报告层消费）。"""

    name: str
    params: dict[str, Any] = field(default_factory=dict)
    signal: dict[str, Any] = field(default_factory=dict)
    filters: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)
    portfolio: dict[str, Any] = field(default_factory=dict)
    class_path: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "StrategySpec":
        unknown = sorted(set(data) - set(_KNOWN_SECTIONS))
        if unknown:
            raise ConfigError(f"策略配置存在未知段 {unknown}；可用段：{list(_KNOWN_SECTIONS)}")
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError("策略配置缺少 name", path="strategy.name")
        out: dict[str, Any] = {"name": name, "class_path": data.get("class")}
        for section in ("params", "signal", "filters", "execution", "portfolio"):
            value = data.get(section, {})
            if value is None:
                value = {}
            if not isinstance(value, Mapping):
                raise ConfigError(f"策略配置段 {section} 必须是映射", path=f"strategy.{section}")
            out[section] = dict(value)
        return cls(**out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "params": dict(self.params),
            "signal": dict(self.signal),
            "filters": dict(self.filters),
            "execution": dict(self.execution),
            "portfolio": dict(self.portfolio),
        }


def load_spec(path: str | Path) -> StrategySpec:
    """读取策略配置文件并解析为 :class:`StrategySpec`。"""
    return StrategySpec.from_mapping(load_strategy_config(path))


def build_strategy(spec: StrategySpec | Mapping[str, Any]) -> BaseStrategy:
    """按配置构建策略实例。"""
    if not isinstance(spec, StrategySpec):
        spec = StrategySpec.from_mapping(spec)
    cls = get_strategy_class(spec.name)
    return cls(spec.params)


def load_strategy(path: str | Path) -> BaseStrategy:
    """从 YAML 文件加载并构建策略。"""
    return build_strategy(load_spec(path))


def filters_to_data_overrides(filters: Mapping[str, Any]) -> dict[str, Any]:
    """把策略 ``filters:`` 段映射为 :class:`~aqs.config.schema.DataConfig` 覆盖。

    支持的键：``exclude_st`` / ``exclude_suspended`` / ``min_list_days`` /
    ``liquidity_window`` / ``min_amount``。
    """
    mapping = {
        "exclude_st": "exclude_st",
        "exclude_suspended": "exclude_suspended",
        "min_list_days": "min_list_days",
    }
    overrides: dict[str, Any] = {}
    liquidity: dict[str, Any] = {}
    unknown: list[str] = []
    for key, value in filters.items():
        if key in mapping:
            overrides[mapping[key]] = value
        elif key == "liquidity_window":
            liquidity["window"] = value
        elif key == "min_amount":
            liquidity["min_amount"] = value
        else:
            unknown.append(key)
    if unknown:
        raise ConfigError(
            f"策略 filters 段存在未知键 {sorted(unknown)}；可用键："
            f"{sorted(list(mapping) + ['liquidity_window', 'min_amount'])}",
            path="strategy.filters",
        )
    if liquidity:
        overrides["liquidity"] = liquidity
    return overrides
