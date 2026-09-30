"""回归缺陷 #7：``NullRiskEngine`` 与 ``RuleRiskEngine`` 的 stats 口径不一致。

原缺陷：``RiskStats`` 只定义在 ``RuleRiskEngine`` 上，``NullRiskEngine`` 没有；
``_diagnostics`` 对二者输出结构不同（``{"checked": 0}`` vs 完整字典），
下游报告无法统一消费，且「未启用风控」与「风控放行」在诊断里无法区分。

修复后：
- ``RiskStats`` 上移到 ``risk/base.py``，并新增 ``enabled`` 字段；
- ``NullRiskEngine.check_order`` 也累计 ``checked``；
- 两种模式的诊断字段集合**完全一致**。
"""

from __future__ import annotations

from typing import Sequence

from tests.compat import approx
from tests.tools import DEFAULT_START, bar_row, dates, entry_signal, make_bars, make_config, make_store

from aqs.config.schema import RiskConfig
from aqs.core.enums import Side
from aqs.engine.backtest import BacktestEngine
from aqs.portfolio.base import OrderPlan
from aqs.risk import NullRiskEngine, RiskStats, RuleRiskEngine
from aqs.strategy.base import StrategyContext

D = dates(12)
SYMS = ["600000.SH", "600001.SH"]
ENTRY_DAY = D[4]

EXPECTED_KEYS = {
    "enabled",
    "checked",
    "rejected",
    "reduced",
    "below_lot",  # 缺陷 #13：削减后不足一手（不计入拒单）
    "paused_events",
    "force_close_events",
    "skipped",
    "reject_rate",
    "reduce_rate",
    "by_rule",
}


class DatedStrategy:
    name = "dated"

    def on_bar(self, ctx: StrategyContext):
        return [entry_signal(s, ctx.date) for s in SYMS] if ctx.date == ENTRY_DAY else []


class TwoNamePortfolio:
    name = "two_name"

    def generate_orders(self, signals, ctx: StrategyContext) -> Sequence[OrderPlan]:
        plans = []
        for sig in signals:
            bar = ctx.snapshot.bar(sig.symbol)
            if bar is None or bar.close <= 0:
                continue
            qty = int(ctx.account.total_value * 0.10 / bar.close // 100) * 100
            if qty >= 100:
                plans.append(OrderPlan(sig.symbol, Side.BUY, qty, tag="entry"))
        return plans


def make_market():
    rows = []
    for day in D:
        for sym in SYMS:
            rows.append(bar_row(day, sym, close=10.0, open_=10.0, prev_close=10.0, volume=1_000_000.0))
    return make_bars(rows)


def run(enabled: bool):
    config = make_config(
        {
            "data": {"quality": {"strict": False}},
            "universe": {"mode": "all"},
            "engine": {"start": D[0], "end": D[-1]},
            "risk": {"enabled": enabled},
        }
    )
    store = make_store(make_market())
    engine = BacktestEngine(store, config, strategy=DatedStrategy(), portfolio=TwoNamePortfolio())
    return engine.run()


# --------------------------------------------------------------------------- #
def test_stats_dict_schema_is_identical_when_disabled():
    disabled = run(False)["risk"]["stats"] if False else run(False).diagnostics["risk"]["stats"]
    enabled = run(True).diagnostics["risk"]["stats"]
    assert set(disabled) == EXPECTED_KEYS
    assert set(enabled) == EXPECTED_KEYS


def test_disabled_risk_counts_checks_but_no_rejections():
    result = run(False)
    stats = result.diagnostics["risk"]["stats"]
    assert stats["enabled"] is False
    assert stats["checked"] >= result.broker.stats.created, "关闭风控时仍应统计被询问的订单数"
    assert stats["checked"] > 0
    assert stats["rejected"] == 0
    assert stats["reduced"] == 0
    assert stats["reject_rate"] == approx(0.0)
    assert stats["reduce_rate"] == approx(0.0)
    assert result.diagnostics["risk_engine"] == "NullRiskEngine"


def test_enabled_risk_reports_enabled_flag():
    result = run(True)
    stats = result.diagnostics["risk"]["stats"]
    assert stats["enabled"] is True
    assert result.diagnostics["risk_engine"] == "RuleRiskEngine"
    assert stats["checked"] > 0


def test_reject_rate_definition_is_documented_by_behaviour():
    """reject_rate = rejected / checked；关闭风控时为 0，但 checked 不为 0。"""
    disabled = run(False).diagnostics["risk"]["stats"]
    assert disabled["reject_rate"] == approx(disabled["rejected"] / disabled["checked"])
    assert disabled["checked"] > 0
    assert disabled["reject_rate"] == approx(0.0)


def test_null_risk_engine_increments_checked_directly():
    from tests.tools import make_bar, make_order, make_risk_context

    engine = NullRiskEngine(RiskConfig(enabled=False))
    for _ in range(3):
        ctx = make_risk_context(order=make_order(SYMS[0], Side.BUY, 100), bars=[make_bar(SYMS[0], D[0], close=10.0)])
        assert engine.check_order(ctx.order, ctx.account, ctx.snapshot).accept
    assert engine.stats.checked == 3
    assert engine.stats.rejected == 0
    assert engine.stats.enabled is False


def test_risk_stats_default_is_enabled_true():
    assert RiskStats().enabled is True
    assert RiskStats().as_dict()["enabled"] is True


def test_rule_engine_stats_enabled_mirrors_config():
    assert RuleRiskEngine(RiskConfig(enabled=True, rules=[])).stats.enabled is True
    assert RuleRiskEngine(RiskConfig(enabled=False, rules=[])).stats.enabled is False


def test_diagnostics_risk_section_has_stable_keys():
    enabled = run(True).diagnostics["risk"]
    disabled = run(False).diagnostics["risk"]
    assert set(enabled) == set(disabled)
    for key in ("rejection", "latency", "stats", "by_rule", "exposure_control", "audit_records"):
        assert key in enabled
        assert key in disabled
