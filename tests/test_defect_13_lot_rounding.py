"""回归缺陷 #13：RMS 削减后订单数量未整手取整（四层根因）。

诊断见 `docs/17_round3_diagnostics.md` §2~§3。四个独立缺陷：

| 编号 | 缺陷 | 后果 |
| --- | --- | --- |
| D2-a | `RiskDecision.reduce` 返回小数股，两个消费点都只做 `min()` 不取整 | 小数股订单 |
| D2-b | 部分成交后「剩余 < 1 手」被当成硬拒单 | 订单已成交却标 `rejected`；拒单率虚增（实测占 80%） |
| D2-c | `control._round_down` 的零股分支返回小数股 | 小数股卖出 → 小数持仓 → 逐日传染 |
| D2-d | 撮合前削减忽略 `filled_quantity` | `filled > quantity` 自相矛盾订单 + 只顺延的僵尸订单 |

本模块覆盖的不变量：

- INV-1：BUY 订单 `quantity` 是 `lot_size` 的整数倍
- INV-2：SELL 订单 `quantity` 是**整数股**
- INV-3：`filled_quantity` 是整数股
- INV-4：持仓 `total_quantity` 是整数股
- INV-5：部分成交后剩余不足一手 → EXPIRED，**不增加** `stats.rejected`
- INV-6：`filled_quantity <= quantity` 恒成立
"""

from __future__ import annotations

from tests.compat import approx, raises
from tests.tools import (
    DEFAULT_START,
    dates,
    entry_signal,
    make_bar,
    make_config,
    make_snapshot,
    make_store,
    flat_market,
)

from aqs.config.loader import load_base_config
from aqs.core.enums import OrderStatus, RejectReason, RiskAction, Side, TimeInForce
from aqs.core.exceptions import EngineError
from aqs.core.models import Order, RiskDecision
from aqs.core.quantity import floor_lot, sell_quantity, whole_shares
from aqs.data.store import DataStore
from aqs.data.synthetic import generate_market_data
from aqs.engine.account import Account
from aqs.engine.backtest import BacktestEngine
from aqs.engine.broker import Broker
from aqs.engine.cost import CostModel
from aqs.engine.matching import MatchingEngine
from aqs.portfolio.registry import build_portfolio
from aqs.risk.base import RiskContext, RiskRule
from aqs.risk.engine import RuleRiskEngine
from aqs.strategy.ma_cross import MACrossStrategy

D = dates(6)
SYM = "600000.SH"
LOT = 100


def make_broker(**engine_over) -> Broker:
    """构造经纪商；默认把参与率上限放开到 100%，让「零头」只来自数量本身。"""
    engine: dict = {"matching": {"max_participation": 1.0}}
    engine.update(engine_over)
    cfg = make_config({"engine": engine})
    return Broker(
        Account(1_000_000.0),
        engine_config=cfg.engine,
        cost_model=CostModel(cfg.costs),
        seed=7,
    )


def tradable(idx: int, *, volume: float = 1_000_000.0):
    bar = make_bar(SYM, D[idx], close=10.0, open_=10.0, volume=volume)
    return make_snapshot([bar], day=D[idx], adv_volume={SYM: 1_000_000.0})


def brokerless_order(*, quantity: float, filled: float) -> Order:
    """构造「已部分成交」的订单（不经过 Broker，用于撮合层单元测试）。"""
    order = Order(order_id="R1", symbol=SYM, side=Side.BUY, quantity=quantity)
    order.filled_quantity = filled
    order.status = OrderStatus.PARTIALLY_FILLED
    order.submit_date = D[1]
    return order


def add_position(account: Account, symbol: str, quantity: float, price: float) -> None:
    """给账户加一笔整数股持仓（可卖量已解锁）。"""
    from aqs.core.models import CostBreakdown, Fill

    account.apply_fill(
        Fill(
            fill_id="F-test",
            order_id="O-test",
            symbol=symbol,
            side=Side.BUY,
            quantity=quantity,
            price=price,
            trade_date=D[0],
            gross_amount=quantity * price,
            cost=CostBreakdown(),
        )
    )
    account.unlock_t1()


class ProbeRule(RiskRule):
    """仅用于测试 `normalize_quantity` 的具体规则（不做任何拦截）。"""

    name = "probe"

    def check(self, ctx: RiskContext) -> RiskDecision:
        return self.allow()


# --------------------------------------------------------------------------- #
# 0. 纯函数：数量归一化
# --------------------------------------------------------------------------- #
def test_floor_lot_helper():
    assert floor_lot(6819.28, LOT) == 6800
    assert floor_lot(99.9, LOT) == 0
    assert floor_lot(100.0, LOT) == 100
    assert floor_lot(-5, LOT) == 0
    assert floor_lot(6819.28, 0) == 6819  # 无整手约束 → 整数股
    assert floor_lot(200.0, 200) == 200


def test_sell_quantity_helper_never_fractional():
    assert sell_quantity(250, LOT) == 200
    assert sell_quantity(96.47, LOT) == 96  # 零股：整数股，不是小数
    assert sell_quantity(0.4, LOT) == 0
    assert sell_quantity(1000, LOT) == 1000
    assert sell_quantity(96.47, 0) == 96
    assert whole_shares(40.3) == 40
    assert whole_shares(0.9) == 0


# --------------------------------------------------------------------------- #
# 1. D2-a：规则层削减后一律是整手（BUY）
# --------------------------------------------------------------------------- #
def test_rule_level_reduce_is_lot_aligned():
    """`normalize_quantity` 必须把小数削减量压成整手（BUY）。"""
    from tests.tools import make_order, make_risk_context

    rule = ProbeRule()
    ctx = make_risk_context(order=make_order(quantity=5500.0))
    ctx.lot_size = LOT
    assert rule.normalize_quantity(ctx, 5481.49327628054) == 5400
    assert rule.normalize_quantity(ctx, 60.89) == 0.0
    assert rule.normalize_quantity(ctx, 5500.0) == 5500


def test_rule_level_reduce_for_sell_is_whole_shares():
    from tests.tools import make_order, make_risk_context

    rule = ProbeRule()
    order = make_order(side=Side.SELL, quantity=96.47)
    ctx = make_risk_context(order=order)
    ctx.lot_size = LOT
    assert rule.normalize_quantity(ctx, 96.4703158766017) == 96
    assert rule.normalize_quantity(ctx, 1828.8281300924023) == 1828


def test_engine_normalizes_reduce_amount():
    """`RuleRiskEngine` 必须归一化任何规则返回的削减量（唯一收口处）。"""
    from tests.tools import make_risk_context

    class SloppyRule(RiskRule):
        name = "sloppy"

        def check(self, ctx: RiskContext) -> RiskDecision:
            return self.reduce(5481.49327628054, "削减到小数")

    engine = RuleRiskEngine(config={"enabled": True, "rules": []}, rules=[SloppyRule()])
    engine.set_lot_size(LOT)
    order = Order(order_id="X1", symbol=SYM, side=Side.BUY, quantity=5500.0)
    snap = tradable(1)
    decision = engine.check_order(order, Account(1_000_000.0), snap)

    assert decision.action is RiskAction.REDUCE
    assert decision.modified_quantity == approx(5400.0)
    assert decision.modified_quantity % LOT == 0


def test_engine_marks_below_lot_as_below_lot_not_reject():
    """削减到不足一手 → `below_lot` 语义，且**不计入** stats.rejected。"""
    class TinyRule(RiskRule):
        name = "tiny"

        def check(self, ctx: RiskContext) -> RiskDecision:
            return self.reduce(60.89436489994169, "削减到不足一手")

    engine = RuleRiskEngine(config={"enabled": True, "rules": []}, rules=[TinyRule()])
    engine.set_lot_size(LOT)
    order = Order(order_id="X2", symbol=SYM, side=Side.BUY, quantity=5500.0)
    decision = engine.check_order(order, Account(1_000_000.0), tradable(1))

    assert decision.action is RiskAction.REJECT
    assert decision.reject_reason is RejectReason.BELOW_LOT
    assert engine.stats.below_lot == 1
    assert engine.stats.rejected == 0, "削减到不足一手不是风险拒单，不得计入拒单率"


def test_engine_respects_config_lot_size():
    """整手股数来自配置，不是硬编码 100。"""
    from aqs.core.enums import RiskAction as RA

    class SloppyRule(RiskRule):
        name = "sloppy"

        def check(self, ctx: RiskContext) -> RiskDecision:
            return self.reduce(1050.0, "削减")

    engine = RuleRiskEngine(config={"enabled": True, "rules": []}, rules=[SloppyRule()])
    engine.set_lot_size(200)
    order = Order(order_id="X3", symbol=SYM, side=Side.BUY, quantity=1200.0)
    decision = engine.check_order(order, Account(1_000_000.0), tradable(1))
    assert decision.action is RA.REDUCE
    assert decision.modified_quantity == approx(1000.0), "200 股一手 → 1050 应削到 1000"


# --------------------------------------------------------------------------- #
# 2. D2-b：部分成交后剩余不足一手 → EXPIRED，不增加拒单
# --------------------------------------------------------------------------- #
def test_residue_after_partial_fill_expires_not_rejected():
    broker = make_broker()
    # 订单 1050 股：首日因参与率上限只成交 1000 股，剩 50 股（不足一手）
    order = broker.create_order(SYM, Side.BUY, 1050.0, signal_date=D[0], submit_date=D[1])
    broker.submit(order)

    broker.process_open(tradable(1, volume=2000.0))  # 参与率上限 → 部分成交
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.filled_quantity == approx(1000.0)
    assert order.remaining == approx(50.0)

    broker.process_open(tradable(2))
    assert order.status is OrderStatus.EXPIRED, "剩余不足一手应过期，而不是被拒单"
    assert broker.stats.rejected == 0, "剩余不足一手不得计入拒单数"
    assert broker.stats.expired_residue == 1
    assert broker.working == []


def test_residue_expiry_is_audited():
    broker = make_broker()
    order = broker.create_order(SYM, Side.BUY, 1050.0, signal_date=D[0], submit_date=D[1])
    broker.submit(order)
    broker.process_open(tradable(1, volume=2000.0))
    broker.process_open(tradable(2))

    events = [r for r in broker.audit.records if r.payload.get("order_id") == order.order_id]
    assert any(
        r.action == "expired" and r.payload.get("reason") == "lot_size_residue" for r in events
    ), "剩余不足一手的过期必须有可审计原因"
    assert order.reject_reason is RejectReason.LOT_SIZE_RESIDUE


def test_true_hard_reject_still_counts():
    """真硬拒绝仍然算拒单（不能因为修复而放过真正的错误）。"""
    broker = make_broker()
    order = broker.create_order(SYM, Side.BUY, 50.0, signal_date=D[0], submit_date=D[1])
    broker.submit(order)
    broker.process_open(tradable(1))
    assert order.status is OrderStatus.REJECTED
    assert broker.stats.rejected == 1


# --------------------------------------------------------------------------- #
# 2b. DROPPED 必须是终态（Q1 澄清项）
# --------------------------------------------------------------------------- #
def test_dropped_is_terminal():
    """`OrderStatus.DROPPED` 必须在 `_TERMINAL_STATUSES` 中。

    若它被当成「非终态」，被丢弃的订单会一直留在 `broker.working` 里，
    并在每个交易日重复参与撮合 —— 正是缺陷 #13 想消除的行为。
    """
    assert OrderStatus.DROPPED.is_terminal is True
    assert OrderStatus.DROPPED.is_active is False


def test_dropped_order_not_resubmittable():
    """已 drop 的订单不得再次提交（`Broker.submit` 必须抛 EngineError）。"""
    broker = make_broker()
    order = broker.create_order(SYM, Side.BUY, 1000.0, signal_date=D[0], submit_date=D[1])
    order.drop(RejectReason.BELOW_LOT, day=D[1])

    assert order.status is OrderStatus.DROPPED
    with raises(EngineError):
        broker.submit(order)
    # 未进入撮合队列，也没有成交
    assert order not in broker.working
    assert broker.stats.submitted == 0


def test_dropped_order_is_not_counted_as_rejected():
    """DROPPED 不计入拒单（口径：拒单率只看 rejected / risk_rejected）。"""
    broker = make_broker()
    order = broker.create_order(SYM, Side.BUY, 1000.0, signal_date=D[0], submit_date=D[1])
    order.drop(RejectReason.BELOW_LOT, day=D[1])
    assert broker.stats.rejected == 0
    row = order.to_dict()
    assert row["final_status"] == "dropped"
    assert row["last_reject_reason"] == "below_lot"


# --------------------------------------------------------------------------- #
# 3. D2-d：削减不得让 filled > quantity；剩余为 0 不再顺延
# --------------------------------------------------------------------------- #
def test_reduce_cannot_push_quantity_below_filled():
    """INV-6：撮合前削减后 `quantity >= filled_quantity`。"""
    broker = make_broker()
    order = broker.create_order(SYM, Side.BUY, 10_500.0, signal_date=D[0], submit_date=D[1])
    broker.submit(order)
    broker.process_open(tradable(1, volume=100_000_000.0))  # 全部成交
    assert order.status is OrderStatus.FILLED

    # 人为模拟「已成交后风控又把总量压到很小」的旧缺陷路径
    broker._apply_reduce(order, 51.42)
    assert order.quantity >= order.filled_quantity, "INV-6 被破坏：filled 大于 quantity"
    assert order.remaining == approx(0.0)


def test_zero_remaining_order_is_settled_not_deferred():
    """剩余为 0 → 立即结算，不再每交易日 defer（消除僵尸订单）。"""
    broker = make_broker()
    order = broker.create_order(SYM, Side.BUY, 1000.0, signal_date=D[0], submit_date=D[1])
    broker.submit(order)
    broker._apply_reduce(order, 0.0)  # 风控把剩余量削减为 0
    assert order.remaining == approx(0.0)

    broker.process_open(tradable(1))
    assert order.status is OrderStatus.EXPIRED
    assert order.deferred_days == 0, "剩余为 0 不应产生顺延"
    assert broker.stats.expired_no_remaining == 1
    assert broker.stats.rejected == 0
    assert broker.working == []


def test_broker_reduce_is_lot_aligned():
    """broker 侧削减同样必须整手（第二道防御）。"""
    broker = make_broker()
    order = broker.create_order(SYM, Side.BUY, 5500.0, signal_date=D[0], submit_date=D[1])
    broker._apply_reduce(order, 5481.49327628054)
    assert order.quantity % LOT == 0
    assert order.quantity == approx(5400.0)


def test_broker_reduce_for_sell_keeps_whole_shares():
    broker = make_broker()
    order = broker.create_order(SYM, Side.SELL, 1000.0, signal_date=D[0], submit_date=D[1])
    broker._apply_reduce(order, 96.4703158766017)
    assert order.quantity == approx(96.0)
    assert abs(order.quantity - round(order.quantity)) < 1e-9


def test_matching_zero_remaining_is_not_retryable():
    """撮合层：无可撮合数量时必须 `can_retry=False`（否则形成僵尸订单）。"""
    cfg = make_config()
    matching = MatchingEngine(cfg.engine, cost_model=CostModel(cfg.costs), seed=7)
    order = Order(order_id="Z1", symbol=SYM, side=Side.BUY, quantity=0.0)
    result = matching.try_match(order, tradable(1), account=Account(1_000_000.0))
    assert result.filled_quantity == 0
    assert result.can_retry is False


def test_matching_reports_residue_quantity():
    """撮合层必须显式暴露「永远无法成交的零头」。"""
    cfg = make_config()
    matching = MatchingEngine(cfg.engine, cost_model=CostModel(cfg.costs), seed=7)
    order = brokerless_order(quantity=1050.0, filled=1000.0)
    result = matching.try_match(order, tradable(1), account=Account(1_000_000.0))
    assert result.reason is RejectReason.LOT_SIZE
    assert result.can_retry is False
    assert result.residue_quantity == approx(50.0)


# --------------------------------------------------------------------------- #
# 4. D2-c：减仓路径不得产生小数股
# --------------------------------------------------------------------------- #
def test_plan_exposure_reduction_is_whole_shares():
    """减仓量必须是整手（持仓 8000 股 / 敞口 80% → 目标 50% → 减 3000 股）。"""
    from aqs.engine.control import plan_exposure_reduction

    account = Account(1_000_000.0)
    add_position(account, SYM, 8000.0, 100.0)  # 持仓 800,000，总资产 1,000,000 → 敞口 80%
    bar = make_bar(SYM, D[1], close=100.0, open_=100.0)
    snapshot = make_snapshot([bar], day=D[1])

    plan = plan_exposure_reduction(account, snapshot, scale=0.5, lot_size=LOT, tolerance=0.01)
    assert plan.orders, "应当产生减仓订单"
    assert plan.exposure_before == approx(0.8)
    for order in plan.orders:
        assert abs(order.quantity - round(order.quantity)) < 1e-9, f"小数股卖单：{order.quantity}"
    assert plan.orders[0].quantity == approx(3000.0)


def test_plan_exposure_reduction_fractional_desired_becomes_whole_shares():
    """关键回归（D2-c）：目标减仓量是**小数**时，卖出量必须取整为整数股。

    构造：持仓 9650 股（敞口 96.5%），减仓比例 1% → 目标减仓 96.5 股（非整数）。
    旧实现（`_round_down` 零股分支 `return float(quantity)`）会原样返回 96.5，
    成交后持仓变成小数，并逐日传染到次日 `sellable`。
    """
    from aqs.engine.control import plan_exposure_reduction

    account = Account(1_000_000.0)
    add_position(account, SYM, 9650.0, 100.0)  # 965,000 持仓 / 1,000,000 总资产 → 敞口 96.5%
    bar = make_bar(SYM, D[1], close=100.0, open_=100.0)
    snapshot = make_snapshot([bar], day=D[1])

    exposure = account.positions_value / account.total_value
    assert exposure == approx(0.965)
    # 反解一个让「目标减仓量」为小数的 scale
    fraction = 0.01
    scale = exposure * (1.0 - fraction)
    desired = 9650.0 * fraction
    assert abs(desired - round(desired)) > 1e-6, "用例前提：目标减仓量应当是非整数"

    plan = plan_exposure_reduction(account, snapshot, scale=scale, lot_size=LOT, tolerance=1e-9)
    assert plan.orders
    qty = plan.orders[0].quantity
    assert abs(qty - round(qty)) < 1e-9, f"小数股卖单：{qty}"
    assert qty == approx(96.0), "96.5 股应取整为 96 股（整数零股），而不是 96.5"


def test_plan_exposure_reduction_odd_lot_is_integer():
    """持仓不足一手时允许一次性卖掉整数零股，但绝不允许小数股。"""
    from aqs.engine.control import plan_exposure_reduction

    account = Account(1_000_000.0)
    add_position(account, SYM, 96.0, 100.0)  # 9,600 持仓 / 1,000,000 总资产
    bar = make_bar(SYM, D[1], close=100.0, open_=100.0)
    snapshot = make_snapshot([bar], day=D[1])

    plan = plan_exposure_reduction(account, snapshot, scale=0.0, lot_size=LOT, tolerance=0.01)
    assert plan.orders
    qty = plan.orders[0].quantity
    assert abs(qty - round(qty)) < 1e-9
    assert qty == approx(96.0)


# --------------------------------------------------------------------------- #
# 5. 账户层：小数股成交必须显式报错
# --------------------------------------------------------------------------- #
def test_account_rejects_fractional_fill():
    from aqs.core.models import CostBreakdown, Fill

    account = Account(1_000_000.0)
    with raises(EngineError):
        account.apply_fill(
            Fill(
                fill_id="F-bad",
                order_id="O-bad",
                symbol=SYM,
                side=Side.BUY,
                quantity=60.43,
                price=10.0,
                trade_date=D[0],
                gross_amount=604.3,
                cost=CostBreakdown(),
            )
        )


# --------------------------------------------------------------------------- #
# 6. 端到端：不变量在完整回测中成立
# --------------------------------------------------------------------------- #
def make_e2e(strict=True, *, symbols: int = 12, lot_size: int = 100):
    start, end = "2022-01-04", "2022-12-30"
    base = load_base_config("configs/base.yaml").with_overlay(
        {
            "data": {"quality": {"strict": False}},
            "universe": {"mode": "all"},
            "engine": {"start": start, "end": end, "initial_cash": 1_000_000.0, "lot_size": lot_size},
        }
    )
    bundle = generate_market_data(n_symbols=symbols, start=start, end=end, seed=20240101)
    store = DataStore(
        bundle.bars,
        index_members=bundle.index_members,
        fundamentals=bundle.fundamentals,
        config=base.data,
        universe_config=base.universe,
    )
    section = {"max_positions": 5, "max_weight_per_symbol": 0.15, "cash_buffer": 0.02}
    portfolio = build_portfolio(section, defaults=base.portfolio, lot_size=base.engine.lot_size)
    strategy = MACrossStrategy({"fast_window": 5, "slow_window": 20})
    engine = BacktestEngine(store, base, strategy=strategy, portfolio=portfolio)
    return engine.run(), engine


def assert_lot_invariants(result, *, lot: int = LOT) -> None:
    """INV-1 / INV-2 / INV-3 / INV-6 的统一检查器（供本模块与后续 M8 复用）。"""
    rows = result.orders.to_dict("records")
    assert rows, "回测应当产生订单"
    for row in rows:
        side = row["side"]
        status = row["final_status"]
        qty = float(row["quantity"])
        filled = float(row["filled_quantity"])

        if filled > 0:
            assert abs(filled - round(filled)) < 1e-9, f"INV-3 违反：成交非整数股 {row}"
        if status is None or status == OrderStatus.DROPPED.value:
            continue  # 被丢弃的订单不进入撮合，数量可以是削减后的任意值（不会被提交）
        if side == "buy":
            assert qty % lot == 0, f"INV-1 违反：BUY 数量非整手 {row}"
        else:
            assert abs(qty - round(qty)) < 1e-9, f"INV-2 违反：SELL 数量非整数股 {row}"
        assert filled <= qty + 1e-9, f"INV-6 违反：filled > quantity {row}"


def test_e2e_orders_satisfy_lot_invariants():
    result, _ = make_e2e()
    assert_lot_invariants(result)


def test_e2e_positions_are_whole_shares():
    result, _ = make_e2e()
    for symbol, pos in result.account.positions.items():
        assert abs(pos.total_quantity - round(pos.total_quantity)) < 1e-9, (
            f"INV-4 违反：{symbol} 持仓非整数股 {pos.total_quantity}"
        )


def test_e2e_no_residue_rejected_orders():
    """修复前：部分成交后剩余不足一手会被标成 `rejected` 并计入拒单率。"""
    result, engine = make_e2e()
    rows = result.orders.to_dict("records")
    bad = [
        r for r in rows
        if r["final_status"] == "rejected"
        and float(r["filled_quantity"]) > 0
        and r["last_reject_reason"] == "lot_size"
    ]
    assert not bad, f"仍存在「已成交却被拒单」的记录：{bad[:3]}"


def test_e2e_reject_rate_has_no_lot_size_inflation():
    """拒单数不应再被「剩余不足一手」污染。"""
    result, engine = make_e2e()
    match_stats = dict(result.broker.matching.stats)
    assert match_stats.get("lot_size", 0) == 0, (
        f"撮合层仍出现 lot_size 事件 {match_stats}，说明仍有非整手订单进入撮合"
    )
    assert result.broker.stats.expired_residue >= 0  # 允许真实存在，但必须可解释


def test_e2e_dropped_below_lot_is_disclosed():
    """被丢弃的订单必须出现在诊断里（不允许静默消失）。"""
    result, _ = make_e2e()
    assert "orders_dropped_below_lot" in result.diagnostics
    assert result.diagnostics["orders_dropped_below_lot"] >= 0
    dropped = [r for r in result.orders.to_dict("records") if r["final_status"] == "dropped"]
    assert len(dropped) == result.diagnostics["orders_dropped_below_lot"]
    for row in dropped:
        assert row["last_reject_reason"] == "below_lot"


def test_e2e_lot_size_comes_from_config():
    """`engine.lot_size=200` → 所有 BUY 订单都是 200 的倍数。"""
    result, _ = make_e2e(lot_size=200)
    assert_lot_invariants(result, lot=200)


def test_demo_orders_csv_has_only_executable_quantities(tmp_path=None):
    """落盘 orders.csv 中不得出现「小数股且被提交」的记录（端到端落盘检查）。"""
    from tests.tools import workspace_tmp

    result, _ = make_e2e()
    with workspace_tmp("defect13") as out:
        frame = result.orders
        frame.to_csv(out / "orders.csv", index=False, encoding="utf-8-sig")
        import csv

        with open(out / "orders.csv", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert rows
        for row in rows:
            if row["final_status"] in ("submitted", "partially_filled", "filled"):
                qty = float(row["quantity"])
                if row["side"] == "buy":
                    assert qty % LOT == 0, row
                else:
                    assert abs(qty - round(qty)) < 1e-9, row


def test_flat_market_store_still_builds():
    """哨兵：本模块依赖的夹具本身可用（避免夹具漂移导致误报通过）。"""
    store = make_store(flat_market(("600000.SH",), n=5))
    assert store.symbols() == ["600000.SH"]
    assert str(D[0]) == DEFAULT_START
    assert entry_signal(SYM, D[0]).symbol == SYM
