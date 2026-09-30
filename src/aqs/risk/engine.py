"""风控引擎（RMS）：规则优先级、短路、审计、热更新、预警、暂停/强平。

聚合逻辑（``docs/06_risk_rms.md`` §2.1）：

```
qty = order.remaining
for rule in sorted(rules, priority):
    d = rule.check(ctx)
    ALLOW  → continue
    REDUCE → qty = min(qty, d.modified_quantity)；若 qty<=0 则拒单；continue
    REJECT / PAUSE / FORCE_CLOSE → 记录并立即返回
return REDUCE(qty) if qty < order.remaining else ALLOW
```
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..config.schema import RiskConfig
from ..core.dates import DateLike
from ..core.enums import RiskAction, Side
from ..core.logging import get_logger
from ..core.models import MarketSnapshot, Order, RiskDecision
from .base import RiskContext, RiskEngine, RiskRule

__all__ = ["RuleRiskEngine", "RiskStats"]

logger = get_logger("risk.engine")


@dataclass(slots=True)
class RiskStats:
    """RMS 运行统计（用于验收：拒单率 / 误杀率 / 触发延迟）。"""

    checked: int = 0
    rejected: int = 0
    reduced: int = 0
    paused_events: int = 0
    force_close_events: int = 0
    skipped: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)

    @property
    def reject_rate(self) -> float:
        return self.rejected / self.checked if self.checked else 0.0

    @property
    def reduce_rate(self) -> float:
        return self.reduced / self.checked if self.checked else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "rejected": self.rejected,
            "reduced": self.reduced,
            "paused_events": self.paused_events,
            "force_close_events": self.force_close_events,
            "skipped": self.skipped,
            "reject_rate": self.reject_rate,
            "reduce_rate": self.reduce_rate,
            "by_rule": dict(self.by_rule),
        }


class RuleRiskEngine(RiskEngine):
    """基于规则集的风控引擎。"""

    def __init__(
        self,
        config: RiskConfig | Mapping[str, Any] | None = None,
        *,
        rules: Sequence[RiskRule] | None = None,
        industry_of: Callable[[str], str | None] | None = None,
        returns_of: Callable[[str, DateLike, int], Sequence[float]] | None = None,
        config_file: str | None = None,
    ) -> None:
        super().__init__(config)
        if rules is None:
            from .registry import build_rules

            rules = build_rules(self.config.rules)
        self.rules = sorted(rules, key=lambda r: r.priority)
        self.stats = RiskStats()
        self.config_file = config_file
        self.industry_of = industry_of
        self.returns_of = returns_of

    # ------------------------------------------------------------------ #
    # 核心
    # ------------------------------------------------------------------ #
    def check_order(self, order: Order, account: Any, snapshot: MarketSnapshot) -> RiskDecision:
        self.stats.checked += 1
        if not self.config.enabled:
            return RiskDecision.allow("risk_disabled")

        ctx = self.build_context(order, account, snapshot)

        if self._paused:
            return self._finalize(
                order,
                RiskDecision.pause("engine_paused", f"交易已暂停：{self._pause_reason}"),
                snapshot,
            )

        if self._force_close_requested and order.side is Side.BUY:
            return self._finalize(
                order,
                RiskDecision.reject("force_close", "已触发强制平仓，禁止开新仓"),
                snapshot,
            )

        quantity = order.remaining
        reduced_by: str | None = None
        reduce_message = ""

        for rule in self.rules:
            decision = rule.check(ctx)
            action = decision.action
            if action is RiskAction.ALLOW:
                if decision.message:
                    self.audit.log(
                        "risk", "note", ts=snapshot.date, rule=rule.name, message=decision.message,
                        order_id=order.order_id,
                    )
                continue

            if action is RiskAction.REDUCE:
                proposed = float(decision.modified_quantity or 0.0)
                new_quantity = max(0.0, min(quantity, proposed))
                self.stats.by_rule[rule.name] = self.stats.by_rule.get(rule.name, 0) + 1
                if new_quantity <= 0:
                    self.stats.rejected += 1
                    return self._finalize(
                        order,
                        RiskDecision.reject(
                            rule.name,
                            decision.message or "风控削减后数量为 0，拒单",
                            reason=decision.reject_reason,
                        ),
                        snapshot,
                    )
                if new_quantity < quantity:
                    quantity = new_quantity
                    reduced_by = rule.name
                    reduce_message = decision.message
                continue

            # reject / pause / force_close：立即短路
            self.stats.by_rule[rule.name] = self.stats.by_rule.get(rule.name, 0) + 1
            if action is RiskAction.PAUSE:
                self.stats.paused_events += 1
                self.pause(decision.message)
            elif action is RiskAction.FORCE_CLOSE:
                self.stats.force_close_events += 1
                self.request_force_close(decision.message)
            else:
                self.stats.rejected += 1
            return self._finalize(order, decision, snapshot)

        if reduced_by is not None and quantity < order.remaining:
            self.stats.reduced += 1
            return self._finalize(
                order,
                RiskDecision.reduce(reduced_by, quantity, reduce_message),
                snapshot,
            )
        return RiskDecision.allow("all_rules_passed")

    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # 组合级检查
    # ------------------------------------------------------------------ #
    def evaluate_controls(self, account: Any, snapshot: MarketSnapshot) -> list[RiskDecision]:
        """每个交易日独立评估**不依赖订单**的组合级规则（回撤、当日亏损、撤单率）。

        为什么需要它：这些规则必须在「当天没有任何订单」时也能触发，
        否则一个只在下单时才被调用的 RMS 会漏掉真正的账户级风险。
        """
        decisions: list[RiskDecision] = []
        if not self.config.enabled:
            return decisions
        ctx = self.build_context(None, account, snapshot)  # type: ignore[arg-type]
        for rule in self.rules:
            if not rule.portfolio_level:
                continue
            decision = rule.check(ctx)
            if decision.action is RiskAction.ALLOW:
                if decision.message:
                    self.audit.log(
                        "risk", "note", ts=snapshot.date, rule=rule.name, message=decision.message
                    )
                continue
            decisions.append(decision)
            self.audit.log(
                "risk",
                "portfolio_check",
                ts=snapshot.date,
                rule=rule.name,
                risk_action=decision.action.value,
                message=decision.message,
            )
            if decision.action is RiskAction.PAUSE:
                self.stats.paused_events += 1
                self.stats.by_rule[rule.name] = self.stats.by_rule.get(rule.name, 0) + 1
                self.pause(decision.message)
                self._record_trigger(decision, None, snapshot)
                break
            if decision.action is RiskAction.FORCE_CLOSE:
                self.stats.force_close_events += 1
                self.stats.by_rule[rule.name] = self.stats.by_rule.get(rule.name, 0) + 1
                self.request_force_close(decision.message)
                self._record_trigger(decision, None, snapshot)
                break
        return decisions

    def build_context(self, order: Order | None, account: Any, snapshot: MarketSnapshot) -> RiskContext:
        return RiskContext(
            order=order,
            account=account,
            snapshot=snapshot,
            day=self.day,
            engine=self.engine_state,
            industry_of=self.industry_of,
            returns_of=self.returns_of,
        )

    def _finalize(self, order: Order, decision: RiskDecision, snapshot: MarketSnapshot) -> RiskDecision:
        """审计 + 触发记录 + observe 模式处理。"""
        self.audit.log(
            "risk",
            "decision",
            ts=snapshot.date,
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side.value,
            quantity=order.quantity,
            risk_action=decision.action.value,
            rule=decision.rule,
            modified_quantity=decision.modified_quantity,
            message=decision.message,
        )
        if decision.action is not RiskAction.ALLOW:
            self._record_trigger(decision, order, snapshot)
        if self.config.mode == "observe" and decision.action is not RiskAction.ALLOW:
            self.stats.skipped += 1
            logger.debug("observe 模式：规则 %s 触发但放行（%s）", decision.rule, decision.message)
            return RiskDecision.allow(f"observe:{decision.rule}")
        return decision

    # ------------------------------------------------------------------ #
    def update_rules(self, rules: Sequence[RiskRule]) -> None:
        super().update_rules(sorted(rules, key=lambda r: r.priority))

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.config.mode,
            "enabled": self.config.enabled,
            "rules": [
                {"name": r.name, "priority": r.priority, "action": r.action, "params": dict(r.params)}
                for r in self.rules
            ],
            "paused": self.is_paused,
            "pause_reason": self.pause_reason,
            "force_close": self.force_close_requested,
            "exposure_scale": self.target_exposure_scale(),
            "alerts": len(self._alerts),
            "stats": self.stats.as_dict(),
        }
