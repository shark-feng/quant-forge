"""风控 RMS 接口与基础设施。

合同要求的核心接口：

```python
RiskDecision CheckOrder(Order, Account, MarketSnapshot)
```

返回 ``accept`` / ``reject_reason`` / ``modified_quantity`` / ``action``
（allow / reject / reduce / pause / force_close）。

除订单级检查外，RMS 还需要三类上下文（否则规则无法落地）：

1. **日内状态** :class:`DayState` —— 当日下单次数、当日撤单率、当日累计成交量、当日净值基准；
2. **引擎状态** :class:`EngineState` —— 峰值净值、回撤、敞口缩放、暂停/强平标志；
3. **外部数据** —— 行业映射（行业暴露规则）、历史收益提供器（VaR 规则）。

RMS **不直接交易**：暂停/减仓/强平通过状态与 :meth:`RiskEngine.target_exposure_scale`
交给引擎执行（日频语义下「立即」= 次日开盘）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Callable, Mapping, Sequence

from ..config.schema import RiskConfig, RiskRuleConfig, as_config
from ..core.dates import DateLike, to_date
from ..core.enums import RiskAction
from ..core.exceptions import RiskError
from ..core.logging import AuditStream, get_logger
from ..core.models import MarketSnapshot, Order, RiskDecision

__all__ = [
    "RiskContext",
    "DayState",
    "EngineState",
    "RiskRule",
    "RiskTrigger",
    "RiskEngine",
    "NullRiskEngine",
]

logger = get_logger("risk.base")


# --------------------------------------------------------------------------- #
# 上下文
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class DayState:
    """单调递增的日内统计（每个交易日重置）。"""

    day: _date | None = None
    start_value: float = 0.0
    orders_submitted: int = 0
    orders_cancelled: int = 0
    orders_rejected: int = 0
    traded_quantity: dict[str, float] = field(default_factory=dict)
    traded_notional: float = 0.0

    def reset(self, day: DateLike, start_value: float) -> None:
        self.day = to_date(day)
        self.start_value = float(start_value)
        self.orders_submitted = 0
        self.orders_cancelled = 0
        self.orders_rejected = 0
        self.traded_quantity.clear()
        self.traded_notional = 0.0

    @property
    def order_count(self) -> int:
        return self.orders_submitted

    @property
    def cancel_ratio(self) -> float:
        if self.orders_submitted <= 0:
            return 0.0
        return self.orders_cancelled / self.orders_submitted

    def traded_on(self, symbol: str) -> float:
        return self.traded_quantity.get(symbol, 0.0)

    def add_trade(self, symbol: str, quantity: float, notional: float = 0.0) -> None:
        self.traded_quantity[symbol] = self.traded_quantity.get(symbol, 0.0) + quantity
        self.traded_notional += notional


@dataclass(slots=True)
class EngineState:
    """组合级状态（跨日维持）。"""

    peak_value: float = 0.0
    current_value: float = 0.0
    exposure_scale: float = 1.0
    paused: bool = False
    force_close: bool = False
    drawdown: float = 0.0

    def update_value(self, value: float) -> None:
        self.current_value = float(value)
        self.peak_value = max(self.peak_value, float(value))
        self.drawdown = (float(value) / self.peak_value - 1.0) if self.peak_value > 0 else 0.0


@dataclass(slots=True)
class RiskContext:
    """一次风控检查的完整上下文（``order`` 为空表示组合级检查）。"""

    order: Order | None
    account: Any
    snapshot: MarketSnapshot
    day: DayState
    engine: EngineState
    industry_of: Callable[[str], str | None] | None = None
    returns_of: Callable[[str, DateLike, int], Sequence[float]] | None = None

    @property
    def symbol(self) -> str:
        return self.order.symbol if self.order is not None else ""

    # ------------------------------ 便捷查询 ------------------------------ #
    def price(self, symbol: str | None = None) -> float:
        target = symbol or self.symbol
        if not target:
            return 0.0
        bar = self.snapshot.bar(target)
        if bar is not None and bar.close > 0:
            return float(bar.close)
        return 0.0

    @property
    def total_value(self) -> float:
        return float(getattr(self.account, "total_value", 0.0))

    @property
    def cash(self) -> float:
        return float(getattr(self.account, "available_cash", lambda: 0.0)())

    def position_value(self, symbol: str) -> float:
        pos = getattr(self.account, "positions", {}).get(symbol)
        return float(pos.market_value) if pos is not None else 0.0

    def industry(self, symbol: str) -> str | None:
        if self.industry_of is None:
            return None
        return self.industry_of(symbol)

    def returns(self, symbol: str, window: int = 60) -> Sequence[float] | None:
        if self.returns_of is None:
            return None
        try:
            return self.returns_of(symbol, self.snapshot.date, window)
        except Exception:  # noqa: BLE001 - 数据缺失不应让风控崩掉
            logger.debug("风控取历史收益失败：%s", symbol, exc_info=True)
            return None


# --------------------------------------------------------------------------- #
# 规则
# --------------------------------------------------------------------------- #
class RiskRule(ABC):
    """单条风控规则。

    要求：
    - 只读上下文，不修改订单（削减数量通过 ``RiskDecision.modified_quantity`` 表达）；
    - 返回 ``allow`` 表示本规则不拦；
    - 具备唯一 ``name`` 与 ``priority``（数值越小越先执行）。
    """

    name: str = "abstract"
    priority: int = 100
    natural_action: str = "reject"
    """规则的自然动作；配置中的 ``action`` 可在允许范围内覆盖它。"""

    portfolio_level: bool = False
    """``True`` 表示该规则不依赖具体订单，每个交易日都会单独评估（回撤、当日亏损、撤单率等）。"""

    def __init__(self, params: Mapping[str, Any] | None = None, *, action: str | None = None) -> None:
        self.params: dict[str, Any] = dict(params or {})
        self.action = action or self.natural_action
        if self.action not in ("allow", "reject", "reduce", "pause", "force_close"):
            raise RiskError(f"规则 {self.name} 的 action 非法：{self.action}")

    @abstractmethod
    def check(self, ctx: RiskContext) -> RiskDecision:  # pragma: no cover - 抽象方法
        raise NotImplementedError

    # ------------------------------ 工具 ------------------------------ #
    @classmethod
    def from_config(cls, config: RiskRuleConfig) -> "RiskRule":
        from .registry import build_rule

        return build_rule(config)

    def allow(self, note: str = "") -> RiskDecision:
        return RiskDecision(action=RiskAction.ALLOW, rule=self.name, message=note)

    def reject(self, message: str) -> RiskDecision:
        return RiskDecision.reject(self.name, message)

    def reduce(self, quantity: float, message: str = "") -> RiskDecision:
        return RiskDecision.reduce(self.name, quantity, message)

    def pause(self, message: str) -> RiskDecision:
        return RiskDecision.pause(self.name, message)

    def force_close(self, message: str) -> RiskDecision:
        return RiskDecision.force_close(self.name, message)

    def limit_to(self, quantity: float, message: str = "") -> RiskDecision:
        """按配置动作决定「削减」还是「直接拒绝」。"""
        if self.action == "reject":
            return self.reject(message or "超过限额")
        return self.reduce(quantity, message)

    def __repr__(self) -> str:  # pragma: no cover
        return f"{type(self).__name__}(name={self.name!r}, priority={self.priority}, action={self.action!r})"


@dataclass(frozen=True, slots=True)
class RiskTrigger:
    """一次规则触发记录（用于审计与验收指标）。"""

    rule: str
    action: str
    triggered_on: _date
    acted_on: _date | None = None
    order_id: str = ""
    symbol: str = ""
    message: str = ""
    latency_days: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "action": self.action,
            "triggered_on": self.triggered_on,
            "acted_on": self.acted_on,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "message": self.message,
            "latency_days": self.latency_days,
        }


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #
class RiskEngine(ABC):
    """风控引擎（RMS）基类。"""

    def __init__(self, config: RiskConfig | Mapping[str, Any] | None = None) -> None:
        self.config = as_config(config, RiskConfig)
        self.audit = AuditStream(self.config.audit_log)
        self.day = DayState()
        self.engine_state = EngineState()
        self.rules: list[RiskRule] = []
        self.triggers: list[RiskTrigger] = []
        self._alerts: list[dict[str, Any]] = []
        self._paused = False
        self._pause_reason = ""
        self._force_close_requested = False

    # ------------------------------ 核心接口 ------------------------------ #
    @abstractmethod
    def check_order(self, order: Order, account: Any, snapshot: MarketSnapshot) -> RiskDecision:  # pragma: no cover
        """对单笔订单执行全部规则。"""
        raise NotImplementedError

    def CheckOrder(self, order: Order, account: Any, snapshot: MarketSnapshot) -> RiskDecision:
        """合同命名兼容。"""
        return self.check_order(order, account, snapshot)

    def build_context(self, order: Order, account: Any, snapshot: MarketSnapshot) -> RiskContext:
        return RiskContext(
            order=order,
            account=account,
            snapshot=snapshot,
            day=self.day,
            engine=self.engine_state,
            industry_of=self.industry_of,
            returns_of=self.returns_of,
        )

    # ------------------------------ 生命周期 ------------------------------ #
    def on_day_start(self, day: DateLike, account: Any) -> None:
        self.day.reset(day, float(getattr(account, "total_value", 0.0)))
        self.engine_state.update_value(float(getattr(account, "total_value", 0.0)))

    def on_day_end(self, day: DateLike, account: Any) -> None:
        self.engine_state.update_value(float(getattr(account, "total_value", 0.0)))

    def on_order_submitted(self, order: Order, day: DateLike | None = None) -> None:
        self.day.orders_submitted += 1

    def on_order_cancelled(self, order: Order, day: DateLike | None = None) -> None:
        self.day.orders_cancelled += 1

    def on_fill(self, fill: Any) -> None:
        self.day.add_trade(
            fill.symbol, float(getattr(fill, "quantity", 0.0)), float(getattr(fill, "gross_amount", 0.0))
        )

    def on_reject(self, order: Order, decision: RiskDecision, snapshot: MarketSnapshot | None = None) -> None:
        self.day.orders_rejected += 1
        self._record_trigger(decision, order, snapshot)

    # ------------------------------ 控制接口 ------------------------------ #
    def pause(self, reason: str = "") -> None:
        self._paused = True
        self._pause_reason = reason
        self._alerts.append({"kind": "pause", "reason": reason, "day": str(self.day.day)})
        self.audit.log("control", "pause", ts=self.day.day, reason=reason)

    def resume(self) -> None:
        self._paused = False
        self._pause_reason = ""
        self._alerts.append({"kind": "resume", "day": str(self.day.day)})
        self.audit.log("control", "resume", ts=self.day.day)

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def pause_reason(self) -> str:
        return self._pause_reason

    def request_force_close(self, reason: str = "") -> None:
        self._force_close_requested = True
        self.engine_state.force_close = True
        self.engine_state.exposure_scale = 0.0
        self._alerts.append({"kind": "force_close", "reason": reason, "day": str(self.day.day)})
        self.audit.log("control", "force_close", ts=self.day.day, reason=reason)

    @property
    def force_close_requested(self) -> bool:
        return self._force_close_requested

    def clear_force_close(self) -> None:
        self._force_close_requested = False
        self.engine_state.force_close = False
        self.engine_state.exposure_scale = 1.0

    def set_exposure_scale(self, scale: float) -> None:
        self.engine_state.exposure_scale = max(0.0, min(1.0, float(scale)))

    def target_exposure_scale(self) -> float:
        """组合层应当采用的敞口比例（1.0 / 0.5 / 0.0）。"""
        if self._force_close_requested:
            return 0.0
        return self.engine_state.exposure_scale

    # ------------------------------ 热更新 ------------------------------ #
    def update_rules(self, rules: Sequence[RiskRule]) -> None:
        """热替换规则集（立即生效）。"""
        self.rules = list(rules)
        self.audit.log("control", "rules_updated", ts=self.day.day, rules=[r.name for r in self.rules])

    def reload_rules(self, path: str | None = None) -> None:
        """从配置文件重新加载规则（热更新）。"""
        from ..config.loader import load_risk_config
        from .registry import build_rules

        target = path or self.config_file
        if not target:
            raise RiskError("未提供风控配置文件路径，无法热更新")
        config = load_risk_config(target)
        self.config = config
        self.update_rules(build_rules(config.rules))

    # ------------------------------ 预警 ------------------------------ #
    def alerts(self) -> list[dict[str, Any]]:
        return list(self._alerts)

    def drain_alerts(self) -> list[dict[str, Any]]:
        out = list(self._alerts)
        self._alerts.clear()
        return out

    # ------------------------------ 钩子属性 ------------------------------ #
    @property
    def config_file(self) -> str | None:
        return getattr(self, "_config_file", None)

    @config_file.setter
    def config_file(self, value: str | None) -> None:
        self._config_file = value

    industry_of: Callable[[str], str | None] | None = None
    returns_of: Callable[[str, DateLike, int], Sequence[float]] | None = None

    # ------------------------------ 内部 ------------------------------ #
    def _record_trigger(
        self,
        decision: RiskDecision,
        order: Order | None,
        snapshot: MarketSnapshot | None,
    ) -> None:
        day = snapshot.date if snapshot is not None else (self.day.day or to_date("2000-01-01"))
        acted_on = day if decision.action.value in ("reject", "reduce") else None
        latency = 0
        if decision.action.value in ("pause", "force_close") and acted_on is not None:
            latency = 1  # 日频语义：控制动作在次日生效
        self.triggers.append(
            RiskTrigger(
                rule=decision.rule,
                action=decision.action.value,
                triggered_on=to_date(day),
                acted_on=to_date(acted_on) if acted_on is not None else None,
                order_id=order.order_id if order is not None else "",
                symbol=order.symbol if order is not None else "",
                message=decision.message,
                latency_days=latency,
            )
        )


class NullRiskEngine(RiskEngine):
    """无操作风控（默认放行）：用于单元测试与「先跑通链路」的场景。"""

    def check_order(self, order: Order, account: Any, snapshot: MarketSnapshot) -> RiskDecision:
        return RiskDecision.allow("null_risk_engine")
