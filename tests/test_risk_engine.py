"""风控引擎测试：优先级、短路、reduce 累积、暂停、强平、热更新、预警、observe 模式。"""

from __future__ import annotations

from typing import Any

from tests.compat import approx, raises
from tests.tools import DEFAULT_START, make_bar, make_order, make_risk_context

from aqs.config.schema import RiskConfig, RiskRuleConfig, construct
from aqs.core.enums import RiskAction, Side
from aqs.core.exceptions import ConfigError, RiskError
from aqs.core.models import MarketSnapshot, Order, RiskDecision
from aqs.engine.account import Account
from aqs.risk.base import RiskContext, RiskRule
from aqs.risk.engine import RuleRiskEngine
from aqs.risk.registry import build_rule, build_rules, get_rule_class

DAY = DEFAULT_START
SYM = "600000.SH"


class AlwaysRule(RiskRule):
    """测试用规则：返回固定决策并记录调用次数。"""

    def __init__(self, name: str, decision: RiskDecision, priority: int = 50, calls: list | None = None) -> None:
        super().__init__({}, action=decision.action.value)
        self.name = name
        self.priority = priority
        self._decision = decision
        self.calls = calls if calls is not None else []

    def check(self, ctx: RiskContext) -> RiskDecision:
        self.calls.append(self.name)
        return self._decision


def make_engine(rules: list[RiskRule], *, mode: str = "enforce") -> RuleRiskEngine:
    config = RiskConfig(mode=mode, rules=[])
    return RuleRiskEngine(config, rules=rules)


def check(engine: RuleRiskEngine, *, side: Side = Side.BUY, quantity: float = 1000) -> RiskDecision:
    account = Account(1_000_000.0)
    bars = [make_bar(SYM, DAY, close=10.0)]
    ctx = make_risk_context(order=make_order(SYM, side, quantity), account=account, bars=bars)
    return engine.check_order(ctx.order, account, ctx.snapshot)


# --------------------------------------------------------------------------- #
# 聚合逻辑
# --------------------------------------------------------------------------- #
def test_priority_order_is_respected():
    calls: list[str] = []
    rules = [
        AlwaysRule("late", RiskDecision.allow(), priority=90, calls=calls),
        AlwaysRule("early", RiskDecision.allow(), priority=10, calls=calls),
        AlwaysRule("middle", RiskDecision.allow(), priority=50, calls=calls),
    ]
    engine = make_engine(rules)
    check(engine)
    assert calls == ["early", "middle", "late"]
    assert [r.name for r in engine.rules] == ["early", "middle", "late"]


def test_reject_short_circuits():
    calls: list[str] = []
    rules = [
        AlwaysRule("blocker", RiskDecision.reject("blocker", "命中"), priority=10, calls=calls),
        AlwaysRule("after", RiskDecision.allow(), priority=20, calls=calls),
    ]
    engine = make_engine(rules)
    decision = check(engine)
    assert decision.action is RiskAction.REJECT
    assert decision.rule == "blocker"
    assert calls == ["blocker"]  # 后续规则未执行


def test_reduce_accumulates_to_minimum():
    rules = [
        AlwaysRule("r1", RiskDecision.reduce("r1", 800), priority=10),
        AlwaysRule("r2", RiskDecision.reduce("r2", 500), priority=20),
    ]
    engine = make_engine(rules)
    decision = check(engine, quantity=1000)
    assert decision.action is RiskAction.REDUCE
    assert decision.modified_quantity == approx(500)
    assert decision.rule == "r2"  # 记为最严格的那条


def test_reduce_to_zero_becomes_reject():
    rules = [AlwaysRule("r1", RiskDecision.reduce("r1", 0), priority=10)]
    engine = make_engine(rules)
    decision = check(engine)
    assert decision.action is RiskAction.REJECT
    assert "削减后数量为 0" in decision.message


def test_pause_stops_further_orders():
    rules = [AlwaysRule("p", RiskDecision.pause("p", "当日亏损超限"), priority=10)]
    engine = make_engine(rules)
    decision = check(engine)
    assert decision.action is RiskAction.PAUSE
    assert engine.is_paused is True
    assert "当日亏损超限" in engine.pause_reason
    # 暂停后一律暂停
    second = check(engine)
    assert second.action is RiskAction.PAUSE
    assert second.rule == "engine_paused"
    # 恢复
    engine.resume()
    assert engine.is_paused is False


def test_force_close_sets_zero_exposure():
    rules = [AlwaysRule("fc", RiskDecision.force_close("fc", "回撤超限"), priority=10)]
    engine = make_engine(rules)
    decision = check(engine)
    assert decision.action is RiskAction.FORCE_CLOSE
    assert engine.force_close_requested is True
    assert engine.target_exposure_scale() == approx(0.0)
    # 移除触发规则后，仅剩引擎级的强平保护：禁止开新仓、允许卖出
    engine.update_rules([])
    blocked = check(engine, side=Side.BUY)
    assert blocked.action is RiskAction.REJECT
    assert blocked.rule == "force_close"
    allowed = check(engine, side=Side.SELL, quantity=100)
    assert allowed.action is RiskAction.ALLOW
    engine.clear_force_close()
    assert engine.target_exposure_scale() == approx(1.0)
    assert check(engine, side=Side.BUY).action is RiskAction.ALLOW


def test_exposure_scale_from_drawdown():
    rules = [build_rule(RiskRuleConfig(name="max_drawdown_action", priority=100, action="force_close", params={}))]
    engine = make_engine(rules)
    account = Account(1_000_000.0)
    bars = [make_bar(SYM, DAY, close=10.0)]
    engine.on_day_start(DAY, account)
    engine.engine_state.update_value(1_000_000.0)
    engine.engine_state.update_value(880_000.0)  # -12%
    check(engine)
    assert engine.target_exposure_scale() == approx(1.0)  # 仅预警
    engine.engine_state.update_value(840_000.0)  # -16%
    check(engine)
    assert engine.target_exposure_scale() == approx(0.5)
    engine.engine_state.update_value(750_000.0)  # -25%
    check(engine)
    assert engine.target_exposure_scale() == approx(0.0)


def test_empty_rule_set_allows_everything():
    engine = make_engine([])
    assert check(engine).action is RiskAction.ALLOW


# --------------------------------------------------------------------------- #
# 审计 / 预警 / 热更新 / observe
# --------------------------------------------------------------------------- #
def test_audit_log_records_decisions():
    rules = [
        AlwaysRule("r1", RiskDecision.reduce("r1", 500), priority=10),
        AlwaysRule("p", RiskDecision.pause("p", "暂停原因"), priority=20),
    ]
    engine = make_engine(rules)
    check(engine)
    decisions = [r for r in engine.audit.records if r.category == "risk"]
    assert decisions
    payload = decisions[-1].payload
    assert payload["risk_action"] in ("reduce", "pause")
    assert payload["rule"] in ("r1", "p")
    controls = engine.audit.filter(category="control")
    assert any(r.action == "pause" for r in controls)


def test_alerts_are_generated_and_drainable():
    rules = [AlwaysRule("fc", RiskDecision.force_close("fc", "触发强平"), priority=10)]
    engine = make_engine(rules)
    check(engine)
    alerts = engine.alerts()
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "force_close"
    assert engine.drain_alerts() and engine.alerts() == []


def test_hot_reload_replaces_rules():
    engine = make_engine([AlwaysRule("old", RiskDecision.reject("old", "旧规则"), priority=10)])
    assert check(engine).action is RiskAction.REJECT
    engine.update_rules([AlwaysRule("new", RiskDecision.allow(), priority=10)])
    assert check(engine).action is RiskAction.ALLOW
    assert [r.name for r in engine.rules] == ["new"]
    assert any(r.action == "rules_updated" for r in engine.audit.records)


def test_observe_mode_records_but_allows():
    rules = [
        AlwaysRule("blocker", RiskDecision.reject("blocker", "命中"), priority=10),
        AlwaysRule("p", RiskDecision.pause("p", "暂停"), priority=20),
    ]
    engine = make_engine(rules, mode="observe")
    decision = check(engine)
    assert decision.action is RiskAction.ALLOW
    assert decision.rule.startswith("observe:")
    assert engine.stats.skipped == 1
    # 即使 observe，暂停动作也会被记录到 stats（但订单放行）
    assert engine.stats.by_rule["blocker"] == 1


def test_disabled_engine_allows():
    config = RiskConfig(enabled=False)
    engine = RuleRiskEngine(config, rules=[AlwaysRule("x", RiskDecision.reject("x", "命中"))])
    assert check(engine).action is RiskAction.ALLOW


def test_stats_counting():
    rules = [
        AlwaysRule("r", RiskDecision.reduce("r", 500), priority=10),
    ]
    engine = make_engine(rules)
    check(engine, quantity=1000)
    stats = engine.stats
    assert stats.checked == 1
    assert stats.reduced == 1
    assert stats.reject_rate == approx(0.0)
    assert stats.by_rule["r"] == 1
    assert "reject_rate" in stats.as_dict()


def test_describe_outputs_configuration():
    engine = make_engine([AlwaysRule("r", RiskDecision.allow(), priority=10)])
    info = engine.describe()
    assert info["mode"] == "enforce"
    assert info["rules"][0]["name"] == "r"
    assert info["paused"] is False
    assert "stats" in info


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
def test_registry_builds_all_config_rules():
    from aqs.config.loader import load_risk_config

    config = load_risk_config("configs/risk.yaml")
    rules = build_rules(config.rules)
    assert len(rules) == len(config.rules)
    names = [r.name for r in rules]
    assert "max_drawdown_action" in names
    assert "portfolio_var_limit" in names
    priorities = [r.priority for r in rules]
    assert priorities == sorted(priorities)


def test_unknown_rule_rejected():
    with raises(ConfigError) as exc:
        get_rule_class("magic_rule")
    assert "magic_rule" in str(exc.value)
    with raises(ConfigError):
        build_rule(RiskRuleConfig(name="magic_rule"))


def test_invalid_action_rejected():
    class Dummy(RiskRule):
        name = "dummy"

        def check(self, ctx: RiskContext) -> RiskDecision:  # pragma: no cover - 仅用于构造
            return RiskDecision.allow()

    with raises(RiskError):
        Dummy({}, action="explode")


def test_engine_wires_config_file():
    engine = RuleRiskEngine(
        construct(RiskConfig, {"mode": "enforce", "rules": []}), rules=[], config_file="configs/risk.yaml"
    )
    assert engine.config_file == "configs/risk.yaml"
    engine.reload_rules()
    assert len(engine.rules) >= 10
