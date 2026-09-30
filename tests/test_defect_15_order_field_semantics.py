"""回归缺陷 #15：orders.csv 的 `reject_reason` 语义易被误读。

原缺陷（D3 诊断）：``Order.defer()`` 会把**顺延原因**写入 ``reject_reason``，
而成交/完成后不清理；``to_dict()`` 又把它导出为字段名 ``reject_reason``。
于是出现 ``status=filled`` + ``reject_reason=suspended`` 这类记录（落盘实测：
ma_cross O0000078、breakout O0000024、volume O0000329），
读者会误以为「订单最终被拒单，原因是停牌」。

修复后口径：

- ``final_status``：终态/当前态（推荐字段）；
- ``last_reject_reason``：**最后一次「未被接受」的原因**，可能是历史原因；
- ``deferred_reasons``：顺延原因序列（完整可追溯）；
- ``rejected_on``：进入终态拒单/过期/撤单的日期；
- ``rejected_before_final``：最终成交但过程曾被顺延（此时 ``last_reject_reason`` 只代表历史）；
- ``status`` / ``reject_reason``：过渡期别名，取值与上面两个字段相同。
"""

from __future__ import annotations

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


def submit(broker: Broker, *, quantity: float = 1000, submit_idx: int = 1, tif=TimeInForce.GTC):
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


def suspended_snapshot(idx: int):
    return make_snapshot([make_bar(SYM, D[idx], close=10.0, is_suspended=True)], day=D[idx])


def tradable_snapshot(idx: int):
    return make_snapshot(
        [make_bar(SYM, D[idx], close=10.0, open_=10.0, volume=1_000_000.0)],
        day=D[idx],
        adv_volume={SYM: 1_000_000.0},
    )


# --------------------------------------------------------------------------- #
# 核心场景：顺延后成交
# --------------------------------------------------------------------------- #
def test_deferred_then_filled_is_marked_as_rejected_before_final():
    """顺延（停牌）一天后成交：final_status=filled，但 last_reject_reason 是历史原因。"""
    broker = make_broker()
    order = submit(broker, quantity=1000)
    broker.process_open(suspended_snapshot(1))
    broker.process_open(tradable_snapshot(2))

    assert order.status is OrderStatus.FILLED
    assert order.reject_reason is RejectReason.SUSPENDED
    assert order.rejected_before_final is True
    assert order.deferred_reasons == [RejectReason.SUSPENDED]
    assert order.deferred_days == 1
    assert order.rejected_on is None, "订单最终成交，未进入拒单/过期终态"


def test_export_exposes_self_explaining_fields():
    """to_dict 必须同时给出 final_status / last_reject_reason 与过渡期别名。"""
    broker = make_broker()
    order = submit(broker, quantity=1000)
    broker.process_open(suspended_snapshot(1))
    broker.process_open(tradable_snapshot(2))

    row = order.to_dict()
    assert row["final_status"] == "filled"
    assert row["last_reject_reason"] == "suspended"
    assert row["deferred_reasons"] == "suspended"
    assert row["rejected_on"] is None
    assert row["rejected_before_final"] is True
    # 过渡期别名：取值必须与推荐字段完全一致（不允许两套口径漂移）
    assert row["status"] == row["final_status"]
    assert row["reject_reason"] == row["last_reject_reason"]


# --------------------------------------------------------------------------- #
# 顺延超期
# --------------------------------------------------------------------------- #
def test_deferred_until_expiry_records_reason_and_date():
    """连续顺延超过 max_defer_days → EXPIRED，并记录原因序列与过期日期。"""
    broker = make_broker(max_defer_days=2)
    order = submit(broker, quantity=1000)
    for idx in (1, 2, 3):
        broker.process_open(suspended_snapshot(idx))

    assert order.status is OrderStatus.EXPIRED
    assert order.deferred_days == 3
    assert order.deferred_reasons == [RejectReason.SUSPENDED] * 3
    assert order.rejected_on == D[3], "过期日期应为最后触发的那一天"
    assert order.rejected_before_final is False
    row = order.to_dict()
    assert row["final_status"] == "expired"
    assert row["rejected_on"] == str(D[3])


# --------------------------------------------------------------------------- #
# 真硬拒单
# --------------------------------------------------------------------------- #
def test_hard_reject_records_date_and_stays_terminal():
    """真硬拒单（不足一手）→ REJECTED，rejected_on 有值，deferred_reasons 为空。"""
    broker = make_broker()
    order = submit(broker, quantity=50)  # 不足一手
    broker.process_open(tradable_snapshot(1))

    assert order.status is OrderStatus.REJECTED
    assert order.reject_reason is RejectReason.LOT_SIZE
    assert order.rejected_on == D[1]
    assert order.deferred_reasons == []
    assert order.rejected_before_final is False
    row = order.to_dict()
    assert row["final_status"] == "rejected"
    assert row["last_reject_reason"] == "lot_size"


def test_cancel_records_date():
    broker = make_broker()
    order = submit(broker, quantity=1000)
    broker.cancel(order, RejectReason.NONE, day=D[1])
    assert order.status is OrderStatus.CANCELLED
    assert order.rejected_on == D[1]
    assert order.to_dict()["final_status"] == "cancelled"


# --------------------------------------------------------------------------- #
# 别名不漂移 + 默认值
# --------------------------------------------------------------------------- #
def test_brand_new_order_has_empty_history():
    """新建订单：无顺延历史、无终态日期，别名与推荐字段一致。"""
    from tests.tools import make_order

    order = make_order()
    row = order.to_dict()
    assert row["deferred_reasons"] == ""
    assert row["rejected_on"] is None
    assert row["rejected_before_final"] is False
    assert row["final_status"] == row["status"]
    assert row["last_reject_reason"] == row["reject_reason"] == "none"


def test_order_frame_contains_recommended_and_alias_columns():
    """orders_frame（即 orders.csv）必须同时包含推荐字段与别名列。"""
    broker = make_broker()
    order = submit(broker, quantity=1000)
    broker.process_open(suspended_snapshot(1))
    broker.process_open(tradable_snapshot(2))
    frame = broker.orders_frame()
    for column in (
        "final_status",
        "last_reject_reason",
        "deferred_reasons",
        "rejected_on",
        "rejected_before_final",
        "status",
        "reject_reason",
    ):
        assert column in frame.columns, f"orders.csv 缺少列：{column}"
    assert frame.loc[0, "final_status"] == "filled"
    assert frame.loc[0, "last_reject_reason"] == "suspended"
    assert order.order_id == frame.loc[0, "order_id"]


def test_default_start_constant_is_used():
    """哨兵：确认订单日期使用了测试夹具的默认起始日（避免夹具漂移）。"""
    assert str(D[0]) == DEFAULT_START
