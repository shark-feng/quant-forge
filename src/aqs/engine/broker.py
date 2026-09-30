"""经纪商/执行模拟器：订单生命周期 + 撮合驱动 + 顺延。

订单生命周期（合同 §六）：

    创建 → 风控检查 → 提交 → 部分成交 → 完全成交 / 撤单 / 拒绝

本模块的职责边界：
- **不选股、不定仓位**（属于 M3/M4）；
- **不做风控决策**（属于 M5，通过 ``risk_hook`` 注入）；
- 只负责「给定订单与市场快照 → 成交或顺延或拒绝」，并保证 A 股硬约束与 T+1 时间语义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Callable, Mapping, Sequence

from ..config.schema import EngineConfig, as_config
from ..core.dates import DateLike, to_date
from ..core.enums import (
    FillSource,
    OrderStatus,
    OrderType,
    RejectReason,
    RiskAction,
    Side,
    TimeInForce,
)
from ..core.exceptions import EngineError, FutureFunctionError
from ..core.logging import AuditStream, get_logger
from ..core.models import CostBreakdown, Fill, MarketSnapshot, Order, RiskDecision
from .account import Account
from .cost import CostModel
from .matching import MatchResult, MatchingEngine

__all__ = ["Broker", "BrokerStats"]

logger = get_logger("engine.broker")

RiskHook = Callable[[Order, "Account", MarketSnapshot], RiskDecision]


@dataclass(slots=True)
class BrokerStats:
    """经纪商统计（用于回测验收与报告）。"""

    created: int = 0
    submitted: int = 0
    filled: int = 0
    partially_filled: int = 0
    expired: int = 0
    rejected: int = 0
    cancelled: int = 0
    deferred_events: int = 0
    fill_count: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = {
            "created": self.created,
            "submitted": self.submitted,
            "filled": self.filled,
            "partially_filled": self.partially_filled,
            "expired": self.expired,
            "rejected": self.rejected,
            "cancelled": self.cancelled,
            "deferred_events": self.deferred_events,
            "fill_count": self.fill_count,
        }
        d.update({f"reason_{k}": v for k, v in self.reasons.items()})
        return d


class Broker:
    """订单管理与撮合驱动。"""

    def __init__(
        self,
        account: Account,
        *,
        engine_config: EngineConfig | Mapping[str, Any] | None = None,
        cost_model: CostModel | None = None,
        matching: MatchingEngine | None = None,
        seed: int = 20240101,
        order_prefix: str = "O",
    ) -> None:
        self.account = account
        self.config = as_config(engine_config, EngineConfig)
        self.cost_model = cost_model or CostModel()
        self.matching = matching or MatchingEngine(self.config, cost_model=self.cost_model, seed=seed)
        self.orders: dict[str, Order] = {}
        self.working: list[Order] = []
        self.closed: list[Order] = []
        self.fills: list[Fill] = []
        self.stats = BrokerStats()
        self._seq = 0
        self._fill_seq = 0
        self._prefix = order_prefix
        self.audit = AuditStream()

    # ------------------------------------------------------------------ #
    # 创建与提交
    # ------------------------------------------------------------------ #
    def create_order(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        *,
        signal_date: DateLike | None = None,
        decision_ts: Any = None,
        submit_date: DateLike | None = None,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        tag: str = "",
        parent_id: str | None = None,
        time_in_force: TimeInForce | None = None,
        max_defer_days: int | None = None,
    ) -> Order:
        """创建订单（**不**提交撮合）。

        Raises:
            FutureFunctionError: 当 ``t_plus_one=True`` 且 ``submit_date <= signal_date``。
        """
        self._seq += 1
        sig = to_date(signal_date) if signal_date is not None else None
        sub = to_date(submit_date) if submit_date is not None else None
        if self.config.t_plus_one and sig is not None and sub is not None and sub <= sig:
            raise FutureFunctionError(
                f"订单时间语义违反 T+1：signal_date={sig} 必须严格早于 submit_date={sub}"
                f"（symbol={symbol}, side={side.value}）"
            )

        qty = float(quantity)
        if side is Side.BUY:
            if qty <= 0:
                raise EngineError(f"买入数量必须为正：{qty}")
        if qty <= 0:
            raise EngineError(f"订单数量必须为正：{qty}")

        order = Order(
            order_id=f"{self._prefix}{self._seq:07d}",
            symbol=symbol,
            side=side,
            quantity=qty,
            order_type=order_type,
            limit_price=limit_price,
            signal_date=sig,
            decision_ts=decision_ts,
            submit_date=sub,
            time_in_force=time_in_force or TimeInForce.GTC,
            max_defer_days=(
                max_defer_days if max_defer_days is not None else self.config.max_defer_days
            ),
            tag=tag,
            parent_id=parent_id,
            created_seq=self._seq,
        )
        self.orders[order.order_id] = order
        self.stats.created += 1
        return order

    def submit(self, order: Order) -> Order:
        """把订单放入撮合队列。"""
        if order.status.is_terminal:
            raise EngineError(f"订单 {order.order_id} 已处于终态 {order.status.value}，无法提交")
        order.status = OrderStatus.SUBMITTED
        self.working.append(order)
        self.stats.submitted += 1
        return order

    def submit_many(self, orders: Sequence[Order]) -> list[Order]:
        return [self.submit(o) for o in orders]

    def cancel(self, order: Order | str, reason: RejectReason = RejectReason.NONE) -> None:
        order = self.orders[order] if isinstance(order, str) else order
        if order.status.is_terminal:
            return
        order.cancel(reason)
        self._retire(order, OrderStatus.CANCELLED)

    def cancel_all(self, reason: RejectReason = RejectReason.NONE) -> int:
        count = 0
        for order in list(self.working):
            self.cancel(order, reason)
            count += 1
        return count

    # ------------------------------------------------------------------ #
    # 撮合
    # ------------------------------------------------------------------ #
    def process_open(
        self,
        snapshot: MarketSnapshot,
        *,
        risk_hook: RiskHook | None = None,
    ) -> list[Fill]:
        """在当日开盘撮合所有到期的活动订单，返回成交回报列表。"""
        new_fills: list[Fill] = []
        for order in list(self.working):
            if order.status.is_terminal:
                self._retire(order, order.status)
                continue
            if order.submit_date is not None and order.submit_date > snapshot.date:
                continue  # 尚未到期（T+1）

            if risk_hook is not None:
                decision = risk_hook(order, self.account, snapshot)
                self.audit.log(
                    "risk",
                    "check",
                    ts=snapshot.ts,
                    order_id=order.order_id,
                    risk_action=decision.action.value,
                    rule=decision.rule,
                )
                if decision.action is RiskAction.REJECT:
                    order.mark_rejected(decision.reject_reason or RejectReason.RISK_REJECTED, by_risk=True)
                    self._retire(order, order.status)
                    self.stats.rejected += 1
                    continue
                if decision.action is RiskAction.PAUSE:
                    continue  # 暂停：保持挂单，等待恢复
                if decision.action is RiskAction.REDUCE and decision.modified_quantity is not None:
                    order.quantity = min(order.quantity, float(decision.modified_quantity))

            result = self.matching.try_match(order, snapshot, account=self.account)
            fill = self._handle_result(order, result, snapshot)
            if fill is not None:
                new_fills.append(fill)
        return new_fills

    def _handle_result(self, order: Order, result: MatchResult, snapshot: MarketSnapshot) -> Fill | None:
        if result.filled:
            self._fill_seq += 1
            fill = Fill(
                fill_id=f"F{self._fill_seq:08d}",
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=result.filled_quantity,
                price=result.price,
                trade_date=snapshot.date,
                ts=snapshot.ts,
                gross_amount=result.filled_quantity * result.price,
                cost=result.cost,
                source=self._fill_source(),
                deferred_days=order.deferred_days,
                tag=order.tag,
            )
            order.apply_fill(result.filled_quantity, result.price)
            self.account.apply_fill(fill)
            self.fills.append(fill)
            self.stats.fill_count += 1
            if order.status is OrderStatus.FILLED:
                self.stats.filled += 1
                self._retire(order, OrderStatus.FILLED)
            else:
                self.stats.partially_filled += 1
                self.stats.reasons[result.reason.value] = self.stats.reasons.get(result.reason.value, 0) + 1
            self.audit.log(
                "fill",
                "filled",
                ts=snapshot.ts,
                order_id=order.order_id,
                symbol=order.symbol,
                quantity=result.filled_quantity,
                price=result.price,
                cost=round(fill.total_cost, 4),
            )
            return fill

        # 未成交
        if result.can_retry and order.time_in_force is TimeInForce.GTC:
            self.stats.deferred_events += 1
            self.stats.reasons[result.reason.value] = self.stats.reasons.get(result.reason.value, 0) + 1
            still_active = order.defer(result.reason)
            if not still_active:
                self.stats.expired += 1
                self._retire(order, OrderStatus.EXPIRED)
                self.audit.log(
                    "order", "expired", ts=snapshot.ts, order_id=order.order_id, reason=result.reason.value
                )
            return None

        order.mark_rejected(result.reason)
        self.stats.rejected += 1
        self.stats.reasons[result.reason.value] = self.stats.reasons.get(result.reason.value, 0) + 1
        self._retire(order, order.status)
        self.audit.log("order", "rejected", ts=snapshot.ts, order_id=order.order_id, reason=result.reason.value)
        return None

    def _fill_source(self) -> FillSource:
        mode = self.config.execution_price
        if mode == "vwap":
            return FillSource.VWAP
        if mode == "close":
            return FillSource.CLOSE
        return FillSource.OPEN

    def _retire(self, order: Order, status: OrderStatus) -> None:
        if order in self.working:
            self.working.remove(order)
        if order not in self.closed:
            self.closed.append(order)

    # ------------------------------------------------------------------ #
    # 查询与导出
    # ------------------------------------------------------------------ #
    def open_orders(self, symbol: str | None = None) -> list[Order]:
        if symbol is None:
            return list(self.working)
        return [o for o in self.working if o.symbol == symbol]

    def order(self, order_id: str) -> Order:
        try:
            return self.orders[order_id]
        except KeyError as exc:
            raise EngineError(f"未知订单号：{order_id}") from exc

    def pending_quantity(self, symbol: str, side: Side | None = None) -> float:
        return sum(
            o.remaining for o in self.working if o.symbol == symbol and (side is None or o.side is side)
        )

    def fills_frame(self) -> Any:
        """成交明细 DataFrame。"""
        import pandas as pd

        if not self.fills:
            return pd.DataFrame()
        return pd.DataFrame([f.to_dict() for f in self.fills])

    def orders_frame(self) -> Any:
        import pandas as pd

        if not self.orders:
            return pd.DataFrame()
        return pd.DataFrame([o.to_dict() for o in self.orders.values()])

    def summary(self) -> dict[str, Any]:
        d = self.stats.as_dict()
        d["open_orders"] = len(self.working)
        d["match_stats"] = dict(self.matching.stats)
        return d
