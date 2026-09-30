"""组合层接口（M4 的实现将在下一轮交付）。

职责：把策略信号转成**订单计划**（数量/资金/持仓数约束），但不做风控否决：
- 目标权重 → 与当前持仓比较 → 生成买/卖计划；
- 约束：等权、单票上限、最大持仓数、现金管理、最小 100 股、T+1 可卖限制。

**为什么返回 :class:`OrderPlan` 而不是直接造订单**：
订单的 ``signal_date / submit_date`` 由引擎统一赋值，T+1 时间语义只在一个地方强制，
策略与组合层无法绕过。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ..core.enums import OrderType, Side
from ..core.models import SignalIntent
from ..strategy.base import StrategyContext

__all__ = ["OrderPlan", "Portfolio", "BasePortfolio", "TargetWeight"]


@dataclass(frozen=True, slots=True)
class TargetWeight:
    """目标权重（组合优化的直接输出，便于与第二阶段优化器对接）。"""

    symbol: str
    weight: float
    score: float = 0.0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class OrderPlan:
    """订单计划：只有「做什么」，没有「什么时候成交」。"""

    symbol: str
    side: Side
    quantity: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    tag: str = ""
    reason: str = ""
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def signed_quantity(self) -> float:
        return self.quantity * self.side.sign


@runtime_checkable
class Portfolio(Protocol):
    """组合协议。"""

    def generate_orders(self, signals: Sequence[SignalIntent], ctx: StrategyContext) -> Sequence[OrderPlan]:
        """把信号转成订单计划。"""
        ...


class BasePortfolio(ABC):
    """组合基类。"""

    name: str = "base"

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        self.params: dict[str, Any] = dict(params or {})

    @abstractmethod
    def generate_orders(self, signals: Sequence[SignalIntent], ctx: StrategyContext) -> Sequence[OrderPlan]:
        raise NotImplementedError

    def warmup(self, ctx: StrategyContext) -> None:  # noqa: B027 - 可选钩子
        """可选：每日信号前调用。"""
