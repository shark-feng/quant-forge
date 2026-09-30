"""事件系统测试：排序、FIFO、录制、订阅、错误策略、阶段时间戳。"""

from __future__ import annotations

import pandas as pd

from tests.compat import approx, raises
from tests.tools import DEFAULT_START, make_bar, make_order, make_snapshot

from aqs.core.enums import EventType, SessionPhase, Side
from aqs.core.events import (
    EventLoop,
    EventQueue,
    EventRecorder,
    FillEvent,
    MarketDataEvent,
    OrderEvent,
    RiskCheckEvent,
    SignalEvent,
    TimerEvent,
)
from aqs.core.exceptions import EngineError
from aqs.core.models import Fill, SignalIntent
from aqs.engine.event_loop import EngineEventLoop

DAY = pd.Timestamp(DEFAULT_START)
TS = DAY + pd.Timedelta(hours=15)


def _bar_events():
    snapshot = make_snapshot([make_bar(day=DAY)], day=DAY)
    return snapshot


# --------------------------------------------------------------------------- #
# 队列排序
# --------------------------------------------------------------------------- #
def test_queue_orders_by_priority_for_same_timestamp():
    q = EventQueue()
    snapshot = _bar_events()
    fill = Fill(
        fill_id="F1",
        order_id="O1",
        symbol="600000.SH",
        side=Side.BUY,
        quantity=100,
        price=10.0,
        trade_date=DAY.date(),
    )
    # 故意逆序入队
    q.push(FillEvent(timestamp=TS, fill=fill))
    q.push(OrderEvent(timestamp=TS, order=make_order()))
    q.push(RiskCheckEvent(timestamp=TS, order=make_order()))
    q.push(SignalEvent(timestamp=TS, signals=()))
    q.push(MarketDataEvent(timestamp=TS, phase=SessionPhase.CLOSE, snapshot=snapshot))
    q.push(TimerEvent(timestamp=TS, phase=SessionPhase.CLOSE))
    order = [q.pop().event_type for _ in range(6)]
    assert order == [
        EventType.TIMER,
        EventType.MARKET_DATA,
        EventType.SIGNAL,
        EventType.RISK_CHECK,
        EventType.ORDER,
        EventType.FILL,
    ]


def test_queue_orders_by_timestamp_first():
    q = EventQueue()
    late = TimerEvent(timestamp=TS + pd.Timedelta(days=1), phase=SessionPhase.PRE_OPEN)
    early_fill = FillEvent(timestamp=TS, fill=None)
    q.push(late)
    q.push(early_fill)
    assert q.pop() is early_fill
    assert q.pop() is late


def test_queue_fifo_for_equal_priority():
    q = EventQueue()
    first = TimerEvent(timestamp=TS, phase=SessionPhase.PRE_OPEN, timer_id="a")
    second = TimerEvent(timestamp=TS, phase=SessionPhase.PRE_OPEN, timer_id="b")
    third = TimerEvent(timestamp=TS, phase=SessionPhase.PRE_OPEN, timer_id="c")
    q.push(first)
    q.push(second)
    q.push(third)
    assert [q.pop().timer_id for _ in range(3)] == ["a", "b", "c"]
    assert len(q) == 0
    with raises(IndexError):
        q.pop()


def test_queue_peek_and_clear():
    q = EventQueue()
    assert q.peek() is None
    q.push(TimerEvent(timestamp=TS, phase=SessionPhase.PRE_OPEN))
    assert q.peek() is not None
    q.clear()
    assert len(q) == 0
    assert bool(q) is False


def test_priority_override():
    q = EventQueue()
    normal = TimerEvent(timestamp=TS, phase=SessionPhase.PRE_OPEN, timer_id="normal")
    forced = TimerEvent(timestamp=TS, phase=SessionPhase.PRE_OPEN, timer_id="forced", priority_override=99)
    q.push(forced)
    q.push(normal)
    assert q.pop() is normal
    assert forced.priority == 99
    assert normal.priority == int(EventType.TIMER)


# --------------------------------------------------------------------------- #
# 事件循环
# --------------------------------------------------------------------------- #
def test_loop_dispatches_to_subscribers_in_order():
    loop = EventLoop()
    seen: list[str] = []
    loop.subscribe(EventType.TIMER, lambda e: seen.append("A"))
    loop.subscribe(EventType.TIMER, lambda e: seen.append("B"))
    loop.subscribe("*", lambda e: seen.append("wild"))
    loop.publish(TimerEvent(timestamp=TS, phase=SessionPhase.PRE_OPEN))
    loop.drain()
    assert seen == ["A", "B", "wild"]


def test_loop_unsubscribe():
    loop = EventLoop()
    seen: list[str] = []
    dispose = loop.subscribe(EventType.TIMER, lambda e: seen.append("x"))
    loop.publish(TimerEvent(timestamp=TS, phase=SessionPhase.PRE_OPEN))
    loop.drain()
    dispose()
    loop.publish(TimerEvent(timestamp=TS, phase=SessionPhase.PRE_OPEN))
    loop.drain()
    assert seen == ["x"]


def test_loop_unhandled_and_stats():
    loop = EventLoop()
    loop.publish(TimerEvent(timestamp=TS, phase=SessionPhase.PRE_OPEN))
    loop.publish(TimerEvent(timestamp=TS, phase=SessionPhase.PRE_OPEN))
    loop.drain()
    assert loop.stats.published == 2
    assert loop.stats.processed == 2
    assert loop.stats.unhandled == 2
    assert loop.stats.by_type["TIMER"] == 2


def test_loop_error_policy_collect():
    loop = EventLoop(on_error="collect")

    def boom(event):
        raise ValueError("处理失败")

    loop.subscribe(EventType.TIMER, boom)
    loop.publish(TimerEvent(timestamp=TS, phase=SessionPhase.PRE_OPEN))
    loop.drain()
    assert loop.stats.errors == 1
    assert len(loop.errors) == 1
    assert isinstance(loop.errors[0][1], ValueError)


def test_loop_error_policy_raise():
    loop = EventLoop(on_error="raise")
    loop.subscribe(EventType.TIMER, lambda e: (_ for _ in ()).throw(ValueError("boom")))
    loop.publish(TimerEvent(timestamp=TS, phase=SessionPhase.PRE_OPEN))
    with raises(ValueError):
        loop.drain()


def test_loop_drain_max_events():
    loop = EventLoop()
    for _ in range(5):
        loop.publish(TimerEvent(timestamp=TS, phase=SessionPhase.PRE_OPEN))
    assert loop.drain(max_events=2) == 2
    assert len(loop) == 3
    assert loop.drain() == 3


def test_loop_unknown_subscription_key():
    loop = EventLoop()
    with raises(EngineError):
        loop.subscribe(12345, lambda e: None)


# --------------------------------------------------------------------------- #
# 录制器
# --------------------------------------------------------------------------- #
def test_recorder_captures_order_and_types():
    loop = EventLoop(recorder=EventRecorder())
    snapshot = _bar_events()
    loop.publish(TimerEvent(timestamp=TS, phase=SessionPhase.PRE_OPEN))
    loop.publish(MarketDataEvent(timestamp=TS, phase=SessionPhase.CLOSE, snapshot=snapshot))
    loop.drain()
    rec = loop.recorder
    assert rec.types() == ["TIMER", "MARKET_DATA"]
    assert len(rec) == 2
    assert len(rec.of_type(EventType.MARKET_DATA)) == 1
    assert "MarketData" in rec.summaries()[1]
    assert rec.timeline()[0][0] == str(TS)


def test_recorder_can_be_disabled():
    rec = EventRecorder(enabled=False)
    loop = EventLoop(recorder=rec)
    loop.publish(TimerEvent(timestamp=TS, phase=SessionPhase.PRE_OPEN))
    loop.drain()
    assert len(rec) == 0


# --------------------------------------------------------------------------- #
# 引擎事件循环：阶段语义
# --------------------------------------------------------------------------- #
def test_engine_loop_phase_ordering():
    loopobj = EngineEventLoop()
    snapshot = _bar_events()
    day = DAY.date()
    loopobj.publish_timer(day, SessionPhase.PRE_OPEN)
    loopobj.publish_market_data(day, SessionPhase.OPEN, snapshot)
    loopobj.publish_market_data(day, SessionPhase.CLOSE, snapshot)
    loopobj.publish_timer(day, SessionPhase.CLOSE)
    loopobj.publish_timer(day, SessionPhase.POST_CLOSE)
    loopobj.drain()
    assert loopobj.recorder.summaries() == [
        "Timer(daily/pre_open)",
        "MarketData(open, 2022-03-01, 1个标的)",
        "MarketData(close, 2022-03-01, 1个标的)",
        "Timer(daily/close)",
        "Timer(daily/post_close)",
    ]
    assert loopobj.current_date == day
    assert loopobj.current_phase is SessionPhase.POST_CLOSE


def test_engine_loop_publishes_signal_risk_order_fill():
    loopobj = EngineEventLoop()
    day_ts = DAY
    open_ts = day_ts + pd.Timedelta(hours=9, minutes=30)
    snapshot = make_snapshot([make_bar(day=day_ts)], day=day_ts, phase=SessionPhase.OPEN)
    day = day_ts.date()
    order = make_order()
    fill = Fill("F1", order.order_id, order.symbol, order.side, 100, 10.0, day, ts=open_ts)
    # 开盘阶段：风控复核 → 订单 → 成交
    loopobj.publish_risk_check(snapshot, order)
    loopobj.publish_order(snapshot, order)
    loopobj.publish_fill(snapshot, fill)
    # 盘后阶段：信号
    loopobj.publish_signals(day, [SignalIntent("600000.SH", 1, day)], "test")
    loopobj.drain()

    assert loopobj.recorder.types() == ["RISK_CHECK", "ORDER", "FILL", "SIGNAL"]
    timestamps = [r.timestamp for r in loopobj.recorder.records]
    assert timestamps == sorted(timestamps)
    assert timestamps[0] == open_ts
    assert timestamps[-1].hour == 15 and timestamps[-1].minute == 10
