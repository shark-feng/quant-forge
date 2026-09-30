"""RMS 组合级控制的执行层：敞口缩放（减仓/清仓）的跨日补偿。

背景（缺陷修复 #4）：T+1 冻结、停牌、跌停都会让卖单当天无法成交。
若减仓只在当日按「可卖数量」计算且不记录缺口，实际敞口会长期高于
:meth:`~aqs.risk.base.RiskEngine.target_exposure_scale`，且无人知道。

本模块的做法：

1. 每次都按**目标敞口 vs 当前敞口**重新计算应减数量（天然具备跨日补偿）；
2. 当日因不可卖而未完成的部分记入 ``pending``，次日（解锁后）优先补减；
3. 未达标的交易日连续天数（``unmet_days``）超过阈值时产生预警；
4. 全过程写入 ``history`` 与诊断，供报告层披露偏差。

**不修改账户与撮合**：只产出 :class:`~aqs.portfolio.base.OrderPlan`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Mapping, Sequence

from ..core.enums import Side
from ..core.models import MarketSnapshot
from ..core.quantity import sell_quantity
from ..portfolio.base import OrderPlan

__all__ = ["ExposureControlState", "ReductionPlan", "plan_exposure_reduction"]

_EPS = 1e-9


@dataclass(slots=True)
class ExposureControlState:
    """跨日维持的敞口控制状态。"""

    target_scale: float = 1.0
    pending: dict[str, float] = field(default_factory=dict)
    """因 T+1 冻结/停牌/跌停未能减仓的**股数**（次日优先补减）。"""
    deferred_days: dict[str, int] = field(default_factory=dict)
    """:attr:`pending` 中每个标的已顺延的交易日数。"""
    deferred_events: int = 0
    """累计「因不可卖而顺延」的事件数。"""
    unmet_days: int = 0
    """连续未达标（实际敞口 > 目标 + 容差）的交易日数。"""
    max_unmet_days: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_scale": self.target_scale,
            "pending_symbols": len(self.pending),
            "pending_quantity": float(sum(self.pending.values())),
            "deferred_events": self.deferred_events,
            "unmet_days": self.unmet_days,
            "max_unmet_days": self.max_unmet_days,
            "deferred_days": dict(self.deferred_days),
        }


@dataclass(frozen=True, slots=True)
class ReductionPlan:
    """一次减仓计划的结果。"""

    orders: tuple[OrderPlan, ...] = ()
    unmet: Mapping[str, float] = field(default_factory=dict)
    reasons: Mapping[str, str] = field(default_factory=dict)
    exposure_before: float = 0.0
    reduction_fraction: float = 0.0

    @property
    def total_planned(self) -> float:
        return float(sum(o.quantity for o in self.orders))


def _whole_shares(quantity: float, lot: int) -> float:
    """把减仓数量归一化为**可执行的整数股**（缺陷 #13）。

    实现来自 :func:`aqs.core.quantity.sell_quantity`（全项目唯一实现）：
    卖出量必须是整数股，且**不得出现小数股**。
    """
    return sell_quantity(quantity, lot)


def plan_exposure_reduction(
    account: Any,
    snapshot: MarketSnapshot,
    *,
    scale: float,
    lot_size: int = 100,
    pending: Mapping[str, float] | None = None,
    tolerance: float = 0.01,
) -> ReductionPlan:
    """按目标敞口生成减仓订单。

    Args:
        account: 账户（只读：``positions`` / ``sellable`` / ``total_value`` / ``positions_value``）。
        snapshot: 当日快照（用于判断停牌；停牌标的自然 ``sellable == 0``）。
        scale: 目标敞口比例（1.0 表示不减仓；0.0 表示清仓）。
        lot_size: 每手股数。
        pending: 上一交易日未完成的减仓数量（按标的）。仅用于**优先处理顺序**与欠账记录；
            实际下单量按当日敞口重新计算（避免与每日重算重复计数而过度卖出）。
        tolerance: 敞口容差；``exposure <= scale + tolerance`` 视为已达标。

    Returns:
        :class:`ReductionPlan`：订单、未完成数量与原因、减仓前的实际敞口。
    """
    pending = dict(pending or {})
    total = float(getattr(account, "total_value", 0.0))
    positions_value = float(getattr(account, "positions_value", 0.0))
    exposure = positions_value / total if total > _EPS else 0.0

    if scale >= 1.0 - _EPS or exposure <= _EPS:
        return ReductionPlan(orders=(), unmet={}, reasons={}, exposure_before=exposure, reduction_fraction=0.0)
    if exposure <= scale + tolerance:
        # 已达标：仅补做上一日遗留的 pending
        reduction_fraction = 0.0
    else:
        reduction_fraction = 1.0 - (scale / exposure)

    orders: list[OrderPlan] = []
    unmet: dict[str, float] = {}
    reasons: dict[str, str] = {}
    positions = getattr(account, "positions", {})

    # 有 pending 的标的优先处理（跨日补偿的「优先级」作用）；
    # 注意：数量按**当前敞口重新计算**，不与 pending 相加 ——
    # 否则「每日重算」与「累加缺口」会重复计数、过度卖出。
    symbols = sorted(positions, key=lambda s: (0 if pending.get(s) else 1, s))

    for symbol in symbols:
        position = positions[symbol]
        held = float(position.total_quantity)
        if held <= _EPS:
            continue
        sellable = float(account.sellable(symbol))
        desired = held * reduction_fraction
        if scale <= _EPS:
            desired = held  # 清仓：目标为 0，全部卖出
        if desired <= _EPS:
            continue
        quantity = min(desired, sellable)
        # 缺陷 #13：卖出量必须是整数股（不足一手时按零股规则取整数）。
        # 当可卖量本身小于目标量时，直接卖光可卖的**整数股**，不做小数透传。
        if quantity < sellable:
            quantity = sell_quantity(quantity, lot_size)
        else:
            quantity = sell_quantity(sellable, lot_size)
        if quantity > 0:
            orders.append(
                OrderPlan(
                    symbol,
                    Side.SELL,
                    quantity,
                    tag="risk_close" if scale <= _EPS else "risk_reduce",
                    reason=f"target_exposure_scale={scale:.2f}",
                )
            )
        shortfall = desired - quantity
        if shortfall > _EPS:
            unmet[symbol] = shortfall
            bar = snapshot.bar(symbol)
            if bar is not None and (bar.is_suspended or bar.volume <= 0):
                reasons[symbol] = "suspended"
            elif sellable <= _EPS:
                reasons[symbol] = "t1_locked"
            else:
                reasons[symbol] = "partial_sellable"

    return ReductionPlan(
        orders=tuple(orders),
        unmet=unmet,
        reasons=reasons,
        exposure_before=exposure,
        reduction_fraction=reduction_fraction,
    )
