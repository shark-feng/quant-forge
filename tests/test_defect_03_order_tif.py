"""回归缺陷 #3：DAY 订单未成交被误标记为 REJECTED。

原缺陷：``Broker._handle_result`` 的兜底分支把「可顺延但只支持 DAY」的订单
当作拒单（``mark_rejected`` + ``rejected += 1``），语义错误且污染拒单率。

修复后口径：
- ``TimeInForce.DAY``：仅在其 ``submit_date`` 当日有效；当日未完成 → **EXPIRED**（不计入拒单）；
- ``TimeInForce.GTC``：顺延；``deferred_days > max_defer_days`` → EXPIRED；
- 硬约束（退市/不足一手/无持仓/限价未触及…）→ REJECTED（不计入过期）。
"""

from __future__ import annotations

from tests.compat import approx
from tests.tools import DEFAULT_START, dates, make_bar, make_config, make_snapshot

from aqs.core.enums import OrderStatus, RejectReason, Side, TimeInForce
from aqs.engine.account import Account
from aqs.engine.broker import Broker
from aqs.engine.cost import CostModel

D = dates(5)
SYM = "600000.SH"


def make_broker(**engine_over) -> Broker:
    cfg = make_config({"engine": engine_over} if engine_over else None)
    return Broker(
        Account(1_000_000.0),
        engine_config=cfg.engine,
        cost_model=CostModel(cfg.costs),
        seed=7,
    )


def submit_day_order(broker: Broker, *, tif: TimeInForce, quantity: float = 1000, submit_idx: int = 1):
    order = broker.create_order(
        SYM,
        Side.BUY,
        quantity,
        signal_date=D[submit_idx - 1],
        submit_date=D[submit_idx],
        time_in_force=tif,
    )
    broker.submit(order)
    return order


def limit_up_snapshot(idx: int):
    bar = make_bar(SYM, D[idx], close=11.0, open_=11.0, limit_up=11.0, limit_down=9.0, prev_close=10.0)
    return make_snapshot([bar], day=D[idx])


def normal_snapshot(idx: int, *, volume: float = 1_000_000.0):
    bar = make_bar(SYM, D[idx], close=10.0, open_=10.0, volume=volume)
    return make_snapshot([bar], day=D[idx])


# --------------------------------------------------------------------------- #
# DAY 语义
# --------------------------------------------------------------------------- #
def test_day_order_blocked_by_limit_up_expires_today():
    broker = make_broker()
    order = submit_day_order(broker, tif=TimeInForce.DAY)
    broker.process_open(limit_up_snapshot(1))

    assert order.status is OrderStatus.EXPIRED
    assert broker.stats.expired == 1
    assert broker.stats.expired_day == 1
    assert broker.stats.rejected == 0, "DAY 订单过期不应计入拒单"
    assert order.reject_reason is RejectReason.LIMIT_UP  # 原因仍被记录
    assert broker.working == []


def test_day_order_blocked_by_suspension_expires_today():
    broker = make_broker()
    order = submit_day_order(broker, tif=TimeInForce.DAY)
    broker.process_open(make_snapshot([make_bar(SYM, D[1], close=10.0, is_suspended=True)], day=D[1]))
    assert order.status is OrderStatus.EXPIRED
    assert broker.stats.expired_day == 1
    assert broker.stats.rejected == 0


def test_day_order_partial_fill_then_expires_next_session():
    """当日部分成交，次日仍未完成 → 剩余部分过期，已成交部分保留。"""
    broker = make_broker()
    order = submit_day_order(broker, tif=TimeInForce.DAY, quantity=1000)
    fills = broker.process_open(normal_snapshot(1, volume=5_000.0))  # 参与率 10% → 上限 500 股
    assert len(fills) == 1
    assert fills[0].quantity == 500
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.filled_quantity == 500

    broker.process_open(normal_snapshot(2))
    assert order.status is OrderStatus.EXPIRED
    assert order.filled_quantity == 500          # 已成交部分保留
    assert broker.stats.expired_day == 1
    assert broker.stats.rejected == 0


def test_day_order_filled_fully_is_not_expired():
    broker = make_broker()
    order = submit_day_order(broker, tif=TimeInForce.DAY, quantity=1000)
    broker.process_open(normal_snapshot(1))
    assert order.status is OrderStatus.FILLED
    assert broker.stats.expired == 0
    assert broker.stats.rejected == 0


def test_day_order_cross_session_on_suspension_expires():
    """当日停牌（可顺延），但 DAY 语义不允许顺延 → 过期。"""
    broker = make_broker()
    order = submit_day_order(broker, tif=TimeInForce.DAY)
    broker.process_open(make_snapshot([make_bar(SYM, D[1], close=10.0, is_suspended=True)], day=D[1]))
    assert order.status is OrderStatus.EXPIRED
    broker.process_open(normal_snapshot(2))
    assert len(broker.fills) == 0, "DAY 订单不应在次日继续撮合"


# --------------------------------------------------------------------------- #
# GTC 语义（保持原行为）
# --------------------------------------------------------------------------- #
def test_gtc_order_defers_instead_of_expiring():
    broker = make_broker()
    order = submit_day_order(broker, tif=TimeInForce.GTC)
    broker.process_open(limit_up_snapshot(1))
    assert order.status is OrderStatus.SUBMITTED
    assert order.deferred_days == 1
    assert broker.stats.expired == 0
    assert broker.stats.rejected == 0
    assert broker.stats.deferred_events == 1


def test_gtc_order_expires_after_max_defer_days():
    broker = make_broker(max_defer_days=1)
    order = submit_day_order(broker, tif=TimeInForce.GTC)
    broker.process_open(limit_up_snapshot(1))
    assert order.status is OrderStatus.SUBMITTED
    broker.process_open(limit_up_snapshot(2))
    assert order.status is OrderStatus.EXPIRED
    assert broker.stats.expired == 1
    assert broker.stats.expired_day == 0, "GTC 顺延超限不计入 DAY 过期"
    assert broker.stats.rejected == 0


def test_gtc_partial_fill_keeps_working():
    broker = make_broker()
    order = submit_day_order(broker, tif=TimeInForce.GTC, quantity=1000)
    broker.process_open(normal_snapshot(1, volume=5_000.0))
    assert order.status is OrderStatus.PARTIALLY_FILLED
    broker.process_open(normal_snapshot(2, volume=5_000.0))
    assert order.status is OrderStatus.FILLED
    assert broker.stats.expired == 0


# --------------------------------------------------------------------------- #
# 硬拒绝仍为 REJECTED
# --------------------------------------------------------------------------- #
def test_hard_reject_is_still_rejected():
    broker = make_broker()
    order = broker.create_order(SYM, Side.BUY, 50, signal_date=D[0], submit_date=D[1])  # 不足一手
    broker.submit(order)
    broker.process_open(normal_snapshot(1))
    assert order.status is OrderStatus.REJECTED
    assert broker.stats.rejected == 1
    assert broker.stats.expired == 0
    assert order.reject_reason is RejectReason.LOT_SIZE


def test_delisted_symbol_rejected_not_expired():
    broker = make_broker()
    order = submit_day_order(broker, tif=TimeInForce.DAY)
    bar = make_bar(SYM, D[1], close=10.0, delist_date=D[0])
    broker.process_open(make_snapshot([bar], day=D[1]))
    assert order.status is OrderStatus.REJECTED
    assert broker.stats.rejected == 1
    assert broker.stats.expired == 0


# --------------------------------------------------------------------------- #
# 统计口径
# --------------------------------------------------------------------------- #
def test_stats_expose_both_expiry_kinds():
    broker = make_broker(max_defer_days=0)
    day_order = submit_day_order(broker, tif=TimeInForce.DAY)
    gtc_order = broker.create_order(
        SYM, Side.BUY, 1000, signal_date=D[0], submit_date=D[1], time_in_force=TimeInForce.GTC
    )
    broker.submit(gtc_order)
    broker.process_open(limit_up_snapshot(1))

    assert day_order.status is OrderStatus.EXPIRED
    assert gtc_order.status is OrderStatus.EXPIRED  # max_defer_days=0 → 立即过期
    stats = broker.stats
    assert stats.expired == 2
    assert stats.expired_day == 1
    assert stats.rejected == 0
    payload = stats.as_dict()
    assert "expired_day" in payload and payload["expired"] == 2


def test_audit_records_expiry_reason():
    broker = make_broker()
    submit_day_order(broker, tif=TimeInForce.DAY)
    broker.process_open(limit_up_snapshot(1))
    expired = [r for r in broker.audit.records if r.action == "expired"]
    assert expired
    assert "day_expired" in str(expired[-1].payload.get("reason", ""))
