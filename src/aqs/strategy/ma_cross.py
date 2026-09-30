"""策略一：均线交叉（MA5 上穿 MA20 买入 / 下穿卖出）。

合同对应：§四.1

    fast[T] > slow[T] 且 fast[T-1] <= slow[T-1]  → 买入信号（金叉当日）
    fast[T] < slow[T] 且 fast[T-1] >= slow[T-1]  → 卖出信号（死叉当日）
    score = (fast - slow) / slow                  → 乖离率，供组合层排序

价格口径：**后复权价**（``price_field`` 默认 ``close_adj``），
成交/涨跌停/费用仍由引擎按原始价处理。
"""

from __future__ import annotations

from typing import Any, Sequence

from ..core.enums import SignalDirection
from ..core.exceptions import ConfigError
from ..core.models import SignalIntent
from .base import BaseStrategy, StrategyContext
from .indicators import is_finite, tail_mean

__all__ = ["MACrossStrategy"]

_PRICE_FIELDS = ("close_adj", "close", "open_adj", "vwap")


class MACrossStrategy(BaseStrategy):
    """快慢均线交叉策略。"""

    name = "ma_cross"
    PARAM_KEYS = ("fast_window", "slow_window", "price_field", "include_exits")

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.fast_window = self._int("fast_window", 5, minimum=2)
        self.slow_window = self._int("slow_window", 20, minimum=3)
        if self.fast_window >= self.slow_window:
            raise ConfigError(
                f"快线窗口 {self.fast_window} 必须小于慢线窗口 {self.slow_window}",
                path="strategy.ma_cross.params.fast_window",
            )
        self.price_field = self._str("price_field", "close_adj", choices=_PRICE_FIELDS)
        self.include_exits = self._bool("include_exits", True)

    # ------------------------------------------------------------------ #
    @property
    def min_history(self) -> int:
        return self.slow_window + 1

    def on_bar(self, ctx: StrategyContext) -> Sequence[SignalIntent]:
        symbols = list(ctx.universe)
        if not symbols:
            return []
        panel = ctx.panel(self.min_history, self.price_field, symbols)
        if panel.shape[0] < self.min_history or panel.shape[1] == 0:
            return []

        fast = tail_mean(panel, self.fast_window, offset=0)
        fast_prev = tail_mean(panel, self.fast_window, offset=1)
        slow = tail_mean(panel, self.slow_window, offset=0)
        slow_prev = tail_mean(panel, self.slow_window, offset=1)

        signals: list[SignalIntent] = []
        for symbol in panel.columns:
            f, fp = fast.get(symbol), fast_prev.get(symbol)
            s, sp = slow.get(symbol), slow_prev.get(symbol)
            if not all(is_finite(v) for v in (f, fp, s, sp)) or s == 0:
                continue
            if f > s and fp <= sp:
                signals.append(
                    SignalIntent(
                        symbol=symbol,
                        direction=SignalDirection.ENTRY,
                        signal_date=ctx.date,
                        score=(f - s) / s,
                        reason="golden_cross",
                        meta={
                            "ma_fast": f,
                            "ma_slow": s,
                            "ma_fast_prev": fp,
                            "ma_slow_prev": sp,
                            "price_field": self.price_field,
                            "close": float(panel[symbol].iloc[-1]),
                        },
                    )
                )
            elif self.include_exits and f < s and fp >= sp:
                signals.append(
                    SignalIntent(
                        symbol=symbol,
                        direction=SignalDirection.EXIT,
                        signal_date=ctx.date,
                        score=(f - s) / s,
                        reason="dead_cross",
                        meta={
                            "ma_fast": f,
                            "ma_slow": s,
                            "ma_fast_prev": fp,
                            "ma_slow_prev": sp,
                            "price_field": self.price_field,
                            "close": float(panel[symbol].iloc[-1]),
                        },
                    )
                )
        return signals
