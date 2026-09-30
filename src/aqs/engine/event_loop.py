"""回测引擎的事件循环封装。

在 :class:`aqs.core.events.EventLoop` 之上提供「交易阶段」语义化发布接口，
使日频主循环的代码与设计文档 §5.2 一一对应，且事件时间戳天然有序：

    09:00 PRE_OPEN → 09:30 OPEN(行情+成交) → 15:00 CLOSE(估值) → 15:10 POST_CLOSE(信号)
"""

from __future__ import annotations

from datetime import date as _date
from enum import IntEnum
from typing import Iterable, Sequence

from ..core.dates import DateLike, phase_timestamp, to_date
from ..core.enums import EventType, SessionPhase
from ..core.events import (
    Event,
    EventLoop,
    EventQueue,
    EventRecord,
    EventRecorder,
    FillEvent,
    LoopStats,
    MarketDataEvent,
    OrderEvent,
    RiskCheckEvent,
    SignalEvent,
    TimerEvent,
)
from ..core.models import Fill, MarketSnapshot, Order, SignalIntent

__all__ = [
    "EngineEventLoop",
    "StepPriority",
    "Event",
    "EventLoop",
    "EventQueue",
    "EventRecord",
    "EventRecorder",
    "LoopStats",
]


class StepPriority(IntEnum):
    """日内步骤优先级。

    同一个交易日里多个事件可能落在**同一时间戳**（例如 15:00 的收盘行情与收盘定时器），
    仅靠事件类型优先级无法表达「先行情、后定时器」的先后。
    引擎因此显式使用「步骤优先级」，保证每日事件序列与设计文档 §5.2 完全一致：

        09:00 定时器(PRE_OPEN) → 09:30 行情(OPEN) → 09:30 风控/订单/成交
        → 15:00 行情(CLOSE) → 15:00 定时器(CLOSE) → 15:10 信号 → 15:10 风控 → 15:10 订单
    """

    PRE_OPEN_TIMER = 0
    OPEN_MARKET_DATA = 10
    OPEN_RISK_CHECK = 20
    OPEN_ORDER = 30
    OPEN_FILL = 40
    CLOSE_MARKET_DATA = 50
    CLOSE_TIMER = 60
    POST_CLOSE_TIMER = 61
    POST_CLOSE_SIGNAL = 70
    POST_CLOSE_RISK_CHECK = 80
    POST_CLOSE_ORDER = 90


class EngineEventLoop(EventLoop):
    """带交易阶段语义的事件循环。"""

    def __init__(self, *, on_error: str = "raise", recorder: EventRecorder | None = None) -> None:
        super().__init__(on_error=on_error, recorder=recorder)
        self.current_date: _date | None = None
        self.current_phase: SessionPhase | None = None

    # ------------------------------------------------------------------ #
    # 语义化发布
    # ------------------------------------------------------------------ #
    def publish_timer(self, day: DateLike, phase: SessionPhase, timer_id: str = "daily") -> TimerEvent:
        priority = (
            StepPriority.PRE_OPEN_TIMER
            if phase is SessionPhase.PRE_OPEN
            else StepPriority.CLOSE_TIMER
            if phase is SessionPhase.CLOSE
            else StepPriority.POST_CLOSE_TIMER
        )
        event = TimerEvent(
            timestamp=phase_timestamp(day, phase), phase=phase, timer_id=timer_id, priority_override=int(priority)
        )
        self.publish(event)
        self._mark(day, phase)
        return event

    def publish_market_data(self, day: DateLike, phase: SessionPhase, snapshot: MarketSnapshot) -> MarketDataEvent:
        priority = (
            StepPriority.OPEN_MARKET_DATA if phase is SessionPhase.OPEN else StepPriority.CLOSE_MARKET_DATA
        )
        event = MarketDataEvent(
            timestamp=phase_timestamp(day, phase), phase=phase, snapshot=snapshot, priority_override=int(priority)
        )
        self.publish(event)
        self._mark(day, phase)
        return event

    def publish_signals(self, day: DateLike, signals: Iterable[SignalIntent], strategy: str = "") -> SignalEvent:
        event = SignalEvent(
            timestamp=phase_timestamp(day, SessionPhase.POST_CLOSE),
            signals=tuple(signals),
            strategy=strategy,
            priority_override=int(StepPriority.POST_CLOSE_SIGNAL),
        )
        self.publish(event)
        self._mark(day, SessionPhase.POST_CLOSE)
        return event

    def publish_risk_check(
        self,
        snapshot: MarketSnapshot,
        order: Order,
        account: object | None = None,
    ) -> RiskCheckEvent:
        priority = (
            StepPriority.OPEN_RISK_CHECK
            if snapshot.phase is SessionPhase.OPEN
            else StepPriority.POST_CLOSE_RISK_CHECK
        )
        event = RiskCheckEvent(
            timestamp=snapshot.ts or phase_timestamp(snapshot.date, snapshot.phase),
            order=order,
            account=account,
            snapshot=snapshot,
            priority_override=int(priority),
        )
        self.publish(event)
        return event

    def publish_order(self, snapshot: MarketSnapshot, order: Order) -> OrderEvent:
        priority = StepPriority.OPEN_ORDER if snapshot.phase is SessionPhase.OPEN else StepPriority.POST_CLOSE_ORDER
        event = OrderEvent(
            timestamp=snapshot.ts or phase_timestamp(snapshot.date, snapshot.phase),
            order=order,
            priority_override=int(priority),
        )
        self.publish(event)
        return event

    def publish_fill(self, snapshot: MarketSnapshot, fill: Fill) -> FillEvent:
        event = FillEvent(
            timestamp=fill.ts or snapshot.ts or phase_timestamp(snapshot.date, snapshot.phase),
            fill=fill,
            priority_override=int(StepPriority.OPEN_FILL),
        )
        self.publish(event)
        return event

    def publish_fills(self, snapshot: MarketSnapshot, fills: Sequence[Fill]) -> int:
        for fill in fills:
            self.publish_fill(snapshot, fill)
        return len(fills)

    # ------------------------------------------------------------------ #
    def _mark(self, day: DateLike, phase: SessionPhase) -> None:
        self.current_date = to_date(day)
        self.current_phase = phase

    def phase_sequence(self) -> list[str]:
        """已处理事件中每日的阶段序列（用于验收测试）。"""
        return [r.summary for r in self.recorder.records if r.event_type == EventType.MARKET_DATA.name or r.event_type == EventType.TIMER.name]
