"""撮合引擎测试：涨跌停、停牌、T+1、整手、部分成交、限价、资金约束。"""

from __future__ import annotations

from datetime import date as _date

from tests.compat import approx
from tests.tools import dates, make_bar, make_config, make_order, make_snapshot

from aqs.core.enums import OrderType, RejectReason, Side, TradingStatus
from aqs.core.models import CostBreakdown, Fill
from aqs.engine.account import Account
from aqs.engine.cost import CostModel
from aqs.engine.matching import MatchingEngine

DAY = dates(3)[0]
D1 = dates(3)[1]
SYM = "600000.SH"


def engine(**engine_over) -> MatchingEngine:
    over = {"engine": engine_over} if engine_over else None
    cfg = make_config(over)
    return MatchingEngine(cfg.engine, cost_model=CostModel(cfg.costs), seed=7)


def account_with_cash(cash: float = 1_000_000.0) -> Account:
    return Account(cash)


def account_with_position(qty: float = 500.0, *, unlock: bool = True) -> Account:
    acc = Account(1_000_000.0)
    acc.apply_fill(
        Fill("F1", "O1", SYM, Side.BUY, qty, 10.0, DAY, gross_amount=qty * 10.0, cost=CostBreakdown())
    )
    if unlock:
        acc.unlock_t1()
    return acc


# --------------------------------------------------------------------------- #
# 正常成交
# --------------------------------------------------------------------------- #
def test_market_buy_fills_at_open_with_slippage():
    eng = engine()
    bar = make_bar(SYM, DAY, close=10.0, open_=10.0, volume=1_000_000.0)
    snap = make_snapshot([bar], day=DAY, adv_volume={SYM: 1_000_000.0})
    order = make_order(SYM, Side.BUY, 1000)
    res = eng.try_match(order, snap, account=account_with_cash())
    assert res.filled is True
    assert res.filled_quantity == 1000
    assert res.price == approx(10.0 * 1.001)
    assert res.reason is RejectReason.NONE
    assert res.can_retry is False
    assert res.status is TradingStatus.NORMAL
    assert res.cost.total > 0


def test_market_sell_fills_at_open_with_slippage():
    eng = engine()
    bar = make_bar(SYM, DAY, close=10.0, open_=10.0)
    snap = make_snapshot([bar], day=DAY)
    order = make_order(SYM, Side.SELL, 500)
    res = eng.try_match(order, snap, account=account_with_position(500))
    assert res.filled_quantity == 500
    assert res.price == approx(10.0 * 0.999)


def test_vwap_execution_price():
    eng = engine(execution_price="vwap")
    bar = make_bar(SYM, DAY, close=10.6, open_=10.4, volume=1_000_000.0, amount=10.5 * 1_000_000.0)
    snap = make_snapshot([bar], day=DAY, adv_volume={SYM: 1_000_000.0})
    res = eng.try_match(make_order(SYM, Side.BUY, 1000), snap, account=account_with_cash())
    assert res.price == approx(10.5 * 1.001)


def test_close_execution_price():
    eng = engine(execution_price="close")
    bar = make_bar(SYM, DAY, close=10.6, open_=10.4)
    snap = make_snapshot([bar], day=DAY)
    res = eng.try_match(make_order(SYM, Side.BUY, 1000), snap, account=account_with_cash())
    assert res.price == approx(10.6 * 1.001)


# --------------------------------------------------------------------------- #
# 停牌 / 退市 / 无行情
# --------------------------------------------------------------------------- #
def test_suspension_blocks_and_defers():
    eng = engine()
    bar = make_bar(SYM, DAY, close=10.0, is_suspended=True)
    snap = make_snapshot([bar], day=DAY)
    res = eng.try_match(make_order(SYM, Side.BUY, 1000), snap, account=account_with_cash())
    assert res.filled is False
    assert res.reason is RejectReason.SUSPENDED
    assert res.can_retry is True
    assert res.status is TradingStatus.SUSPENDED


def test_delisted_is_not_retryable():
    eng = engine()
    bar = make_bar(SYM, D1, close=10.0, delist_date=DAY)
    snap = make_snapshot([bar], day=D1)
    res = eng.try_match(make_order(SYM, Side.BUY, 1000), snap, account=account_with_cash())
    assert res.reason is RejectReason.DELISTED
    assert res.can_retry is False


def test_missing_quote_is_retryable():
    eng = engine()
    snap = make_snapshot([], day=DAY)
    res = eng.try_match(make_order(SYM, Side.BUY, 1000), snap, account=account_with_cash())
    assert res.reason is RejectReason.NO_QUOTE
    assert res.can_retry is True


# --------------------------------------------------------------------------- #
# 涨跌停
# --------------------------------------------------------------------------- #
def test_limit_up_blocks_buy():
    eng = engine()
    bar = make_bar(SYM, DAY, close=11.0, open_=11.0, limit_up=11.0, limit_down=9.0, prev_close=10.0)
    snap = make_snapshot([bar], day=DAY)
    res = eng.try_match(make_order(SYM, Side.BUY, 1000), snap, account=account_with_cash())
    assert res.filled is False
    assert res.reason is RejectReason.LIMIT_UP
    assert res.can_retry is True


def test_limit_up_allows_sell():
    eng = engine()
    bar = make_bar(SYM, DAY, close=11.0, open_=11.0, limit_up=11.0, limit_down=9.0, prev_close=10.0)
    snap = make_snapshot([bar], day=DAY)
    res = eng.try_match(make_order(SYM, Side.SELL, 500), snap, account=account_with_position(500))
    assert res.filled_quantity == 500
    assert res.price == approx(11.0 * 0.999)


def test_limit_down_blocks_sell():
    eng = engine()
    bar = make_bar(SYM, DAY, close=9.0, open_=9.0, limit_up=11.0, limit_down=9.0, prev_close=10.0)
    snap = make_snapshot([bar], day=DAY)
    res = eng.try_match(make_order(SYM, Side.SELL, 500), snap, account=account_with_position(500))
    assert res.filled is False
    assert res.reason is RejectReason.LIMIT_DOWN
    assert res.can_retry is True


def test_limit_down_allows_buy():
    eng = engine()
    bar = make_bar(SYM, DAY, close=9.0, open_=9.0, limit_up=11.0, limit_down=9.0, prev_close=10.0)
    snap = make_snapshot([bar], day=DAY)
    res = eng.try_match(make_order(SYM, Side.BUY, 1000), snap, account=account_with_cash())
    assert res.filled_quantity == 1000
    assert res.price == approx(9.0 * 1.001)


def test_limit_up_probability_override():
    eng = engine(matching={"limit_up_fill_prob": 1.0})
    bar = make_bar(SYM, DAY, close=11.0, open_=11.0, limit_up=11.0, limit_down=9.0, prev_close=10.0)
    snap = make_snapshot([bar], day=DAY)
    res = eng.try_match(make_order(SYM, Side.BUY, 1000), snap, account=account_with_cash())
    assert res.filled_quantity == 1000


def test_suspend_flag_with_volume_still_blocked():
    eng = engine()
    bar = make_bar(SYM, DAY, close=10.0, is_suspended=True, volume=1_000_000.0)
    assert bar.volume == 0.0  # 构造器把停牌日的成交量归零
    snap = make_snapshot([bar], day=DAY)
    assert eng.try_match(make_order(SYM, Side.BUY, 100), snap).reason is RejectReason.SUSPENDED


# --------------------------------------------------------------------------- #
# T+1 与整手
# --------------------------------------------------------------------------- #
def test_t_plus_one_blocks_same_day_sell():
    eng = engine()
    bar = make_bar(SYM, DAY, close=10.0)
    snap = make_snapshot([bar], day=DAY)
    acc = account_with_position(500, unlock=False)  # 当日买入，未解锁
    res = eng.try_match(make_order(SYM, Side.SELL, 500), snap, account=acc)
    assert res.filled is False
    assert res.reason is RejectReason.T1_LOCK
    assert res.can_retry is True


def test_sell_without_position_is_terminal():
    eng = engine()
    bar = make_bar(SYM, DAY, close=10.0)
    snap = make_snapshot([bar], day=DAY)
    res = eng.try_match(make_order(SYM, Side.SELL, 100), snap, account=Account(1_000_000.0))
    assert res.reason is RejectReason.INSUFFICIENT_POSITION
    assert res.can_retry is False


def test_sell_reduces_to_available_quantity():
    eng = engine()
    bar = make_bar(SYM, DAY, close=10.0, volume=1_000_000.0)
    snap = make_snapshot([bar], day=DAY)
    res = eng.try_match(make_order(SYM, Side.SELL, 1000), snap, account=account_with_position(300))
    assert res.filled_quantity == 300
    assert res.partial is True


def test_odd_lot_sell_is_allowed():
    eng = engine()
    bar = make_bar(SYM, DAY, close=10.0)
    snap = make_snapshot([bar], day=DAY)
    res = eng.try_match(make_order(SYM, Side.SELL, 150), snap, account=account_with_position(150))
    assert res.filled_quantity == 150


def test_buy_quantity_floored_to_lot():
    eng = engine()
    bar = make_bar(SYM, DAY, close=10.0)
    snap = make_snapshot([bar], day=DAY)
    res = eng.try_match(make_order(SYM, Side.BUY, 250), snap, account=account_with_cash())
    assert res.filled_quantity == 200


def test_buy_below_one_lot_rejected():
    eng = engine()
    bar = make_bar(SYM, DAY, close=10.0)
    snap = make_snapshot([bar], day=DAY)
    res = eng.try_match(make_order(SYM, Side.BUY, 50), snap, account=account_with_cash())
    assert res.reason is RejectReason.LOT_SIZE
    assert res.can_retry is False


# --------------------------------------------------------------------------- #
# 资金约束
# --------------------------------------------------------------------------- #
def test_insufficient_cash_rejects_small_account():
    eng = engine()
    bar = make_bar(SYM, DAY, close=10.0)
    snap = make_snapshot([bar], day=DAY)
    res = eng.try_match(make_order(SYM, Side.BUY, 1000), snap, account=Account(500.0))
    assert res.reason is RejectReason.INSUFFICIENT_CASH
    assert res.can_retry is True


def test_cash_constraint_reduces_quantity():
    eng = engine()
    bar = make_bar(SYM, DAY, close=10.0)
    snap = make_snapshot([bar], day=DAY)
    res = eng.try_match(make_order(SYM, Side.BUY, 1000), snap, account=Account(2000.0))
    assert res.filled_quantity == 100
    assert res.partial is True


def test_affordable_quantity_helper():
    eng = engine()
    assert eng.affordable_quantity(10_000.0, 10.0) == 900  # 10.01×100 = 1001 元/手
    assert eng.affordable_quantity(0.0, 10.0) == 0
    assert eng.affordable_quantity(1000.0, 10.0) == 0


def test_affordable_quantity_includes_impact_cost():
    """回归用例：可买数量必须把冲击成本算进去，否则撮合时会现金透支。"""
    eng = engine()
    cash = 1_000_000.0
    adv = 1_000_000.0
    qty = eng.affordable_quantity(cash, 10.0, adv_volume=adv, trade_date=DAY)
    assert qty > 0

    def total(q: float) -> float:
        r = eng.cost_model.compute(
            side=Side.BUY, quantity=q, reference_price=10.0, trade_date=DAY, adv_volume=adv
        )
        return r.gross_amount + r.total_cost

    assert total(qty) <= cash + 1e-9            # 恰好买得起
    assert total(qty + 100) > cash              # 再加一手就买不起


def test_affordable_quantity_respects_buffer():
    eng = engine()
    full = eng.affordable_quantity(1_000_000.0, 10.0, adv_volume=1_000_000.0, trade_date=DAY)
    buffered = eng.affordable_quantity(
        1_000_000.0, 10.0, adv_volume=1_000_000.0, trade_date=DAY, buffer=0.05
    )
    assert buffered < full


# --------------------------------------------------------------------------- #
# 参与率与部分成交
# --------------------------------------------------------------------------- #
def test_participation_cap_creates_partial_fill():
    eng = engine()
    bar = make_bar(SYM, DAY, close=10.0, volume=10_000.0)
    snap = make_snapshot([bar], day=DAY)
    res = eng.try_match(make_order(SYM, Side.BUY, 5000), snap, account=account_with_cash())
    assert res.filled_quantity == 1000  # 10_000 × 10% 向下取整到整手
    assert res.partial is True
    assert res.can_retry is True
    assert res.queue_remaining == 4000


def test_no_partial_fill_when_disabled():
    eng = engine(matching={"allow_partial_fill": False})
    bar = make_bar(SYM, DAY, close=10.0, volume=10_000.0)
    snap = make_snapshot([bar], day=DAY)
    res = eng.try_match(make_order(SYM, Side.BUY, 5000), snap, account=account_with_cash())
    assert res.filled is False
    assert res.reason is RejectReason.INSUFFICIENT_LIQUIDITY
    assert res.can_retry is True


def test_liquidity_cap_zero_when_no_volume():
    eng = engine()
    bar = make_bar(SYM, DAY, close=10.0, volume=50.0)
    assert eng.liquidity_cap(bar) == 0.0


def test_floor_lot_helper():
    eng = engine()
    assert eng.floor_lot(1234) == 1200
    assert eng.floor_lot(99) == 0
    assert eng.floor_lot(0) == 0


# --------------------------------------------------------------------------- #
# 限价单
# --------------------------------------------------------------------------- #
def test_limit_buy_not_touched_when_low_above_limit():
    eng = engine()
    bar = make_bar(SYM, DAY, close=10.0, open_=10.0, low=9.8, high=10.2)
    snap = make_snapshot([bar], day=DAY)
    order = make_order(SYM, Side.BUY, 1000, order_type=OrderType.LIMIT, limit_price=9.5)
    res = eng.try_match(order, snap, account=account_with_cash())
    assert res.reason is RejectReason.PRICE_OUT_OF_RANGE
    assert res.can_retry is True


def test_limit_buy_fills_at_limit_when_below_open():
    eng = engine()
    bar = make_bar(SYM, DAY, close=10.0, open_=10.0, low=9.4, high=10.2)
    snap = make_snapshot([bar], day=DAY)
    order = make_order(SYM, Side.BUY, 1000, order_type=OrderType.LIMIT, limit_price=9.5)
    res = eng.try_match(order, snap, account=account_with_cash())
    assert res.filled_quantity == 1000
    assert res.price == approx(9.5 * 1.001)


def test_limit_sell_not_touched_when_high_below_limit():
    eng = engine()
    bar = make_bar(SYM, DAY, close=10.0, open_=10.0, low=9.8, high=10.2)
    snap = make_snapshot([bar], day=DAY)
    order = make_order(SYM, Side.SELL, 500, order_type=OrderType.LIMIT, limit_price=10.5)
    res = eng.try_match(order, snap, account=account_with_position(500))
    assert res.reason is RejectReason.PRICE_OUT_OF_RANGE


# --------------------------------------------------------------------------- #
# 统计与描述
# --------------------------------------------------------------------------- #
def test_match_stats_counted():
    eng = engine()
    snaps = {
        "ok": make_snapshot([make_bar(SYM, DAY, close=10.0)], day=DAY),
        "susp": make_snapshot([make_bar(SYM, DAY, close=10.0, is_suspended=True)], day=DAY),
    }
    eng.try_match(make_order(SYM, Side.BUY, 1000), snaps["ok"], account=account_with_cash())
    eng.try_match(make_order(SYM, Side.BUY, 1000), snaps["susp"], account=account_with_cash())
    assert eng.stats["filled"] == 1
    assert eng.stats[RejectReason.SUSPENDED.value] == 1


def test_describe_reports_config():
    info = engine().describe()
    assert info["lot_size"] == 100
    assert info["execution_price"] == "open"
    assert info["max_participation"] == approx(0.10)
    assert info["price_limit"]["main"] == approx(0.10)


def test_ensure_positive_lot():
    from aqs.core.exceptions import EngineError
    from aqs.engine.matching import ensure_positive_lot

    assert ensure_positive_lot(200) == 200
    try:
        ensure_positive_lot(150)
    except EngineError:
        pass
    else:  # pragma: no cover
        raise AssertionError("非整手数量应当报错")
