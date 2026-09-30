"""风控规则逐条测试（每条规则至少「触发」与「不触发」两个用例）。"""

from __future__ import annotations

import numpy as np

from tests.compat import approx
from tests.tools import DEFAULT_START, buy_position, make_bar, make_order, make_risk_context

from aqs.core.enums import OrderType, RiskAction, Side
from aqs.engine.account import Account
from aqs.risk.rules import (
    CancelRatioRule,
    CashSufficiencyRule,
    DailyLossLimitRule,
    GrossExposureRule,
    IndustryExposureRule,
    MaxDrawdownRule,
    MaxOrderNotionalRule,
    MaxOrderQuantityRule,
    MaxPositionPerSymbolRule,
    MaxTradeVolumeDailyRule,
    OrderFrequencyRule,
    PortfolioVaRLimitRule,
    PriceDeviationRule,
)

DAY = DEFAULT_START
SYM = "600000.SH"


def flat_account(cash: float = 1_000_000.0) -> Account:
    return Account(cash)


# --------------------------------------------------------------------------- #
# 订单级
# --------------------------------------------------------------------------- #
def test_max_order_quantity_rule():
    rule = MaxOrderQuantityRule({"max_quantity": 1_000_000})
    ctx = make_risk_context(order=make_order(SYM, Side.BUY, 2_000_000), account=flat_account())
    decision = rule.check(ctx)
    assert decision.action is RiskAction.REDUCE
    assert decision.modified_quantity == approx(1_000_000)
    # 不触发
    ctx2 = make_risk_context(order=make_order(SYM, Side.BUY, 800_000), account=flat_account())
    assert rule.check(ctx2).action is RiskAction.ALLOW


def test_max_order_quantity_rule_can_reject():
    rule = MaxOrderQuantityRule({"max_quantity": 1000}, action="reject")
    ctx = make_risk_context(order=make_order(SYM, Side.BUY, 2000), account=flat_account())
    assert rule.check(ctx).action is RiskAction.REJECT


def test_max_order_notional_rule():
    rule = MaxOrderNotionalRule({"max_notional": 2_000_000.0})
    bars = [make_bar(SYM, DAY, close=10.0)]
    ctx = make_risk_context(
        order=make_order(SYM, Side.BUY, 300_000), account=flat_account(), bars=bars
    )
    decision = rule.check(ctx)
    assert decision.action is RiskAction.REDUCE
    assert decision.modified_quantity == approx(200_000)  # 2,000,000 / 10
    ctx2 = make_risk_context(order=make_order(SYM, Side.BUY, 100_000), account=flat_account(), bars=bars)
    assert rule.check(ctx2).action is RiskAction.ALLOW


def test_max_order_notional_skips_without_price():
    rule = MaxOrderNotionalRule({"max_notional": 1.0})
    ctx = make_risk_context(order=make_order("999999.SH", Side.BUY, 1000), account=flat_account(), bars=[])
    assert rule.check(ctx).action is RiskAction.ALLOW


def test_price_deviation_rule():
    rule = PriceDeviationRule({"max_deviation": 0.05})
    bars = [make_bar(SYM, DAY, close=10.0)]
    # 限价 10.6 偏离 6% → 拒
    order = make_order(SYM, Side.BUY, 1000, order_type=OrderType.LIMIT, limit_price=10.6)
    assert rule.check(make_risk_context(order=order, bars=bars, account=flat_account())).action is RiskAction.REJECT
    # 限价 10.4 → 放行
    order2 = make_order(SYM, Side.BUY, 1000, order_type=OrderType.LIMIT, limit_price=10.4)
    assert rule.check(make_risk_context(order=order2, bars=bars, account=flat_account())).action is RiskAction.ALLOW
    # 市价单不受限价偏离约束
    order3 = make_order(SYM, Side.BUY, 1000)
    assert rule.check(make_risk_context(order=order3, bars=bars, account=flat_account())).action is RiskAction.ALLOW
    # 关闭开关
    rule_off = PriceDeviationRule({"max_deviation": 0.01, "enabled": False})
    assert rule_off.check(make_risk_context(order=order, bars=bars, account=flat_account())).action is RiskAction.ALLOW


def test_order_frequency_rule():
    rule = OrderFrequencyRule({"max_orders_per_day": 100})
    ctx = make_risk_context(order=make_order(SYM, Side.BUY, 100), account=flat_account())
    ctx.day.orders_submitted = 99
    assert rule.check(ctx).action is RiskAction.ALLOW
    ctx.day.orders_submitted = 100
    assert rule.check(ctx).action is RiskAction.REJECT


def test_cancel_ratio_rule():
    rule = CancelRatioRule({"max_cancel_ratio": 0.8, "min_orders_for_check": 50})
    ctx = make_risk_context(order=make_order(SYM, Side.BUY, 100), account=flat_account())
    ctx.day.orders_submitted = 100
    ctx.day.orders_cancelled = 50
    assert rule.check(ctx).action is RiskAction.ALLOW
    ctx.day.orders_cancelled = 90
    decision = rule.check(ctx)
    assert decision.action is RiskAction.PAUSE
    assert "撤单率" in decision.message
    # 样本不足不判定
    ctx2 = make_risk_context(order=make_order(SYM, Side.BUY, 100), account=flat_account())
    ctx2.day.orders_submitted = 10
    ctx2.day.orders_cancelled = 10
    assert rule.check(ctx2).action is RiskAction.ALLOW


# --------------------------------------------------------------------------- #
# 仓位与敞口
# --------------------------------------------------------------------------- #
def test_max_trade_volume_daily_rule():
    rule = MaxTradeVolumeDailyRule({"max_daily_volume_ratio": 0.20})
    bars = [make_bar(SYM, DAY, close=10.0, volume=1_000_000.0)]
    ctx = make_risk_context(
        order=make_order(SYM, Side.BUY, 300_000),
        account=flat_account(),
        bars=bars,
        adv_volume={SYM: 1_000_000.0},
    )
    decision = rule.check(ctx)
    assert decision.action is RiskAction.REDUCE
    assert decision.modified_quantity == approx(200_000)
    # 当日已用 150 万分之一百五十 → 剩余 5 万
    ctx.day.add_trade(SYM, 150_000)
    assert rule.check(ctx).modified_quantity == approx(50_000)
    # 用完 → 拒
    ctx.day.add_trade(SYM, 50_000)
    assert rule.check(ctx).action is RiskAction.REJECT
    # 无 ADV → 跳过
    ctx3 = make_risk_context(order=make_order(SYM, Side.BUY, 999_999), bars=bars, account=flat_account())
    assert rule.check(ctx3).action is RiskAction.ALLOW


def test_max_position_per_symbol_rule():
    rule = MaxPositionPerSymbolRule({"max_weight": 0.10})
    bars = [make_bar(SYM, DAY, close=10.0)]
    account = flat_account()
    # 无持仓：上限 10 万元 → 1 万股
    ctx = make_risk_context(order=make_order(SYM, Side.BUY, 20_000), account=account, bars=bars)
    assert rule.check(ctx).modified_quantity == approx(10_000)
    # 已有 5 万元持仓 → 剩余额度 5 万元 → 5000 股
    account2 = flat_account()
    buy_position(account2, SYM, 5_000, 10.0)
    ctx2 = make_risk_context(order=make_order(SYM, Side.BUY, 20_000), account=account2, bars=bars)
    assert rule.check(ctx2).modified_quantity == approx(5_000)
    # 已满仓 → 削减到 0（引擎会转成拒单）
    account3 = flat_account()
    buy_position(account3, SYM, 10_000, 10.0)
    ctx3 = make_risk_context(order=make_order(SYM, Side.BUY, 1_000), account=account3, bars=bars)
    assert rule.check(ctx3).modified_quantity == approx(0.0)


def test_industry_exposure_rule():
    rule = IndustryExposureRule({"max_industry_weight": 0.30, "industry_map": {SYM: "tech", "600001.SH": "tech"}})
    bars = [make_bar(SYM, DAY, close=10.0), make_bar("600001.SH", DAY, close=10.0)]
    account = flat_account()
    buy_position(account, "600001.SH", 20_000, 10.0)  # 20 万 / 100 万 = 20%
    ctx = make_risk_context(order=make_order(SYM, Side.BUY, 20_000), account=account, bars=bars)
    decision = rule.check(ctx)
    assert decision.action is RiskAction.REDUCE
    assert decision.modified_quantity == approx(10_000)  # 只剩 10 万额度
    # 无行业信息 → 跳过
    rule2 = IndustryExposureRule({"max_industry_weight": 0.10})
    ctx2 = make_risk_context(order=make_order(SYM, Side.BUY, 100_000), account=account, bars=bars)
    assert rule2.check(ctx2).action is RiskAction.ALLOW
    # 通过 industry_of 回调提供行业
    ctx3 = make_risk_context(
        order=make_order(SYM, Side.BUY, 20_000),
        account=account,
        bars=bars,
        industry_of=lambda s: "tech",
    )
    assert rule2.check(ctx3).action is RiskAction.REDUCE


def test_gross_exposure_rule():
    rule = GrossExposureRule({"max_gross_exposure": 1.0, "allow_short": False})
    bars = [make_bar(SYM, DAY, close=10.0), make_bar("600001.SH", DAY, close=10.0)]
    account = flat_account()
    buy_position(account, "600001.SH", 90_000, 10.0)  # 90 万 = 90%
    ctx = make_risk_context(order=make_order(SYM, Side.BUY, 20_000), account=account, bars=bars)
    decision = rule.check(ctx)
    assert decision.action is RiskAction.REDUCE
    assert decision.modified_quantity == approx(10_000)
    # 满仓 → 拒
    account2 = flat_account()
    buy_position(account2, "600001.SH", 100_000, 10.0)
    ctx2 = make_risk_context(order=make_order(SYM, Side.BUY, 1_000), account=account2, bars=bars)
    assert rule.check(ctx2).action is RiskAction.REJECT


def test_gross_exposure_blocks_short_selling():
    rule = GrossExposureRule({"allow_short": False})
    bars = [make_bar(SYM, DAY, close=10.0)]
    # 无持仓卖出 → 拒（禁止卖空）
    ctx = make_risk_context(order=make_order(SYM, Side.SELL, 1000), account=flat_account(), bars=bars)
    assert rule.check(ctx).action is RiskAction.REJECT
    # 卖出超过持仓 → 削减到持仓量
    account = flat_account()
    buy_position(account, SYM, 1_000, 10.0)
    ctx2 = make_risk_context(order=make_order(SYM, Side.SELL, 1_500), account=account, bars=bars)
    decision = rule.check(ctx2)
    assert decision.action is RiskAction.REDUCE
    assert decision.modified_quantity == approx(1_000)
    # 卖出等于持仓 → 放行（T+1 可卖量由撮合层在执行当日强制）
    ctx3 = make_risk_context(order=make_order(SYM, Side.SELL, 1_000), account=account, bars=bars)
    assert rule.check(ctx3).action is RiskAction.ALLOW


def test_cash_sufficiency_rule():
    rule = CashSufficiencyRule({"min_cash_ratio": 0.02})
    bars = [make_bar(SYM, DAY, close=10.0)]
    account = flat_account(30_000.0)
    ctx = make_risk_context(order=make_order(SYM, Side.BUY, 10_000), account=account, bars=bars)
    decision = rule.check(ctx)
    # 总资产 3 万 → 需保留 600 元，可花 2.94 万 → 2,940 股
    assert decision.action is RiskAction.REDUCE
    assert decision.modified_quantity == approx(2_940, rel=1e-3)
    # 现金占比过低（持仓 99 万 + 现金 1 万）→ 削减到 0（引擎会转成拒单）
    account2 = flat_account(1_000_000.0)
    buy_position(account2, "600001.SH", 99_000, 10.0)
    assert account2.cash == approx(10_000.0)
    ctx2 = make_risk_context(
        order=make_order(SYM, Side.BUY, 10_000), account=account2, bars=[*bars, make_bar("600001.SH", DAY, close=10.0)]
    )
    decision2 = rule.check(ctx2)
    assert decision2.action is RiskAction.REDUCE
    assert decision2.modified_quantity == approx(0.0, abs=1e-6)
    # 现金充足 → 放行
    ctx3 = make_risk_context(order=make_order(SYM, Side.BUY, 1_000), account=flat_account(), bars=bars)
    assert rule.check(ctx3).action is RiskAction.ALLOW


# --------------------------------------------------------------------------- #
# 损失与回撤
# --------------------------------------------------------------------------- #
def test_daily_loss_limit_rule():
    rule = DailyLossLimitRule({"max_daily_loss": 0.03})
    account = flat_account(960_000.0)
    ctx = make_risk_context(order=make_order(SYM, Side.BUY, 1000), account=account, start_value=1_000_000.0)
    decision = rule.check(ctx)
    assert decision.action is RiskAction.PAUSE
    assert "当日亏损" in decision.message
    # 亏损 2% → 放行
    ctx2 = make_risk_context(
        order=make_order(SYM, Side.BUY, 1000), account=flat_account(980_000.0), start_value=1_000_000.0
    )
    assert rule.check(ctx2).action is RiskAction.ALLOW


def test_max_drawdown_rule_bands():
    rule = MaxDrawdownRule(
        {"warn_drawdown": 0.10, "reduce_drawdown": 0.15, "force_close_drawdown": 0.20, "reduce_exposure": 0.5}
    )
    # 正常
    ctx = make_risk_context(order=make_order(SYM, Side.BUY, 100), account=flat_account(), start_value=1_000_000.0)
    ctx.engine.update_value(1_000_000.0)
    assert rule.check(ctx).action is RiskAction.ALLOW
    assert ctx.engine.exposure_scale == approx(1.0)

    # 预警区（-12%）：放行但记录
    ctx2 = make_risk_context(order=make_order(SYM, Side.BUY, 100), account=flat_account(), start_value=1_000_000.0)
    ctx2.engine.update_value(1_000_000.0)
    ctx2.engine.update_value(880_000.0)
    decision = rule.check(ctx2)
    assert decision.action is RiskAction.ALLOW
    assert "预警" in decision.message

    # 减仓区（-16%）：敞口降至 0.5
    ctx3 = make_risk_context(order=make_order(SYM, Side.BUY, 100), account=flat_account(), start_value=1_000_000.0)
    ctx3.engine.update_value(1_000_000.0)
    ctx3.engine.update_value(840_000.0)
    assert rule.check(ctx3).action is RiskAction.ALLOW
    assert ctx3.engine.exposure_scale == approx(0.5)

    # 强平区（-25%）
    ctx4 = make_risk_context(order=make_order(SYM, Side.BUY, 100), account=flat_account(), start_value=1_000_000.0)
    ctx4.engine.update_value(1_000_000.0)
    ctx4.engine.update_value(750_000.0)
    decision4 = rule.check(ctx4)
    assert decision4.action is RiskAction.FORCE_CLOSE
    assert ctx4.engine.exposure_scale == approx(0.0)


def test_portfolio_var_limit_rule():
    rng = np.random.default_rng(11)
    series = list(rng.normal(0.0, 0.02, 80))

    def provider(symbol: str, day, window: int):
        return series

    rule = PortfolioVaRLimitRule(
        {"max_var": 0.20, "confidence": 0.95, "window": 60, "horizon": 1, "min_obs": 30}
    )
    bars = [make_bar(SYM, DAY, close=10.0)]
    # 上限很宽松 → 放行
    ctx = make_risk_context(
        order=make_order(SYM, Side.BUY, 10_000),
        account=flat_account(),
        bars=bars,
        returns_of=provider,
    )
    assert rule.check(ctx).action is RiskAction.ALLOW

    # 上限收紧到 0.1%（单票 10% 仓位的 VaR ≈ 0.33%）→ 削减
    tight = PortfolioVaRLimitRule({"max_var": 0.001, "window": 60, "min_obs": 30})
    decision = tight.check(ctx)
    assert decision.action is RiskAction.REDUCE
    assert decision.modified_quantity is not None
    assert 0 <= decision.modified_quantity < 10_000

    # 无收益提供器 → 跳过
    ctx2 = make_risk_context(order=make_order(SYM, Side.BUY, 10_000), account=flat_account(), bars=bars)
    assert tight.check(ctx2).action is RiskAction.ALLOW
    assert "VaR 规则跳过" in tight.check(ctx2).message

    # 样本不足 → 跳过
    short = PortfolioVaRLimitRule({"max_var": 0.01, "min_obs": 500})
    assert short.check(ctx).action is RiskAction.ALLOW

    # 卖出不受 VaR 约束
    account = flat_account()
    buy_position(account, SYM, 1_000, 10.0)
    ctx3 = make_risk_context(
        order=make_order(SYM, Side.SELL, 1_000), account=account, bars=bars, returns_of=provider
    )
    assert tight.check(ctx3).action is RiskAction.ALLOW


# --------------------------------------------------------------------------- #
# 规则接口
# --------------------------------------------------------------------------- #
def test_rule_from_config_builds():
    from aqs.config.schema import RiskRuleConfig
    from aqs.risk.registry import build_rule

    rule = build_rule(RiskRuleConfig(name="max_order_quantity", priority=5, action="reduce", params={"max_quantity": 10}))
    assert rule.name == "max_order_quantity"
    assert rule.priority == 5
    assert isinstance(rule, MaxOrderQuantityRule)


def test_rule_allow_helper_returns_allow():
    rule = MaxOrderQuantityRule({})
    decision = rule.allow("说明")
    assert decision.action is RiskAction.ALLOW
    assert decision.rule == "max_order_quantity"
    assert decision.message == "说明"
