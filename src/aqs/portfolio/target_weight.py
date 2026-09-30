"""目标权重组合：把策略信号转成订单计划（M4 正式实现）。

处理顺序（顺序很重要，见 ``docs/05_portfolio_layer.md`` §4）：

1. **退出**：EXIT 信号 → 卖出可卖数量（T+1 未解锁的部分留给引擎顺延）；
2. **目标集合**：现有持仓（未收到 EXIT）+ 按 score 排序的前若干新标的（受最大持仓数限制）；
3. **调仓**（可选，``rebalance=True``）：目标外的持仓全部卖出；
4. **敞口**：现金 + 预计卖出回款 → 扣除现金缓冲 → 乘凯利敞口（启用时）；
5. **买单**：目标权重 × 总资产 → 整手数量，受可用资金约束；
6. **输出顺序**：卖单在前、买单在后 —— 引擎在 T+1 开盘按 FIFO 撮合，
   先卖后买才用得上卖出回款。

默认 ``rebalance=False``：策略只在交叉/跌破当日发 EXIT，
若每天都把「当日无 ENTRY 信号」的老持仓卖掉，会产生无意义的高换手。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..core.enums import OrderType, Side, SignalDirection
from ..core.exceptions import ConfigError
from ..core.models import SignalIntent
from ..strategy.base import StrategyContext
from .base import BasePortfolio, OrderPlan, TargetWeight
from .sizing import KellyResult, SizingConfig, equal_weights, kelly_from_trades, score_weights

__all__ = ["TargetWeightPortfolio"]

_EPS = 1e-9


class TargetWeightPortfolio(BasePortfolio):
    """等权/分数加权 + 单票上限 + 最大持仓数 + 现金管理 + 凯利敞口。"""

    name = "target_weight"

    def __init__(self, config: SizingConfig | Mapping[str, Any] | None = None) -> None:
        if isinstance(config, SizingConfig):
            sizing = config
            params: dict[str, Any] = {}
        else:
            from .registry import sizing_from_params

            params = dict(config or {})
            sizing = sizing_from_params(params)
        super().__init__(params)
        self.sizing: SizingConfig = sizing
        self._traded_symbols: set[str] = set()
        self._last_targets: list[TargetWeight] = []
        self._last_kelly: KellyResult | None = None
        self._last_exposure: float = 1.0

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def generate_orders(self, signals: Sequence[SignalIntent], ctx: StrategyContext) -> Sequence[OrderPlan]:
        cfg = self.sizing
        account = ctx.account
        if not signals:
            return []

        sell_plans: list[OrderPlan] = []
        buy_plans: list[OrderPlan] = []

        # ---------- ① 退出信号 ----------
        exit_symbols = list(dict.fromkeys(s.symbol for s in signals if s.direction is SignalDirection.EXIT))
        exit_set = set(exit_symbols)
        for symbol in exit_symbols:
            available = account.sellable(symbol)
            if available > 0:
                sell_plans.append(
                    OrderPlan(symbol, Side.SELL, available, tag="exit", reason="strategy_exit")
                )

        # ---------- ② 目标集合 ----------
        # 仓位名额必须把「已持有的」和「已提交但未成交的买单」都算进去：
        # 涨停/停牌顺延中的买单可能过几天才成交，若不计入就会突破最大持仓数。
        #
        # 名额释放采用**保守口径**：只有持仓真正消失（卖出已成交）才释放名额，
        # 卖出信号本身不释放。否则「卖单因跌停顺延未成交 + 同日新开仓」会让持仓数超过上限。
        pending_buys = set(ctx.pending_buy_symbols)
        pending_sells = set(ctx.pending_sell_symbols)
        held_like = sorted(set(account.holding_symbols) | pending_buys)
        entry_symbols = {s.symbol for s in signals if s.direction is SignalDirection.ENTRY}
        entries = [
            s
            for s in signals
            if s.direction is SignalDirection.ENTRY
            and s.symbol not in held_like
            and s.symbol not in pending_sells
            and (cfg.allow_reentry or s.symbol not in self._traded_symbols)
        ]
        entries.sort(key=lambda s: (s.score, s.symbol), reverse=True)
        slots = max(cfg.max_positions - len(held_like), 0)
        new_entries = entries[:slots]
        new_names = [s.symbol for s in new_entries]
        target = list(held_like) + new_names

        # ---------- ③ 调仓：卖出目标外的持仓 ----------
        if cfg.rebalance:
            for symbol in account.holding_symbols:
                if symbol in exit_set or symbol in entry_symbols:
                    continue
                available = account.sellable(symbol)
                if available > 0:
                    sell_plans.append(
                        OrderPlan(symbol, Side.SELL, available, tag="rebalance", reason="not_in_target")
                    )

        # ---------- ④ 可投资金与敞口 ----------
        self._last_exposure = self._compute_exposure(account, cfg)
        expected_proceeds = 0.0
        for plan in sell_plans:
            bar = ctx.snapshot.bar(plan.symbol)
            price = bar.close if bar is not None else 0.0
            expected_proceeds += plan.quantity * price
        investable = (account.available_cash() + expected_proceeds) * (1.0 - cfg.cash_buffer)

        # ---------- ⑤ 目标权重（只对新标的分配资金） ----------
        weights = self._build_weights(new_entries, len(target), cfg)
        self._last_targets = [
            TargetWeight(symbol=name, weight=weights.get(name, 0.0), reason="entry") for name in new_names
        ]

        remaining = investable
        total_value = account.total_value
        for entry in new_entries:
            bar = ctx.snapshot.bar(entry.symbol)
            if bar is None or bar.close <= 0:
                continue
            weight = weights.get(entry.symbol, 0.0)
            if weight <= 0:
                continue
            budget = min(weight * total_value, remaining)
            quantity = self._round_lot(budget / bar.close, cfg.lot_size)
            if quantity <= 0:
                continue
            buy_plans.append(
                OrderPlan(
                    entry.symbol,
                    Side.BUY,
                    quantity,
                    order_type=OrderType.MARKET,
                    tag="entry",
                    reason="entry",
                    meta={
                        "target_weight": weight,
                        "score": entry.score,
                        "signal_reason": entry.reason,
                        "budget": budget,
                    },
                )
            )
            remaining -= quantity * bar.close

        self._traded_symbols.update(plan.symbol for plan in sell_plans)
        # 卖单在前、买单在后（T+1 开盘 FIFO 撮合，先卖后买才用得上回款）
        return sell_plans + buy_plans

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _round_lot(quantity: float, lot: int) -> float:
        if quantity <= 0:
            return 0.0
        return float(int(quantity // lot) * lot)

    def _compute_exposure(self, account: Any, cfg: SizingConfig) -> float:
        """总敞口：默认 1.0；启用凯利时按已平仓交易统计计算半凯利敞口。"""
        if not cfg.kelly.enabled:
            self._last_kelly = None
            return 1.0
        result = kelly_from_trades(getattr(account, "trade_returns", lambda: [])(), cfg.kelly)
        self._last_kelly = result
        return result.exposure

    def _build_weights(
        self,
        new_entries: Sequence[SignalIntent],
        n_target: int,
        cfg: SizingConfig,
    ) -> dict[str, float]:
        """目标权重（占**总资产**比例，已含现金缓冲与凯利敞口）。

        有效总敞口 = ``(1 - cash_buffer) × kelly_exposure``，再在候选之间分配：

        - ``equal``：``min(有效敞口 / n, 单票上限)``，其中 ``n`` 是目标持仓总数（含已有持仓）；
        - ``score_prop``：按 score 正比例分配后在**有效敞口**内归一化，再施加单票上限。

        这样凯利敞口会同时约束「总仓位」与「每个新仓位的绝对规模」，
        而单票上限始终是最硬的那道约束。
        """
        if not new_entries:
            return {}
        max_weight = cfg.max_weight_per_symbol
        effective = max(0.0, (1.0 - cfg.cash_buffer) * self._last_exposure)
        if effective <= 0:
            return {s.symbol: 0.0 for s in new_entries}

        if cfg.weighting == "equal":
            n = max(n_target, len(new_entries), 1)
            weight = min(effective / n, max_weight)
            return {s.symbol: weight for s in new_entries}

        raw = score_weights([(s.symbol, s.score) for s in new_entries], max_weight=None)
        return {sym: min(w * effective, max_weight) for sym, w in raw.items()}

    # ------------------------------------------------------------------ #
    def describe(self) -> dict[str, Any]:
        cfg = self.sizing
        return {
            "name": self.name,
            "weighting": cfg.weighting,
            "max_positions": cfg.max_positions,
            "max_weight_per_symbol": cfg.max_weight_per_symbol,
            "cash_buffer": cfg.cash_buffer,
            "allow_reentry": cfg.allow_reentry,
            "rebalance": cfg.rebalance,
            "lot_size": cfg.lot_size,
            "kelly": {
                "enabled": cfg.kelly.enabled,
                "mode": cfg.kelly.mode,
                "fraction": cfg.kelly.fraction,
                "cap": cfg.kelly.cap,
                "min_trades": cfg.kelly.min_trades,
            },
            "last_exposure": self._last_exposure,
            "last_kelly": self._last_kelly.as_dict() if self._last_kelly else None,
            "last_targets": [
                {"symbol": t.symbol, "weight": t.weight, "reason": t.reason} for t in self._last_targets
            ],
        }

    @property
    def last_targets(self) -> list[TargetWeight]:
        """最近一次生成的目标权重（供报告层做归因）。"""
        return list(self._last_targets)
