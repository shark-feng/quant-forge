"""风控规则集合（对应合同 §七 要求的 10+ 条规则）。

每条规则只做一件事，且是**纯判定**：读上下文、返回 :class:`RiskDecision`。
引擎按优先级聚合，``reject/pause/force_close`` 短路，``reduce`` 取最严格者。

规则可扩展：实现 :class:`~aqs.risk.base.RiskRule` 并注册到 ``risk.registry`` 即可，
配置文件中直接写规则名。
"""

from __future__ import annotations

from datetime import date as _date
from typing import Any, Mapping, Sequence

from ..core.enums import Side
from ..core.models import RiskDecision
from .base import RiskContext, RiskRule
from .var import historical_var_es, portfolio_returns

__all__ = [
    "MaxOrderQuantityRule",
    "MaxOrderNotionalRule",
    "PriceDeviationRule",
    "OrderFrequencyRule",
    "CancelRatioRule",
    "MaxTradeVolumeDailyRule",
    "MaxPositionPerSymbolRule",
    "IndustryExposureRule",
    "GrossExposureRule",
    "CashSufficiencyRule",
    "DailyLossLimitRule",
    "MaxDrawdownRule",
    "PortfolioVaRLimitRule",
]

_EPS = 1e-9


# --------------------------------------------------------------------------- #
# 订单级
# --------------------------------------------------------------------------- #
class MaxOrderQuantityRule(RiskRule):
    """单笔订单最大数量（股）。"""

    name = "max_order_quantity"
    priority = 10
    natural_action = "reduce"

    def __init__(self, params: Mapping[str, Any] | None = None, *, action: str | None = None) -> None:
        super().__init__(params, action=action)
        self.max_quantity = float(self.params.get("max_quantity", 1_000_000))

    def check(self, ctx: RiskContext) -> RiskDecision:
        qty = ctx.order.remaining
        if qty <= self.max_quantity:
            return self.allow()
        return self.limit_to(
            self.max_quantity,
            f"单笔订单 {qty:.0f} 股超过上限 {self.max_quantity:.0f} 股",
        )


class MaxOrderNotionalRule(RiskRule):
    """单笔订单最大金额（元）。"""

    name = "max_order_notional"
    priority = 11
    natural_action = "reduce"

    def __init__(self, params: Mapping[str, Any] | None = None, *, action: str | None = None) -> None:
        super().__init__(params, action=action)
        self.max_notional = float(self.params.get("max_notional", 2_000_000.0))

    def check(self, ctx: RiskContext) -> RiskDecision:
        price = ctx.price()
        if price <= 0:
            return self.allow("无有效价格，金额规则跳过")
        notional = ctx.order.remaining * price
        if notional <= self.max_notional:
            return self.allow()
        allowed = self.max_notional / price
        return self.limit_to(allowed, f"单笔金额 {notional:,.0f} 元超过上限 {self.max_notional:,.0f} 元")


class PriceDeviationRule(RiskRule):
    """价格偏离限制。

    - 限价单：限价相对最新价偏离不得超过阈值；
    - 市价单：当日涨跌停判定由撮合层负责，此处只做「无有效价格」保护。
    """

    name = "price_deviation"
    priority = 20
    natural_action = "reject"

    def __init__(self, params: Mapping[str, Any] | None = None, *, action: str | None = None) -> None:
        super().__init__(params, action=action)
        self.max_deviation = float(self.params.get("max_deviation", 0.05))
        self.enabled = bool(self.params.get("enabled", True))

    def check(self, ctx: RiskContext) -> RiskDecision:
        if not self.enabled or ctx.order.limit_price is None:
            return self.allow()
        price = ctx.price()
        if price <= 0:
            return self.reject("无有效最新价，无法校验限价偏离")
        deviation = abs(float(ctx.order.limit_price) / price - 1.0)
        if deviation <= self.max_deviation + _EPS:
            return self.allow()
        return self.reject(
            f"限价 {ctx.order.limit_price:.3f} 相对最新价 {price:.3f} 偏离 {deviation:.2%}，"
            f"超过上限 {self.max_deviation:.2%}"
        )


class OrderFrequencyRule(RiskRule):
    """下单频率限制。

    日频回测下只有「当日总笔数」可真实生效；分钟级限制需要 Tick 回放（第三阶段），
    此处保留配置项并在审计中说明。
    """

    name = "order_frequency"
    priority = 30
    natural_action = "reject"
    portfolio_level = True

    def __init__(self, params: Mapping[str, Any] | None = None, *, action: str | None = None) -> None:
        super().__init__(params, action=action)
        self.max_per_day = int(self.params.get("max_orders_per_day", 2000))
        self.max_per_minute = int(self.params.get("max_orders_per_minute", 30))

    def check(self, ctx: RiskContext) -> RiskDecision:
        submitted = ctx.day.orders_submitted
        if submitted >= self.max_per_day:
            return self.reject(f"当日下单数 {submitted} 已达上限 {self.max_per_day}")
        return self.allow()


class CancelRatioRule(RiskRule):
    """撤单率限制：当日撤单率过高时暂停交易（交易所与券商的常见约束）。"""

    name = "cancel_ratio"
    priority = 31
    natural_action = "pause"
    portfolio_level = True

    def __init__(self, params: Mapping[str, Any] | None = None, *, action: str | None = None) -> None:
        super().__init__(params, action=action)
        self.max_cancel_ratio = float(self.params.get("max_cancel_ratio", 0.8))
        self.min_orders = int(self.params.get("min_orders_for_check", 50))

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.day.orders_submitted < self.min_orders:
            return self.allow()
        ratio = ctx.day.cancel_ratio
        if ratio <= self.max_cancel_ratio:
            return self.allow()
        return self.pause(
            f"当日撤单率 {ratio:.2%} 超过上限 {self.max_cancel_ratio:.2%}（{ctx.day.orders_cancelled}/"
            f"{ctx.day.orders_submitted} 笔）"
        )


# --------------------------------------------------------------------------- #
# 仓位与敞口级
# --------------------------------------------------------------------------- #
class MaxTradeVolumeDailyRule(RiskRule):
    """单日最大交易量：当日累计成交 + 本单不得超过 ADV 的一定比例。"""

    name = "max_trade_volume_daily"
    priority = 40
    natural_action = "reduce"

    def __init__(self, params: Mapping[str, Any] | None = None, *, action: str | None = None) -> None:
        super().__init__(params, action=action)
        self.max_ratio = float(self.params.get("max_daily_volume_ratio", 0.20))
        self.fallback_adv = float(self.params.get("fallback_adv", 0.0))

    def check(self, ctx: RiskContext) -> RiskDecision:
        adv = ctx.snapshot.adv_v(ctx.order.symbol, default=0.0) or self.fallback_adv
        if adv <= 0:
            return self.allow("缺少 ADV 数据，单日成交量规则跳过")
        limit = adv * self.max_ratio
        used = ctx.day.traded_on(ctx.order.symbol)
        remaining = limit - used
        if remaining <= 0:
            return self.reject(
                f"{ctx.order.symbol} 当日已成交 {used:,.0f} 股，达到单日上限 {limit:,.0f} 股（ADV×{self.max_ratio:.0%}）"
            )
        if ctx.order.remaining <= remaining:
            return self.allow()
        return self.limit_to(
            remaining,
            f"单日累计成交量上限 {limit:,.0f} 股（已用 {used:,.0f}），本单削减至 {remaining:,.0f} 股",
        )


class MaxPositionPerSymbolRule(RiskRule):
    """单票最大持仓（按成交后市值占总资产比例）。"""

    name = "max_position_per_symbol"
    priority = 50
    natural_action = "reduce"

    def __init__(self, params: Mapping[str, Any] | None = None, *, action: str | None = None) -> None:
        super().__init__(params, action=action)
        self.max_weight = float(self.params.get("max_weight", 0.10))

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.order.side is not Side.BUY:
            return self.allow()
        total = ctx.total_value
        price = ctx.price()
        if total <= 0 or price <= 0:
            return self.allow()
        current = ctx.position_value(ctx.order.symbol)
        limit_value = self.max_weight * total
        allowed_value = limit_value - current
        if allowed_value <= 0:
            return self.limit_to(
                0.0,
                f"{ctx.order.symbol} 当前持仓市值占比已达 {current / total:.2%}，超过上限 {self.max_weight:.2%}",
            )
        allowed_qty = allowed_value / price
        if ctx.order.remaining <= allowed_qty:
            return self.allow()
        return self.limit_to(
            allowed_qty,
            f"成交后单票权重将超过 {self.max_weight:.2%}，本单削减至 {allowed_qty:,.0f} 股",
        )


class IndustryExposureRule(RiskRule):
    """行业暴露上限。缺少行业映射时跳过（并在审计中说明）。"""

    name = "industry_exposure"
    priority = 60
    natural_action = "reduce"

    def __init__(self, params: Mapping[str, Any] | None = None, *, action: str | None = None) -> None:
        super().__init__(params, action=action)
        self.max_industry_weight = float(self.params.get("max_industry_weight", 0.30))
        self.industry_map: Mapping[str, str] = dict(self.params.get("industry_map") or {})

    def _industry_of(self, ctx: RiskContext, symbol: str) -> str | None:
        mapped = self.industry_map.get(symbol)
        if mapped:
            return mapped
        return ctx.industry(symbol)

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.order.side is not Side.BUY:
            return self.allow()
        industry = self._industry_of(ctx, ctx.order.symbol)
        if not industry:
            return self.allow(f"{ctx.order.symbol} 无行业分类，行业暴露规则跳过")
        total = ctx.total_value
        price = ctx.price()
        if total <= 0 or price <= 0:
            return self.allow()
        exposure = 0.0
        for symbol, position in getattr(ctx.account, "positions", {}).items():
            if self._industry_of(ctx, symbol) == industry:
                exposure += float(position.market_value)
        limit_value = self.max_industry_weight * total
        allowed_value = limit_value - exposure
        if allowed_value <= 0:
            return self.limit_to(
                0.0,
                f"行业 {industry} 暴露已达 {exposure / total:.2%}，超过上限 {self.max_industry_weight:.2%}",
            )
        allowed_qty = allowed_value / price
        if ctx.order.remaining <= allowed_qty:
            return self.allow()
        return self.limit_to(
            allowed_qty,
            f"行业 {industry} 暴露将超过 {self.max_industry_weight:.2%}，本单削减至 {allowed_qty:,.0f} 股",
        )


class GrossExposureRule(RiskRule):
    """总市值敞口上限与卖空限制。"""

    name = "gross_exposure"
    priority = 70
    natural_action = "reject"

    def __init__(self, params: Mapping[str, Any] | None = None, *, action: str | None = None) -> None:
        super().__init__(params, action=action)
        self.max_gross = float(self.params.get("max_gross_exposure", 1.00))
        self.allow_short = bool(self.params.get("allow_short", False))

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.order.side is Side.SELL and not self.allow_short:
            # 卖空限制：以**持仓总量**为准（订单在 T+1 执行，届时 T+1 冻结已解锁；
            # 真正的可卖量约束由撮合层在执行当日强制）。
            held = ctx.account.total_quantity(ctx.order.symbol)
            if held <= _EPS:
                return self.reject(f"禁止卖空：{ctx.order.symbol} 无持仓")
            if ctx.order.remaining > held + _EPS:
                return self.reduce(
                    held, f"卖出数量 {ctx.order.remaining:,.0f} 超过持仓 {held:,.0f}，削减至持仓量"
                )
            return self.allow()

        total = ctx.total_value
        price = ctx.price()
        if total <= 0 or price <= 0:
            return self.allow()
        positions_value = float(getattr(ctx.account, "positions_value", 0.0))
        post_value = positions_value + ctx.order.remaining * price
        if post_value / total <= self.max_gross + _EPS:
            return self.allow()
        allowed_value = self.max_gross * total - positions_value
        if allowed_value <= 0:
            return self.reject(
                f"总市值敞口已达 {positions_value / total:.2%}，超过上限 {self.max_gross:.2%}"
            )
        # 超过上限时削减到「刚好不越线」，而不是整单拒绝（风控不应误杀可执行的订单）
        return self.reduce(
            allowed_value / price,
            f"总市值敞口将超过 {self.max_gross:.2%}，本单削减至 {allowed_value / price:,.0f} 股",
        )


class CashSufficiencyRule(RiskRule):
    """现金充足性：成交后现金比例不得低于下限。"""

    name = "cash_sufficiency"
    priority = 80
    natural_action = "reduce"

    def __init__(self, params: Mapping[str, Any] | None = None, *, action: str | None = None) -> None:
        super().__init__(params, action=action)
        self.min_cash_ratio = float(self.params.get("min_cash_ratio", 0.02))

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.order.side is not Side.BUY:
            return self.allow()
        total = ctx.total_value
        price = ctx.price()
        if total <= 0 or price <= 0:
            return self.allow()
        cash = ctx.cash
        min_cash = self.min_cash_ratio * total
        spendable = cash - min_cash
        if spendable <= 0:
            return self.limit_to(
                0.0,
                f"现金 {cash:,.0f} 元不足以保留 {self.min_cash_ratio:.2%} 的最低现金比例",
            )
        allowed_qty = spendable / price
        if ctx.order.remaining <= allowed_qty:
            return self.allow()
        return self.limit_to(allowed_qty, f"为保留 {self.min_cash_ratio:.2%} 现金，本单削减至 {allowed_qty:,.0f} 股")


# --------------------------------------------------------------------------- #
# 损失与回撤级
# --------------------------------------------------------------------------- #
class DailyLossLimitRule(RiskRule):
    """当日最大亏损：触发后暂停当日剩余交易。"""

    name = "daily_loss_limit"
    priority = 90
    natural_action = "pause"
    portfolio_level = True

    def __init__(self, params: Mapping[str, Any] | None = None, *, action: str | None = None) -> None:
        super().__init__(params, action=action)
        self.max_daily_loss = float(self.params.get("max_daily_loss", 0.03))

    def check(self, ctx: RiskContext) -> RiskDecision:
        start = ctx.day.start_value
        if start <= 0:
            return self.allow()
        loss = ctx.total_value / start - 1.0
        if loss >= -self.max_daily_loss:
            return self.allow()
        return self.pause(f"当日亏损 {loss:.2%} 超过上限 {self.max_daily_loss:.2%}，暂停当日交易")


class MaxDrawdownRule(RiskRule):
    """最大回撤分档动作：预警 / 减仓 / 强制平仓。

    - 回撤 ∈ [warn, reduce)          → 预警（仍放行，写入审计）
    - 回撤 ∈ [reduce, force_close)   → 敞口缩放至 ``reduce_exposure``（默认 0.5）
    - 回撤 >= force_close           → 请求强制平仓并停止交易
    """

    name = "max_drawdown_action"
    priority = 100
    natural_action = "force_close"
    portfolio_level = True

    def __init__(self, params: Mapping[str, Any] | None = None, *, action: str | None = None) -> None:
        super().__init__(params, action=action)
        self.warn = float(self.params.get("warn_drawdown", 0.10))
        self.reduce_at = float(self.params.get("reduce_drawdown", 0.15))
        self.close_at = float(self.params.get("force_close_drawdown", 0.20))
        self.reduce_exposure = float(self.params.get("reduce_exposure", 0.5))

    def check(self, ctx: RiskContext) -> RiskDecision:
        drawdown = abs(min(ctx.engine.drawdown, 0.0))
        if drawdown >= self.close_at:
            ctx.engine.exposure_scale = 0.0
            return self.force_close(
                f"回撤 {drawdown:.2%} 达到强平阈值 {self.close_at:.2%}，强制平仓并停止交易"
            )
        if drawdown >= self.reduce_at:
            ctx.engine.exposure_scale = min(ctx.engine.exposure_scale, self.reduce_exposure)
            return self.allow(
                f"回撤 {drawdown:.2%} 达到减仓阈值 {self.reduce_at:.2%}，敞口降至 {self.reduce_exposure:.0%}"
            )
        if drawdown >= self.warn:
            return self.allow(f"回撤 {drawdown:.2%} 达到预警阈值 {self.warn:.2%}")
        return self.allow()


class PortfolioVaRLimitRule(RiskRule):
    """组合 VaR 上限（历史模拟法）：成交后组合 1 日 VaR 超过上限则削减。"""

    name = "portfolio_var_limit"
    priority = 110
    natural_action = "reduce"

    def __init__(self, params: Mapping[str, Any] | None = None, *, action: str | None = None) -> None:
        super().__init__(params, action=action)
        self.max_var = float(self.params.get("max_var", 0.03))
        self.confidence = float(self.params.get("confidence", 0.95))
        self.window = int(self.params.get("window", 60))
        self.horizon = int(self.params.get("horizon", 1))
        self.min_obs = int(self.params.get("min_obs", 30))

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.order.side is not Side.BUY:
            return self.allow()
        if ctx.returns_of is None:
            return self.allow("未配置历史收益提供器，VaR 规则跳过")
        total = ctx.total_value
        price = ctx.price()
        if total <= 0 or price <= 0:
            return self.allow()

        positions = getattr(ctx.account, "positions", {})
        symbols = [s for s in positions if s != ctx.order.symbol]
        series: dict[str, Sequence[float]] = {}
        for symbol in symbols:
            data = ctx.returns(symbol, self.window)
            if data is None or len(data) < self.min_obs:
                return self.allow(f"{symbol} 历史收益不足（<{self.min_obs}），VaR 规则跳过")
            series[symbol] = list(data)

        candidate = ctx.returns(ctx.order.symbol, self.window)
        if candidate is None or len(candidate) < self.min_obs:
            return self.allow(f"{ctx.order.symbol} 历史收益不足（<{self.min_obs}），VaR 规则跳过")
        series[ctx.order.symbol] = list(candidate)

        n_obs = min(len(v) for v in series.values())
        order_symbols = sorted(series)
        matrix = _to_matrix([series[s][-n_obs:] for s in order_symbols])
        current_weights = [ctx.position_value(s) / total for s in order_symbols]
        var_now = historical_var_es(
            portfolio_returns(current_weights, matrix), confidence=self.confidence, horizon=self.horizon,
            min_obs=self.min_obs,
        ).var

        # 本单成交后的权重（买入：该标的市值增加）
        add_value = ctx.order.remaining * price
        idx = order_symbols.index(ctx.order.symbol)
        post_weights = list(current_weights)
        post_weights[idx] += add_value / total
        if sum(post_weights) > 1.0:
            # 超过 100% 说明现金不足，交给现金规则处理，这里按可投上限截断
            return self.allow("权重之和超过 1，交由现金与敞口规则处理")
        var_post = historical_var_es(
            portfolio_returns(post_weights, matrix), confidence=self.confidence, horizon=self.horizon,
            min_obs=self.min_obs,
        ).var

        if var_post <= self.max_var + _EPS:
            return self.allow()
        if var_now >= self.max_var:
            return self.limit_to(
                0.0,
                f"组合 VaR {var_now:.2%} 已超过上限 {self.max_var:.2%}（{self.confidence:.0%}）",
            )
        # 线性插值求满足 VaR 上限的最大加仓金额
        room = (self.max_var - var_now) / max(var_post - var_now, _EPS)
        allowed_value = max(room, 0.0) * add_value
        return self.limit_to(
            allowed_value / price,
            f"组合 VaR 将由 {var_now:.2%} 升至 {var_post:.2%}，超过上限 {self.max_var:.2%}，本单削减",
        )


def _to_matrix(rows: Sequence[Sequence[float]]) -> Any:
    import numpy as np

    return np.asarray([[float(x) for x in row] for row in rows], dtype="float64").T
