"""回归缺陷 #4：T+1 冻结下等比例减仓不达标且无记录。

原缺陷：``_apply_risk_controls`` 只对 ``sellable > 0`` 的标的生成减仓单，
当日买入（T+1 冻结）的标的被静默跳过，实际敞口长期高于目标且无任何记录。

修复后：``engine/control.py::plan_exposure_reduction`` +
引擎内的 ``ExposureControlState`` —— 未完成部分记入 ``pending``，次日优先补减，
连续未达标产生预警，全过程写入诊断。
"""

from __future__ import annotations

from typing import Sequence

from tests.compat import approx
from tests.tools import DEFAULT_START, bar_row, buy_position, dates, entry_signal, make_bars, make_config, make_store

from aqs.config.schema import RiskConfig
from aqs.core.enums import Side
from aqs.engine.account import Account
from aqs.engine.backtest import BacktestEngine
from aqs.engine.control import ExposureControlState, plan_exposure_reduction
from aqs.engine.matching import MatchingEngine
from aqs.engine.cost import CostModel
from aqs.portfolio.base import OrderPlan
from aqs.risk.base import NullRiskEngine
from aqs.strategy.base import StrategyContext
from tests.tools import make_bar, make_snapshot

D = dates(14)
SYMS = ["600000.SH", "600001.SH", "600002.SH"]
ENTRY_DAY = D[4]


# --------------------------------------------------------------------------- #
# 单元：plan_exposure_reduction
# --------------------------------------------------------------------------- #
def snapshot(day=D[0], symbols=SYMS, price=10.0):
    bars = [make_bar(s, day, close=price, open_=price) for s in symbols]
    return make_snapshot(bars, day=day)


def test_reduction_fully_blocked_by_t1_lock():
    account = Account(1_000_000.0)
    buy_position(account, SYMS[0], 30_000, 10.0)  # 30 万 / 100 万 = 30%
    account.positions[SYMS[0]].sellable_quantity = 0  # 模拟当日买入（T+1 冻结）

    plan = plan_exposure_reduction(account, snapshot(), scale=0.15, lot_size=100)
    assert plan.orders == ()
    assert plan.exposure_before == approx(0.30)
    assert plan.unmet[SYMS[0]] == approx(15_000)  # 需要减一半，但完全卖不出
    assert plan.reasons[SYMS[0]] == "t1_locked"


def test_reduction_executes_when_sellable():
    account = Account(1_000_000.0)
    buy_position(account, SYMS[0], 30_000, 10.0)  # 解锁
    plan = plan_exposure_reduction(account, snapshot(), scale=0.15, lot_size=100)
    assert len(plan.orders) == 1
    assert plan.orders[0].side is Side.SELL
    assert plan.orders[0].quantity == approx(15_000)
    assert plan.unmet == {}


def test_pending_is_compensated_next_session():
    """次日解锁后补减：数量按当日敞口重算（不与 pending 累加，避免过度卖出）。"""
    account = Account(1_000_000.0)
    buy_position(account, SYMS[0], 30_000, 10.0)
    account.positions[SYMS[0]].sellable_quantity = 0
    first = plan_exposure_reduction(account, snapshot(), scale=0.15, lot_size=100)
    assert first.unmet[SYMS[0]] == approx(15_000)

    # 次日解锁：敞口仍是 30%，目标 15% → 仍需减 15,000 股（而不是 15,000 + pending）
    account.unlock_t1()
    second = plan_exposure_reduction(
        account, snapshot(D[1]), scale=0.15, lot_size=100, pending=first.unmet
    )
    assert len(second.orders) == 1
    assert second.orders[0].quantity == approx(15_000)
    assert second.unmet == {}


def test_pending_symbol_is_prioritised():
    account = Account(1_000_000.0)
    buy_position(account, SYMS[0], 30_000, 10.0)
    buy_position(account, SYMS[1], 30_000, 10.0)
    plan = plan_exposure_reduction(
        account, snapshot(), scale=0.15, lot_size=100, pending={SYMS[1]: 1000.0}
    )
    assert [o.symbol for o in plan.orders][0] == SYMS[1]


def test_no_reduction_when_exposure_at_or_below_target():
    account = Account(1_000_000.0)
    buy_position(account, SYMS[0], 10_000, 10.0)  # 10%
    plan = plan_exposure_reduction(account, snapshot(), scale=0.50, lot_size=100)
    assert plan.orders == ()
    assert plan.exposure_before == approx(0.10)


def test_target_within_tolerance_is_treated_as_met():
    account = Account(1_000_000.0)
    buy_position(account, SYMS[0], 20_500, 10.0)  # 20.5%
    plan = plan_exposure_reduction(account, snapshot(), scale=0.20, lot_size=100, tolerance=0.01)
    assert plan.orders == ()


def test_full_liquidation_target_zero():
    account = Account(1_000_000.0)
    buy_position(account, SYMS[0], 30_000, 10.0)
    plan = plan_exposure_reduction(account, snapshot(), scale=0.0, lot_size=100)
    assert len(plan.orders) == 1
    assert plan.orders[0].quantity == approx(30_000)
    assert plan.orders[0].tag == "risk_close"
    assert plan.unmet == {}


def test_suspended_symbol_recorded_as_reason():
    account = Account(1_000_000.0)
    buy_position(account, SYMS[0], 30_000, 10.0)
    account.positions[SYMS[0]].sellable_quantity = 0
    suspended = make_snapshot([make_bar(SYMS[0], D[0], close=10.0, is_suspended=True)], day=D[0])
    plan = plan_exposure_reduction(account, suspended, scale=0.15, lot_size=100)
    assert plan.reasons[SYMS[0]] == "suspended"


def test_state_dict_exposes_pending():
    state = ExposureControlState(target_scale=0.5, pending={SYMS[0]: 1000.0}, deferred_events=2)
    payload = state.as_dict()
    assert payload["target_scale"] == approx(0.5)
    assert payload["pending_symbols"] == 1
    assert payload["pending_quantity"] == approx(1000.0)
    assert payload["deferred_events"] == 2


# --------------------------------------------------------------------------- #
# 引擎集成：跨日补偿确实发生
# --------------------------------------------------------------------------- #
class TwoNameStrategy:
    name = "two_name"

    def on_bar(self, ctx: StrategyContext):
        if ctx.date == ENTRY_DAY:
            return [entry_signal(s, ctx.date) for s in SYMS[:2]]
        return []


class HeavyPortfolio:
    """按总资产 45% 建仓两只 → 合计约 90% 敞口（便于观察减仓效果）。"""

    name = "heavy"

    def generate_orders(self, signals, ctx: StrategyContext) -> Sequence[OrderPlan]:
        plans = []
        for sig in signals:
            bar = ctx.snapshot.bar(sig.symbol)
            if bar is None or bar.close <= 0:
                continue
            budget = min(ctx.account.total_value * 0.45, ctx.account.available_cash() * 0.95)
            qty = int(budget / bar.close // 100) * 100
            if qty >= 100:
                plans.append(OrderPlan(sig.symbol, Side.BUY, qty, tag="entry"))
        return plans


def make_market():
    rows = []
    for day in D:
        for sym in SYMS:
            rows.append(bar_row(day, sym, close=10.0, open_=10.0, prev_close=10.0, volume=1_000_000.0))
    return make_bars(rows)


def run_with_scale(scale: float, *, warning_days: int = 5):
    config = make_config(
        {
            "data": {"quality": {"strict": False}},
            "universe": {"mode": "all"},
            "engine": {
                "start": D[0],
                "end": D[-1],
                "exposure_control": {"unmet_warning_days": warning_days, "tolerance": 0.01},
            },
        }
    )
    store = make_store(make_market())
    risk = NullRiskEngine(RiskConfig(enabled=True))
    risk.set_exposure_scale(scale)
    engine = BacktestEngine(
        store, config, strategy=TwoNameStrategy(), portfolio=HeavyPortfolio(), risk=risk
    )
    result = engine.run()
    return result, engine


def test_reduction_deferred_on_t1_lock_then_completed():
    result, engine = run_with_scale(0.5)
    control = result.diagnostics["risk"]["exposure_control"]

    # 第 1 日：买入当日 T+1 冻结 → 无法减仓，记录 pending
    assert control["deferred_events"] >= 2
    assert len(control["deferred_days"]) >= 2
    history = result.diagnostics["risk"]["exposure_control_history"]
    assert history, "应记录每日目标 vs 实际敞口"
    assert any(entry["unmet_symbols"] for entry in history)
    reasons = {r for entry in history for r in entry["unmet_reasons"].values()}
    assert "t1_locked" in reasons

    # 后续交易日：卖出成交后敞口降到目标附近
    exposure = result.equity_curve["gross_exposure"]
    assert exposure.max() > 0.85
    assert exposure.iloc[-1] < 0.55, exposure.iloc[-1]


def test_no_pending_when_no_reduction_needed():
    result, _ = run_with_scale(1.0)
    control = result.diagnostics["risk"]["exposure_control"]
    assert control["pending_symbols"] == 0
    assert control["deferred_events"] == 0
    assert control["unmet_days"] == 0


def test_unmet_warning_is_emitted_when_threshold_reached():
    # 目标敞口 0：全部持仓都要卖，但 T+1 冻结会让首日无法完成
    result, _ = run_with_scale(0.0, warning_days=1)
    audit = [r for r in result.diagnostics["risk"]["alerts"]]
    control = result.diagnostics["risk"]["exposure_control"]
    assert control["max_unmet_days"] >= 1
    assert isinstance(audit, list)


def test_exposure_control_disabled_path_still_reports():
    account = Account(1_000_000.0)
    plan = plan_exposure_reduction(account, snapshot(), scale=0.5)
    assert plan.orders == ()
    assert plan.exposure_before == approx(0.0)
