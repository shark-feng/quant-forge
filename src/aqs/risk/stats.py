"""RMS 验收指标：拒单率、误杀率、触发延迟（合同 §九）。

- **拒单率** = 被拒订单 / 被检查订单  —— 直接来自 :class:`~aqs.risk.engine.RiskStats`；
- **误杀率** = 被拒订单中「事后 N 个交易日按原方向为盈利」的比例 —— 需要事后行情，
  因此由 :func:`evaluate_rejections` 在回测结束后用数据仓库评估；
- **触发延迟** = 规则条件首次满足日 → 动作生效日 的交易日数 —— 由引擎在
  :class:`~aqs.risk.base.RiskTrigger` 中记录（日频语义下，控制类动作延迟 1 个交易日）。

**误杀率的口径必须写进报告**：这是事后视角的近似指标，不代表风控当时做错了
（当时的信息集不同），只用于评估阈值是否过严。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Iterable, Sequence

from ..core.dates import DateLike, to_date
from ..core.enums import Side
from ..core.logging import get_logger
from .base import RiskTrigger

__all__ = ["RejectionRecord", "RejectionReport", "LatencyReport", "evaluate_rejections", "summarize_latency"]

logger = get_logger("risk.stats")


@dataclass(frozen=True, slots=True)
class RejectionRecord:
    """一笔被风控拒绝（或削减）的订单记录。"""

    order_id: str
    symbol: str
    side: Side
    quantity: float
    price: float
    day: _date
    rule: str
    action: str = "reject"

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "price": self.price,
            "day": self.day,
            "rule": self.rule,
            "action": self.action,
        }


@dataclass(slots=True)
class RejectionReport:
    """误杀率评估结果。"""

    n_rejections: int = 0
    n_evaluated: int = 0
    n_false: int = 0
    horizon: int = 5
    by_rule: dict[str, int] = field(default_factory=dict)
    details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def false_reject_rate(self) -> float:
        """误杀率 = 误杀数 / 可评估的被拒订单数。"""
        return self.n_false / self.n_evaluated if self.n_evaluated else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_rejections": self.n_rejections,
            "n_evaluated": self.n_evaluated,
            "n_false": self.n_false,
            "false_reject_rate": self.false_reject_rate,
            "horizon_days": self.horizon,
            "by_rule": dict(self.by_rule),
        }


@dataclass(slots=True)
class LatencyReport:
    """触发延迟统计（单位：交易日）。

    ``n_approx`` 为「未绑定交易日历、退化为 +1 自然日」的条目数；
    > 0 时说明延迟口径为近似值，报告中必须标注（缺陷修复 #1）。
    """

    n_triggers: int = 0
    by_action: dict[str, int] = field(default_factory=dict)
    by_rule: dict[str, int] = field(default_factory=dict)
    latencies: list[int] = field(default_factory=list)
    n_approx: int = 0

    @property
    def mean_days(self) -> float:
        return sum(self.latencies) / len(self.latencies) if self.latencies else 0.0

    @property
    def max_days(self) -> int:
        return max(self.latencies) if self.latencies else 0

    @property
    def approx_ratio(self) -> float:
        return self.n_approx / self.n_triggers if self.n_triggers else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_triggers": self.n_triggers,
            "mean_days": self.mean_days,
            "max_days": self.max_days,
            "n_approx": self.n_approx,
            "approx_ratio": self.approx_ratio,
            "by_action": dict(self.by_action),
            "by_rule": dict(self.by_rule),
        }


def evaluate_rejections(
    records: Sequence[RejectionRecord],
    store: Any,
    *,
    horizon: int = 5,
) -> RejectionReport:
    """用事后行情评估被拒订单是否为「误杀」。

    判定：以被拒订单当日收盘价为基准，取 ``horizon`` 个交易日后的收盘价：
    - 被拒买单若后续上涨 → 该次拦截使组合错失收益，记为误杀；
    - 被拒卖单若后续下跌 → 该次拦截使组合避免了损失… 反过来看，
      若被拒卖单后续上涨，说明「当时拦下是正确的」→ 不算误杀。
    """
    report = RejectionReport(n_rejections=len(records), horizon=horizon)
    for record in records:
        report.by_rule[record.rule] = report.by_rule.get(record.rule, 0) + 1
        entry = store.bar(record.symbol, record.day)
        forward = store.calendar.next_trading_day(record.day, horizon) if horizon > 0 else record.day
        if entry is None or forward is None:
            continue
        exit_bar = store.bar(record.symbol, forward)
        if exit_bar is None or entry.close <= 0:
            continue
        ret = exit_bar.close / entry.close - 1.0
        missed = (record.side is Side.BUY and ret > 0) or (record.side is Side.SELL and ret < 0)
        report.n_evaluated += 1
        if missed:
            report.n_false += 1
        report.details.append(
            {
                "order_id": record.order_id,
                "symbol": record.symbol,
                "side": record.side.value,
                "rule": record.rule,
                "day": record.day,
                "forward_return": ret,
                "missed_opportunity": missed,
            }
        )
    return report


def summarize_latency(triggers: Iterable[RiskTrigger]) -> LatencyReport:
    """汇总触发延迟（含近似条目计数）。"""
    report = LatencyReport()
    for trigger in triggers:
        report.n_triggers += 1
        report.latencies.append(int(trigger.latency_days))
        if getattr(trigger, "latency_approx", False):
            report.n_approx += 1
        report.by_action[trigger.action] = report.by_action.get(trigger.action, 0) + 1
        report.by_rule[trigger.rule] = report.by_rule.get(trigger.rule, 0) + 1
    return report
