"""事件定义与事件队列。

事件类型（对应合同 §六）：
:class:`TimerEvent` / :class:`MarketDataEvent` / :class:`SignalEvent` /
:class:`RiskCheckEvent` / :class:`OrderEvent` / :class:`FillEvent`

排序规则：``(timestamp, priority, 入队序号)``。
- ``priority`` 默认取自 :class:`EventType` 的整数值（Timer < MarketData < Signal < RiskCheck < Order < Fill）；
- 同一时间戳、同一优先级的多个事件按入队先后（FIFO）处理，保证回测可复现。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Iterable, Iterator, Mapping, Sequence

import pandas as pd

from .enums import EventType, SessionPhase
from .exceptions import EngineError
from .logging import get_logger
from .models import Fill, MarketSnapshot, Order, SignalIntent

__all__ = [
    "Event",
    "TimerEvent",
    "MarketDataEvent",
    "SignalEvent",
    "RiskCheckEvent",
    "OrderEvent",
    "FillEvent",
    "EventQueue",
    "EventRecorder",
    "EventRecord",
    "EventLoop",
    "LoopStats",
]

logger = get_logger("core.events")

Handler = Callable[[Any], None]


class Event:
    """事件基类（仅提供协议与优先级逻辑，不作为数据容器使用）。"""

    __slots__ = ()

    EVENT_TYPE: ClassVar[EventType]
    timestamp: pd.Timestamp
    priority_override: int | None

    @property
    def event_type(self) -> EventType:
        return type(self).EVENT_TYPE

    @property
    def priority(self) -> int:
        if self.priority_override is not None:
            return int(self.priority_override)
        return int(self.EVENT_TYPE)

    @property
    def trade_date(self) -> Any:
        """事件所属交易日。"""
        return self.timestamp.date()

    def summary(self) -> str:  # pragma: no cover - 仅用于日志/录制
        return self.event_type.name


# --------------------------------------------------------------------------- #
# 具体事件
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class TimerEvent(Event):
    """定时事件：开盘前准备、收盘估值、盘后信号计算。"""

    timestamp: pd.Timestamp
    phase: SessionPhase = SessionPhase.PRE_OPEN
    timer_id: str = "daily"
    priority_override: int | None = None

    EVENT_TYPE: ClassVar[EventType] = EventType.TIMER

    def summary(self) -> str:
        return f"Timer({self.timer_id}/{self.phase.value})"


@dataclass(frozen=True, slots=True)
class MarketDataEvent(Event):
    """行情事件：携带当日某阶段的市场快照。"""

    timestamp: pd.Timestamp
    phase: SessionPhase = SessionPhase.OPEN
    snapshot: MarketSnapshot | None = None
    priority_override: int | None = None

    EVENT_TYPE: ClassVar[EventType] = EventType.MARKET_DATA

    def summary(self) -> str:
        n = len(self.snapshot) if self.snapshot is not None else 0
        return f"MarketData({self.phase.value}, {self.trade_date}, {n}个标的)"


@dataclass(frozen=True, slots=True)
class SignalEvent(Event):
    """信号事件：策略在 T 日收盘后输出的一组信号意向。"""

    timestamp: pd.Timestamp
    signals: tuple[SignalIntent, ...] = ()
    strategy: str = ""
    priority_override: int | None = None

    EVENT_TYPE: ClassVar[EventType] = EventType.SIGNAL

    def __post_init__(self) -> None:
        if not isinstance(self.signals, tuple):
            object.__setattr__(self, "signals", tuple(self.signals))

    def summary(self) -> str:
        return f"Signal({self.strategy or 'strategy'}, {len(self.signals)}条)"


@dataclass(frozen=True, slots=True)
class RiskCheckEvent(Event):
    """风控检查事件：订单提交给撮合之前必须经过 RMS。"""

    timestamp: pd.Timestamp
    order: Order | None = None
    account: Any = None
    snapshot: MarketSnapshot | None = None
    priority_override: int | None = None

    EVENT_TYPE: ClassVar[EventType] = EventType.RISK_CHECK

    def summary(self) -> str:
        oid = self.order.order_id if self.order is not None else "-"
        return f"RiskCheck({oid})"


@dataclass(frozen=True, slots=True)
class OrderEvent(Event):
    """订单事件：通过风控、进入撮合队列。"""

    timestamp: pd.Timestamp
    order: Order | None = None
    priority_override: int | None = None

    EVENT_TYPE: ClassVar[EventType] = EventType.ORDER

    def summary(self) -> str:
        if self.order is None:
            return "Order(-)"
        return f"Order({self.order.order_id}, {self.order.side.value}, {self.order.symbol})"


@dataclass(frozen=True, slots=True)
class FillEvent(Event):
    """成交事件：撮合完成后的成交回报。"""

    timestamp: pd.Timestamp
    fill: Fill | None = None
    priority_override: int | None = None

    EVENT_TYPE: ClassVar[EventType] = EventType.FILL

    def summary(self) -> str:
        if self.fill is None:
            return "Fill(-)"
        return f"Fill({self.fill.symbol}, {self.fill.side.value}, {self.fill.quantity:g}@{self.fill.price:.3f})"


# --------------------------------------------------------------------------- #
# 事件队列
# --------------------------------------------------------------------------- #
class EventQueue:
    """按 ``(timestamp, priority, seq)`` 排序的最小堆事件队列。"""

    __slots__ = ("_heap", "_seq", "_pushed")

    def __init__(self) -> None:
        self._heap: list[tuple[int, int, int, Event]] = []
        self._seq = 0
        self._pushed = 0

    def push(self, event: Event) -> int:
        self._seq += 1
        self._pushed += 1
        heapq.heappush(self._heap, (int(event.timestamp.value), int(event.priority), self._seq, event))
        return self._seq

    def push_many(self, events: Iterable[Event]) -> None:
        for e in events:
            self.push(e)

    def pop(self) -> Event:
        if not self._heap:
            raise IndexError("事件队列为空")
        return heapq.heappop(self._heap)[3]

    def peek(self) -> Event | None:
        return self._heap[0][3] if self._heap else None

    def peek_timestamp(self) -> pd.Timestamp | None:
        if not self._heap:
            return None
        return pd.Timestamp(self._heap[0][0])

    def clear(self) -> None:
        self._heap.clear()

    def __len__(self) -> int:
        return len(self._heap)

    def __bool__(self) -> bool:
        return bool(self._heap)

    def __iter__(self) -> Iterator[Event]:
        return iter([item[3] for item in sorted(self._heap, key=lambda x: (x[0], x[1], x[2]))])

    @property
    def pushed_count(self) -> int:
        return self._pushed


# --------------------------------------------------------------------------- #
# 事件录制（用于验收：事件顺序正确）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class EventRecord:
    """一条事件处理记录。"""

    seq: int
    timestamp: pd.Timestamp
    event_type: str
    priority: int
    summary: str

    def __str__(self) -> str:
        return f"#{self.seq} {self.timestamp:%Y-%m-%d %H:%M} {self.event_type}({self.priority}) {self.summary}"


class EventRecorder:
    """事件录制器：记录处理顺序，供测试断言与审计。"""

    __slots__ = ("_records", "enabled")

    def __init__(self, *, enabled: bool = True) -> None:
        self._records: list[EventRecord] = []
        self.enabled = enabled

    def record(self, seq: int, event: Event) -> None:
        if not self.enabled:
            return
        self._records.append(
            EventRecord(
                seq=seq,
                timestamp=event.timestamp,
                event_type=event.event_type.name,
                priority=int(event.priority),
                summary=event.summary(),
            )
        )

    @property
    def records(self) -> list[EventRecord]:
        return list(self._records)

    def types(self) -> list[str]:
        return [r.event_type for r in self._records]

    def summaries(self) -> list[str]:
        return [r.summary for r in self._records]

    def timeline(self) -> list[tuple[str, str, str]]:
        return [(str(r.timestamp), r.event_type, r.summary) for r in self._records]

    def of_type(self, event_type: EventType | str) -> list[EventRecord]:
        name = event_type.name if isinstance(event_type, EventType) else str(event_type)
        return [r for r in self._records if r.event_type == name]

    def clear(self) -> None:
        self._records.clear()

    def __len__(self) -> int:
        return len(self._records)


@dataclass(slots=True)
class LoopStats:
    """事件循环统计。"""

    published: int = 0
    processed: int = 0
    unhandled: int = 0
    errors: int = 0
    by_type: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "published": self.published,
            "processed": self.processed,
            "unhandled": self.unhandled,
            "errors": self.errors,
            "by_type": dict(self.by_type),
        }


# --------------------------------------------------------------------------- #
# 事件循环
# --------------------------------------------------------------------------- #
class EventLoop:
    """事件循环：发布 → 排序 → 分发。

    Args:
        on_error: ``"raise"`` 立即抛出；``"log"`` 记录后继续；``"collect"`` 收集到 ``self.errors``。
        recorder: 可选的 :class:`EventRecorder`。
    """

    def __init__(
        self,
        *,
        on_error: str = "raise",
        recorder: EventRecorder | None = None,
    ) -> None:
        if on_error not in ("raise", "log", "collect"):
            raise EngineError(f"未知的 on_error 策略：{on_error}")
        self.queue = EventQueue()
        self.recorder = recorder if recorder is not None else EventRecorder()
        self.on_error = on_error
        self.stats = LoopStats()
        self.errors: list[tuple[Event, BaseException]] = []
        self._handlers: dict[EventType, list[Handler]] = {}
        self._wildcard: list[Handler] = []
        self._processed_seq = 0

    # ------------------------------ 订阅 ------------------------------ #
    def subscribe(self, key: EventType | type | str, handler: Handler) -> Callable[[], None]:
        """注册处理器，返回取消订阅的可调用对象。

        ``key`` 可以是 :class:`EventType`、事件类，或 ``"*"`` 通配。
        """
        if key == "*":
            self._wildcard.append(handler)
            return lambda: self._wildcard.remove(handler)

        etype = key if isinstance(key, EventType) else getattr(key, "EVENT_TYPE", None)
        if etype is None:
            raise EngineError(f"无法识别的订阅键：{key!r}")
        handlers = self._handlers.setdefault(etype, [])
        handlers.append(handler)
        return lambda: handlers.remove(handler)

    def subscribers(self, event_type: EventType) -> list[Handler]:
        return list(self._handlers.get(event_type, ()))

    # ------------------------------ 发布 ------------------------------ #
    def publish(self, event: Event) -> None:
        self.queue.push(event)
        self.stats.published += 1

    def publish_many(self, events: Iterable[Event]) -> None:
        for e in events:
            self.publish(e)

    # ------------------------------ 处理 ------------------------------ #
    def process_next(self) -> Event | None:
        """处理队列中最前面的一个事件；队列为空时返回 ``None``。"""
        if not self.queue:
            return None
        event = self.queue.pop()
        self._processed_seq += 1
        self.recorder.record(self._processed_seq, event)
        self.stats.processed += 1
        key = event.event_type.name
        self.stats.by_type[key] = self.stats.by_type.get(key, 0) + 1

        handlers = list(self._handlers.get(event.event_type, ())) + list(self._wildcard)
        if not handlers:
            self.stats.unhandled += 1
            return event
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001 - 统一错误策略
                self.stats.errors += 1
                self.errors.append((event, exc))
                if self.on_error == "raise":
                    raise
                logger.exception("处理事件 %s 时出错：%s", event.summary(), exc)
        return event

    def drain(self, max_events: int | None = None) -> int:
        """处理直到队列为空或达到上限，返回处理数量。"""
        count = 0
        while (max_events is None or count < max_events) and self.queue:
            self.process_next()
            count += 1
        return count

    # 语义化别名：日频主循环中「本阶段事件全部处理完」的意图更清晰
    run = drain

    def step(self) -> Event | None:
        """兼容 alias。"""
        return self.process_next()

    def pending_types(self) -> list[str]:
        return [e.event_type.name for e in self.queue]

    def __len__(self) -> int:
        return len(self.queue)
