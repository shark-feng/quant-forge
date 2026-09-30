"""RMS 与引擎联调：订单削减、当日亏损暂停、回撤减仓/强平、验收指标输出。"""

from __future__ import annotations

from typing import Sequence

from tests.compat import approx
from tests.tools import bar_row, dates, entry_signal, make_bars, make_config, make_store

from aqs.config.schema import RiskConfig, construct
from aqs.core.enums import Side
from aqs.engine.backtest import BacktestEngine
from aqs.risk.base import NullRiskEngine
from aqs.risk.engine import RuleRiskEngine
from aqs.strategy.base import StrategyContext

D = dates(14)
SYMS = ["600000.SH", "600001.SH", "600002.SH"]
ENTRY_DAY = D[4]
CRASH_DAY = 6


def make_market(crash_pct: float = 0.0, *, crash_from: int = CRASH_DAY, dry_run: bool = False):
    rows = []
    for i, day in enumerate(D):
        price = 10.0
        if crash_pct and i >= crash_from and not dry_run:
            price = 10.0 * (1.0 - crash_pct)
        for sym in SYMS:
            rows.append(bar_row(day, sym, close=price, open_=price, prev_close=10.0, volume=1_000_000.0))
    return make_bars(rows)


class DatedStrategy:
    name = "dated"

    def __init__(self, entries=(ENTRY_DAY,)) -> None:
        self.entries = set(entries)

    def on_bar(self, ctx: StrategyContext):
        if ctx.date in self.entries:
            return [entry_signal(sym, ctx.date) for sym in SYMS[:2]]
        return []


class FixedPortfolio:
    """每笔买入按总资产的固定比例下单（便于观察 RMS 削减效果）。"""

    name = "fixed"

    def __init__(self, weight: float = 0.15) -> None:
        self.weight = weight

    def generate_orders(self, signals, ctx: StrategyContext) -> Sequence:
        from aqs.portfolio.base import OrderPlan

        plans = []
        for sig in signals:
            bar = ctx.snapshot.bar(sig.symbol)
            if bar is None or bar.close <= 0:
                continue
            budget = min(ctx.account.total_value * self.weight, ctx.account.available_cash() * 0.9)
            qty = int(budget / bar.close // 100) * 100
            if qty >= 100:
                plans.append(OrderPlan(sig.symbol, Side.BUY, qty, tag="entry"))
        return plans


def run_with_risk(rules: list[dict], *, crash: float = 0.0, portfolio_weight: float = 0.15, enabled: bool = True):
    config = make_config(
        {
            "data": {"quality": {"strict": False}},
            "universe": {"mode": "all"},
            "engine": {"start": D[0], "end": D[-1]},
        }
    )
    store = make_store(make_market(crash))
    risk_config = construct(
        RiskConfig, {"enabled": enabled, "mode": "enforce", "audit_log": None, "rules": rules}
    )
    risk = RuleRiskEngine(risk_config) if enabled else NullRiskEngine(risk_config)
    engine = BacktestEngine(
        store,
        config,
        strategy=DatedStrategy(),
        portfolio=FixedPortfolio(portfolio_weight),
        risk=risk,
    )
    return engine.run(), store, risk


# --------------------------------------------------------------------------- #
# I1 订单削减
# --------------------------------------------------------------------------- #
def test_max_order_quantity_reduces_fill():
    result, _, risk = run_with_risk(
        [
            {"name": "max_order_quantity", "priority": 10, "action": "reduce", "params": {"max_quantity": 1000}}
        ]
    )
    assert len(result.trades) == 2
    assert all(row.quantity <= 1000 for row in result.trades.itertuples())
    assert result.diagnostics["risk"]["stats"]["reduced"] >= 2


def test_single_name_weight_cap_enforced_by_rms():
    result, _, _ = run_with_risk(
        [
            {
                "name": "max_position_per_symbol",
                "priority": 50,
                "action": "reduce",
                "params": {"max_weight": 0.05},
            }
        ]
    )
    assert len(result.trades) == 2
    for row in result.trades.itertuples():
        assert row.gross_amount <= 0.05 * 1_000_000.0 * 1.02


def test_order_notional_limit():
    result, _, _ = run_with_risk(
        [
            {
                "name": "max_order_notional",
                "priority": 11,
                "action": "reduce",
                "params": {"max_notional": 50_000.0},
            }
        ]
    )
    assert len(result.trades) > 0
    for row in result.trades.itertuples():
        assert row.gross_amount <= 50_000.0 * 1.02


# --------------------------------------------------------------------------- #
# I2 当日亏损暂停
# --------------------------------------------------------------------------- #
def test_daily_loss_limit_pauses_trading():
    result, _, risk = run_with_risk(
        [
            {
                "name": "daily_loss_limit",
                "priority": 90,
                "action": "pause",
                "params": {"max_daily_loss": 0.03},
            },
            {"name": "gross_exposure", "priority": 70, "action": "reject", "params": {"allow_short": False}},
        ],
        crash=0.06,
        portfolio_weight=0.45,
    )
    assert risk.is_paused is True
    assert result.diagnostics["risk_paused"] is True
    assert result.diagnostics["risk"]["latency"]["by_action"].get("pause", 0) >= 1
    # 触发后不再开新仓：持仓数不超过触发前的数量
    assert result.equity_curve["n_positions"].max() <= 2


def test_daily_loss_limit_not_triggered_in_flat_market():
    result, _, risk = run_with_risk(
        [{"name": "daily_loss_limit", "priority": 90, "action": "pause", "params": {"max_daily_loss": 0.03}}]
    )
    assert risk.is_paused is False
    assert len(result.trades) == 2


# --------------------------------------------------------------------------- #
# I3 回撤强平 / I4 回撤减仓
# --------------------------------------------------------------------------- #
def test_max_drawdown_forces_liquidation():
    result, _, risk = run_with_risk(
        [
            {
                "name": "max_drawdown_action",
                "priority": 100,
                "action": "force_close",
                "params": {
                    "warn_drawdown": 0.02,
                    "reduce_drawdown": 0.04,
                    "force_close_drawdown": 0.05,
                    "reduce_exposure": 0.5,
                },
            },
            {"name": "gross_exposure", "priority": 70, "action": "reject", "params": {"allow_short": False}},
        ],
        crash=0.06,
        portfolio_weight=0.45,
    )
    assert risk.force_close_requested is True
    assert result.diagnostics["risk_force_close"] is True
    # 强平后持仓清零
    assert result.account.positions == {}
    assert any(row.tag == "risk_close" for row in result.trades.itertuples())
    # 强平后不再开新仓
    assert result.account.total_quantity(SYMS[0]) == 0.0


def test_max_drawdown_reduces_exposure():
    result, _, risk = run_with_risk(
        [
            {
                "name": "max_drawdown_action",
                "priority": 100,
                "action": "force_close",
                "params": {
                    "warn_drawdown": 0.01,
                    "reduce_drawdown": 0.03,
                    "force_close_drawdown": 0.90,
                    "reduce_exposure": 0.5,
                },
            },
            {"name": "gross_exposure", "priority": 70, "action": "reject", "params": {"allow_short": False}},
        ],
        crash=0.06,
        portfolio_weight=0.45,
    )
    assert risk.force_close_requested is False
    assert any(row.tag == "risk_reduce" for row in result.trades.itertuples())
    # 减仓后敞口从 ~90% 降至 ~50%（整手约束下会有少量偏差）
    exposure = result.equity_curve["gross_exposure"]
    peak = exposure.max()
    assert peak > 0.85
    assert exposure.iloc[-1] < 0.55
    assert exposure.iloc[-1] < peak * 0.65
    assert risk.target_exposure_scale() == approx(0.5)


# --------------------------------------------------------------------------- #
# I6/I7 诊断与关闭
# --------------------------------------------------------------------------- #
def test_diagnostics_contains_rms_metrics():
    result, _, _ = run_with_risk(
        [
            {
                "name": "max_order_quantity",
                "priority": 10,
                "action": "reduce",
                "params": {"max_quantity": 1000},
            }
        ]
    )
    diag = result.diagnostics
    assert diag["risk_engine"] == "RuleRiskEngine"
    assert diag["risk_enabled"] is True
    risk = diag["risk"]
    assert set(risk) >= {"rejection", "latency", "by_rule", "stats"}
    assert "false_reject_rate" in risk["rejection"]
    assert "mean_days" in risk["latency"]
    assert risk["stats"]["checked"] > 0
    assert diag["risk_rules"] == ["max_order_quantity"]


def test_risk_disabled_allows_everything():
    result, _, risk = run_with_risk(
        [
            {
                "name": "max_position_per_symbol",
                "priority": 50,
                "action": "reject",
                "params": {"max_weight": 0.01},
            }
        ],
        enabled=False,
    )
    assert isinstance(risk, NullRiskEngine)
    assert result.diagnostics["risk_engine"] == "NullRiskEngine"
    assert len(result.trades) == 2
    assert result.diagnostics["risk"]["rejection"]["n_rejections"] == 0


def test_risk_observe_mode_allows_but_records():
    config = make_config(
        {"data": {"quality": {"strict": False}}, "universe": {"mode": "all"}, "engine": {"start": D[0], "end": D[-1]}}
    )
    store = make_store(make_market())
    risk_config = construct(
        RiskConfig,
        {
            "enabled": True,
            "mode": "observe",
            "audit_log": None,
            "rules": [
                {
                    "name": "max_order_quantity",
                    "priority": 10,
                    "action": "reduce",
                    "params": {"max_quantity": 1000},
                }
            ],
        },
    )
    engine = BacktestEngine(
        store, config, strategy=DatedStrategy(), portfolio=FixedPortfolio(), risk=RuleRiskEngine(risk_config)
    )
    result = engine.run()
    # observe 模式放行，但记录了触发
    assert len(result.trades) == 2
    assert max(row.quantity for row in result.trades.itertuples()) > 1000
    assert result.diagnostics["risk"]["stats"]["skipped"] >= 1
