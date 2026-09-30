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
from ..core.quantity import floor_lot, sell_quantity
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
    """经纪商统计（用于回测验收与报告）。

    口径（缺陷修复 #3 / #13）：

    - ``expired``：有效期内未成交而作废（DAY 当日过期 + GTC 顺延超限 + 剩余不可执行）；
    - ``expired_day``：其中由 ``TimeInForce.DAY`` 语义导致的过期数；
    - ``expired_residue``：其中因**部分成交后剩余不足一手**而作废的数量（缺陷 #13）；
    - ``expired_no_remaining``：其中因**剩余数量已被风控削减为 0** 而作废的数量（缺陷 #13）；
    - ``rejected``：被硬约束或风控拒绝（退市/不足一手/超持仓/风控规则…）；
    - **过期不计入拒单率**。
    """

    created: int = 0
    submitted: int = 0
    filled: int = 0
    partially_filled: int = 0
    expired: int = 0
    expired_day: int = 0
    expired_residue: int = 0
    expired_no_remaining: int = 0
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
            "expired_day": self.expired_day,
            "expired_residue": self.expired_residue,
            "expired_no_remaining": self.expired_no_remaining,
            "rejected": self.rejected,
            "cancelled": self.cancelled,
            "deferred_events": self.deferred_events,
            "fill_count": self.fill_count,
        }
        d.update({f"reason_{k}": v for k, v in self.reasons.items()})
        return d

    @property
    def genuine_rejected(self) -> int:
        """真实拒单数 = ``rejected``（缺陷 #13 后，「剩余不足一手」已不再计入其中）。

        保留此属性是为了让报告层显式表达「拒单率只统计真实拒单」这一口径。
        """
        return self.rejected


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

    def cancel(
        self,
        order: Order | str,
        reason: RejectReason = RejectReason.NONE,
        *,
        day: _date | None = None,
    ) -> None:
        order = self.orders[order] if isinstance(order, str) else order
        if order.status.is_terminal:
            return
        order.cancel(reason, day=day)
        self._retire(order, OrderStatus.CANCELLED)

    def cancel_all(self, reason: RejectReason = RejectReason.NONE, *, day: _date | None = None) -> int:
        count = 0
        for order in list(self.working):
            self.cancel(order, reason, day=day)
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

            # DAY 订单只在 submit_date 当日有效：跨到下一个交易日仍未完成 → 过期（不计入拒单）
            if (
                order.time_in_force is TimeInForce.DAY
                and order.submit_date is not None
                and order.submit_date < snapshot.date
            ):
                self.stats.expired += 1
                self.stats.expired_day += 1
                order.expire(order.reject_reason, day=snapshot.date)
                self._retire(order, OrderStatus.EXPIRED)
                self.audit.log(
                    "order",
                    "expired",
                    ts=snapshot.ts,
                    order_id=order.order_id,
                    reason="day_order_session_passed",
                )
                continue

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
                    order.mark_rejected(
                        decision.reject_reason or RejectReason.RISK_REJECTED,
                        by_risk=True,
                        day=snapshot.date,
                    )
                    self._retire(order, order.status)
                    self.stats.rejected += 1
                    continue
                if decision.action is RiskAction.PAUSE:
                    continue  # 暂停：保持挂单，等待恢复
                if decision.action is RiskAction.REDUCE and decision.modified_quantity is not None:
                    self._apply_reduce(order, float(decision.modified_quantity))

            result = self.matching.try_match(order, snapshot, account=self.account)
            fill = self._handle_result(order, result, snapshot)
            if fill is not None:
                new_fills.append(fill)
        return new_fills

    def _lot_size_for(self, side: Side) -> int:
        """该方向适用的整手股数（BUY 需要整手；SELL 只需整数股）。"""
        return int(self.config.lot_size) if side is Side.BUY else 0

    def _apply_reduce(self, order: Order, modified_quantity: float) -> None:
        """应用风控削减量（缺陷 #13）。

        两条不变量：

        1. **数量必须是可执行股数**：BUY 向下取整到整手，SELL 向下取整到整数股；
        2. **INV-6：``filled_quantity <= quantity`` 恒成立**。
           削减量是对**剩余可下单量**的封顶（RMS 规则以 ``order.remaining`` 为基准计算），
           因此新的总量 = 已成交 + 被封顶后的剩余，绝不允许把总量压到已成交量之下
           （旧实现直接 ``min(quantity, modified)``，产生了 ``filled > quantity`` 的
           自相矛盾订单与只会顺延到期的「僵尸订单」）。
        """
        cap = sell_quantity(modified_quantity, self._lot_size_for(order.side))
        if order.side is Side.BUY:
            cap = floor_lot(modified_quantity, int(self.config.lot_size))
        new_remaining = min(order.remaining, cap)
        order.quantity = order.filled_quantity + max(new_remaining, 0.0)

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

        # 未成交：按有效期语义区分「顺延 / 过期 / 拒单」（缺陷修复 #3）
        self.stats.reasons[result.reason.value] = self.stats.reasons.get(result.reason.value, 0) + 1

        # 缺陷 #13：部分成交后「剩余不足一手」→ **过期**，而不是硬拒单。
        # 订单确实成交了一部分，把它标成 rejected 会同时造成两个后果：
        #   (1) orders.csv 出现「已成交但状态为拒单」的自相矛盾记录；
        #   (2) 拒单率被大量虚增（实测占到 ma_cross 拒单数的 80%）。
        lot = self._lot_size_for(order.side)
        if lot > 0 and order.filled_quantity > 0 and 0 < order.remaining < lot:
            self.stats.expired += 1
            self.stats.expired_residue += 1
            order.expire(RejectReason.LOT_SIZE_RESIDUE, day=snapshot.date)
            self._retire(order, OrderStatus.EXPIRED)
            self.audit.log(
                "order",
                "expired",
                ts=snapshot.ts,
                order_id=order.order_id,
                reason="lot_size_residue",
                remaining=float(order.remaining),
                filled=float(order.filled_quantity),
                lot_size=lot,
            )
            return None

        # 缺陷 #13：剩余数量已被削减为 0 → 直接结算，不再无意义顺延（消除僵尸订单）。
        if order.remaining <= 0:
            self.stats.expired += 1
            self.stats.expired_no_remaining += 1
            order.expire(order.reject_reason, day=snapshot.date)
            self._retire(order, OrderStatus.EXPIRED)
            self.audit.log(
                "order",
                "expired",
                ts=snapshot.ts,
                order_id=order.order_id,
                reason="no_remaining",
                filled=float(order.filled_quantity),
            )
            return None

        if result.can_retry and order.time_in_force is TimeInForce.GTC:
            self.stats.deferred_events += 1
            still_active = order.defer(result.reason, day=snapshot.date)
            if not still_active:
                self.stats.expired += 1
                self._retire(order, OrderStatus.EXPIRED)
                self.audit.log(
                    "order",
                    "expired",
                    ts=snapshot.ts,
                    order_id=order.order_id,
                    reason=result.reason.value,
                )
            return None

        if result.can_retry:
            # DAY：当日有效，未成交即过期（不计入拒单统计）
            self.stats.expired += 1
            self.stats.expired_day += 1
            order.expire(result.reason, day=snapshot.date)
            self._retire(order, OrderStatus.EXPIRED)
            self.audit.log(
                "order",
                "expired",
                ts=snapshot.ts,
                order_id=order.order_id,
                reason=f"day_expired:{result.reason.value}",
            )
            return None

        # 硬拒绝（退市 / 不足一手 / 超持仓 / 限价未触及 …）
        order.mark_rejected(result.reason, day=snapshot.date)
        self.stats.rejected += 1
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
