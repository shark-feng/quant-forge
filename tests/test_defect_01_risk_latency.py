"""回归缺陷 #1：风控触发延迟（latency_days）计算错误。

原缺陷：``pause/force_close`` 分支把 ``acted_on`` 置为 ``None``，
导致 ``latency_days`` 恒为 0，``acted_on`` 永远为空。

修复后口径：
- 订单级动作（reject / reduce）：``acted_on = 触发日``，``latency_days = 0``；
- 控制类动作（pause / force_close）：``acted_on = 下一个交易日``，``latency_days = 1``；
- 未绑定交易日历时退化为「+1 自然日」并把 ``latency_approx`` 置 True（不静默近似）。
"""

from __future__ import annotations

from datetime import timedelta

from tests.compat import approx
from tests.tools import DEFAULT_START, dates, make_bar, make_order, make_risk_context

from aqs.config.schema import RiskConfig
from aqs.core.enums import RiskAction
from aqs.core.models import RiskDecision
from aqs.risk.base import NullRiskEngine, RiskEngine, RiskTrigger
from aqs.risk.engine import RuleRiskEngine
from aqs.risk.stats import summarize_latency

D = dates(8)  # 2022-03-01 起的工作日序列（含周末断点）
FRIDAY = next(day for day in D if day.weekday() == 4)  # 该序列中的第一个周五


def callable_next_trading_day(day):
    """模拟交易日历：严格返回 ``day`` 之后的第一个交易日。"""
    for candidate in D:
        if candidate > day:
            return candidate
    return None


def make_engine(*, bind: bool = True) -> RuleRiskEngine:
    engine = RuleRiskEngine(RiskConfig(enabled=True, rules=[]))
    if bind:
        engine.bind_calendar(callable_next_trading_day)
    return engine


def record(engine: RiskEngine, decision: RiskDecision, day, order=None) -> RiskTrigger:
    ctx = make_risk_context(order=order or make_order(), bars=[make_bar(day=day)], day=day)
    engine._record_trigger(decision, order, ctx.snapshot)  # noqa: SLF001 - 直接验证记录的字段
    return engine.triggers[-1]


# --------------------------------------------------------------------------- #
def test_order_level_action_has_zero_latency():
    engine = make_engine()
    trigger = record(engine, RiskDecision.reject("rule_x", "命中"), D[1])
    assert trigger.action == "reject"
    assert trigger.triggered_on == D[1]
    assert trigger.acted_on == D[1]
    assert trigger.latency_days == 0
    assert trigger.latency_approx is False


def test_reduce_action_has_zero_latency():
    engine = make_engine()
    trigger = record(engine, RiskDecision.reduce("rule_y", 100, "削减"), D[1])
    assert trigger.acted_on == D[1]
    assert trigger.latency_days == 0


def test_pause_action_acts_next_trading_day():
    engine = make_engine()
    trigger = record(engine, RiskDecision.pause("daily_loss_limit", "当日亏损超限"), D[1])
    assert trigger.action == "pause"
    assert trigger.acted_on == D[2]
    assert trigger.latency_days == 1
    assert trigger.latency_approx is False


def test_force_close_action_acts_next_trading_day():
    engine = make_engine()
    trigger = record(engine, RiskDecision.force_close("max_drawdown_action", "回撤超限"), D[0])
    assert trigger.acted_on == D[1]
    assert trigger.latency_days == 1


def test_latency_uses_trading_days_not_calendar_days():
    """周五触发 → 生效日为下周一（跨周末仍计 1 个交易日）。"""
    engine = make_engine()
    trigger = record(engine, RiskDecision.pause("p", "周五触发"), FRIDAY)
    assert FRIDAY.weekday() == 4
    assert trigger.triggered_on == FRIDAY
    assert trigger.acted_on is not None
    assert trigger.acted_on.weekday() == 0                 # 下周一
    assert (trigger.acted_on - trigger.triggered_on).days >= 3   # 跨了周末
    assert trigger.latency_days == 1                             # 交易日只差 1 天


def test_latency_without_calendar_is_marked_approx():
    engine = make_engine(bind=False)
    trigger = record(engine, RiskDecision.pause("p", "无日历"), D[1])
    assert trigger.acted_on == D[1] + timedelta(days=1)  # 退化为 +1 自然日
    assert trigger.latency_days == 1
    assert trigger.latency_approx is True


def test_latency_summary_reports_approx_count():
    engine = make_engine(bind=False)
    record(engine, RiskDecision.pause("p", "无日历"), D[1])
    report = summarize_latency(engine.triggers)
    assert report.n_triggers == 1
    assert report.n_approx == 1
    assert report.approx_ratio == approx(1.0)
    assert report.by_action["pause"] == 1


def test_trigger_dict_exposes_latency_fields():
    engine = make_engine()
    trigger = record(engine, RiskDecision.pause("p", "x"), D[1])
    payload = trigger.as_dict()
    assert payload["latency_days"] == 1
    assert payload["acted_on"] == D[2]
    assert payload["latency_approx"] is False


def test_null_risk_engine_has_no_triggers():
    engine = NullRiskEngine(RiskConfig(enabled=False))
    assert engine.triggers == []
    assert summarize_latency(engine.triggers).n_triggers == 0


def test_mixed_triggers_summary():
    engine = make_engine()
    record(engine, RiskDecision.reject("r1", "x"), D[0])
    record(engine, RiskDecision.pause("r2", "y"), D[1])
    record(engine, RiskDecision.force_close("r3", "z"), D[2])
    report = summarize_latency(engine.triggers)
    assert report.n_triggers == 3
    assert report.mean_days == approx(2 / 3)
    assert report.max_days == 1
    assert report.by_action == {"reject": 1, "pause": 1, "force_close": 1}
    assert report.n_approx == 0
