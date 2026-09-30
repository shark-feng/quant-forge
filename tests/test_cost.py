"""成本模型测试：佣金、印花税、过户费、滑点、冲击成本、成本加倍。"""

from __future__ import annotations

from datetime import date

from tests.compat import approx

from aqs.config.schema import CostConfig, construct
from aqs.core.enums import Side
from aqs.engine.cost import CostModel

DAY = date(2024, 5, 6)
ADV = 1_000_000.0


def model(**overrides) -> CostModel:
    return CostModel(construct(CostConfig, overrides))


# --------------------------------------------------------------------------- #
# 佣金
# --------------------------------------------------------------------------- #
def test_commission_rate_applies_above_minimum():
    m = model()
    r = m.compute(side=Side.BUY, quantity=100_000, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    gross = 100_000 * 10.01
    assert r.gross_amount == approx(gross)
    assert r.breakdown.commission == approx(gross * 0.00025)


def test_minimum_commission_applies_to_small_orders():
    m = model(min_commission=5.0)
    r = m.compute(side=Side.BUY, quantity=100, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    assert r.gross_amount == approx(1001.0)
    assert r.breakdown.commission == approx(5.0)


def test_commission_rate_zero():
    m = model(commission_rate=0.0, min_commission=0.0)
    r = m.compute(side=Side.BUY, quantity=1000, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    assert r.breakdown.commission == approx(0.0)


# --------------------------------------------------------------------------- #
# 印花税（仅卖出，按日期分段）
# --------------------------------------------------------------------------- #
def test_stamp_tax_sell_only():
    m = model()
    buy = m.compute(side=Side.BUY, quantity=1000, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    sell = m.compute(side=Side.SELL, quantity=1000, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    assert buy.breakdown.stamp_tax == 0.0
    assert sell.breakdown.stamp_tax == approx(sell.gross_amount * 0.0005)


def test_stamp_tax_rate_changes_on_2023_08_28():
    m = model()
    old = m.compute(side=Side.SELL, quantity=1000, reference_price=10.0, trade_date=date(2023, 8, 25), adv_volume=ADV)
    new = m.compute(side=Side.SELL, quantity=1000, reference_price=10.0, trade_date=date(2023, 8, 28), adv_volume=ADV)
    assert old.breakdown.stamp_tax == approx(old.gross_amount * 0.001)
    assert new.breakdown.stamp_tax == approx(new.gross_amount * 0.0005)
    assert old.breakdown.stamp_tax == approx(2 * new.breakdown.stamp_tax)


def test_stamp_tax_can_be_disabled_by_empty_schedule():
    m = model(stamp_tax={"schedule": [{"effective_from": "1990-01-01", "rate": 0.0}]})
    sell = m.compute(side=Side.SELL, quantity=1000, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    assert sell.breakdown.stamp_tax == approx(0.0)


# --------------------------------------------------------------------------- #
# 过户费（双向）
# --------------------------------------------------------------------------- #
def test_transfer_fee_both_sides():
    m = model()
    buy = m.compute(side=Side.BUY, quantity=1000, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    sell = m.compute(side=Side.SELL, quantity=1000, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    assert buy.breakdown.transfer_fee == approx(buy.gross_amount * 0.00001)
    assert sell.breakdown.transfer_fee == approx(sell.gross_amount * 0.00001)


# --------------------------------------------------------------------------- #
# 滑点
# --------------------------------------------------------------------------- #
def test_slippage_moves_price_against_direction():
    m = model()
    buy = m.compute(side=Side.BUY, quantity=1000, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    sell = m.compute(side=Side.SELL, quantity=1000, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    assert buy.executed_price == approx(10.01)
    assert sell.executed_price == approx(9.99)
    assert buy.breakdown.slippage == approx(1000 * 0.01)
    assert sell.breakdown.slippage == approx(1000 * 0.01)
    assert buy.slippage_rate == approx(0.001)


def test_slippage_as_cost_when_price_not_adjusted():
    m = model()
    r = m.compute(
        side=Side.BUY,
        quantity=1000,
        reference_price=10.0,
        trade_date=DAY,
        adv_volume=ADV,
        apply_price_adjust=False,
    )
    assert r.executed_price == approx(10.0)
    assert r.breakdown.slippage == approx(1000 * 10.0 * 0.001)


def test_slippage_components_add_up():
    m = model(slippage={"fixed_bps": 5.0, "prop_bps": 10.0})
    r = m.compute(side=Side.BUY, quantity=1000, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    assert r.executed_price == approx(10.0 * 1.0015)
    assert r.slippage_rate == approx(0.0015)


def test_zero_slippage():
    m = model(slippage={"fixed_bps": 0.0, "prop_bps": 0.0})
    r = m.compute(side=Side.BUY, quantity=1000, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    assert r.executed_price == approx(10.0)
    assert r.breakdown.slippage == approx(0.0)


# --------------------------------------------------------------------------- #
# 冲击成本
# --------------------------------------------------------------------------- #
def test_impact_cost_formula():
    m = model(impact={"enabled": True, "eta": 0.1, "theta": 0.5})
    qty = 100_000.0
    r = m.compute(side=Side.BUY, quantity=qty, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    expected_fraction = 0.1 * (qty / ADV) ** 0.5
    assert r.impact_fraction == approx(expected_fraction)
    assert r.breakdown.impact == approx(r.gross_amount * expected_fraction)


def test_impact_disabled():
    m = model(impact={"enabled": False})
    r = m.compute(side=Side.BUY, quantity=100_000, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    assert r.breakdown.impact == approx(0.0)


def test_impact_missing_adv_is_counted_not_silently_ignored():
    m = model(impact={"enabled": True})
    r = m.compute(side=Side.BUY, quantity=1000, reference_price=10.0, trade_date=DAY, adv_volume=None)
    assert r.breakdown.impact == approx(0.0)
    assert m.missing_adv_count == 1


def test_impact_theta_changes_curvature():
    linear = model(impact={"enabled": True, "eta": 0.1, "theta": 1.0})
    sqrt = model(impact={"enabled": True, "eta": 0.1, "theta": 0.5})
    r_lin = linear.compute(side=Side.BUY, quantity=100_000, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    r_sqrt = sqrt.compute(side=Side.BUY, quantity=100_000, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    # Q/ADV = 0.1 → θ=1 时 1%，θ=0.5 时 3.16%
    assert r_lin.impact_fraction == approx(0.01)
    assert r_sqrt.impact_fraction == approx(0.1 * 0.1**0.5)
    assert r_sqrt.impact_fraction > r_lin.impact_fraction


# --------------------------------------------------------------------------- #
# 总额与成本加倍
# --------------------------------------------------------------------------- #
def test_total_is_sum_of_components():
    m = model()
    r = m.compute(side=Side.SELL, quantity=10_000, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    b = r.breakdown
    assert b.total == approx(
        b.fixed_fee + b.commission + b.stamp_tax + b.transfer_fee + b.slippage + b.impact
    )
    assert r.total_cost == approx(b.total)


def test_fixed_fee_per_order():
    m = model(fixed_fee_per_order=3.0)
    r = m.compute(side=Side.BUY, quantity=1000, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    assert r.breakdown.fixed_fee == approx(3.0)


def test_cost_doubling_scales_total_by_two():
    base = model()
    doubled = model(scale=2.0)
    args = dict(quantity=10_000, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    base_r = base.compute(side=Side.BUY, **args)
    dbl_r = doubled.compute(side=Side.BUY, **args)
    ratio = dbl_r.total_cost / base_r.total_cost
    assert abs(ratio - 2.0) < 0.01, f"成本加倍后比值应为 2，实际 {ratio}"
    # 成交价同时变差（滑点加倍）
    assert dbl_r.executed_price > base_r.executed_price


def test_cost_scaled_helper():
    m = model()
    assert m.scaled(3.0).scale == approx(3.0)
    assert m.scale == approx(1.0)


def test_zero_quantity_returns_empty_cost():
    m = model()
    r = m.compute(side=Side.BUY, quantity=0.0, reference_price=10.0, trade_date=DAY, adv_volume=ADV)
    assert r.total_cost == approx(0.0)
    assert r.breakdown.total == approx(0.0)


def test_round_trip_rate_is_positive_and_reasonable():
    """往返成本率：佣金 0.025%×2 + 印花税 0.05% + 过户费 0.001%×2 + 滑点 0.1%×2 ≈ 0.302%。"""
    m = model(impact={"enabled": False})
    rate = m.round_trip_rate(adv_volume=ADV, notional=1_000_000, day=DAY)
    assert 0.0028 < rate < 0.0032, rate
    # 计入冲击成本后往返成本显著上升
    with_impact = model().round_trip_rate(adv_volume=ADV, notional=1_000_000, day=DAY)
    assert with_impact > rate
    assert with_impact == approx(rate + 2 * 0.1 * (100_000 / ADV) ** 0.5, rel=0.05)


def test_describe_contains_key_fields():
    info = model().describe()
    assert info["commission_rate"] == approx(0.00025)
    assert info["slippage_bps"]["proportional"] == approx(10.0)
    assert info["impact"]["eta"] == approx(0.1)
    assert len(info["stamp_tax_schedule"]) >= 2


def test_breakdown_add_and_scale():
    from aqs.core.models import CostBreakdown

    a = CostBreakdown(commission=1.0, stamp_tax=2.0)
    b = CostBreakdown(commission=3.0, slippage=4.0)
    c = a + b
    assert c.commission == approx(4.0)
    assert c.stamp_tax == approx(2.0)
    assert c.slippage == approx(4.0)
    assert c.total == approx(10.0)
    assert a.scaled(2.0).total == approx(6.0)
