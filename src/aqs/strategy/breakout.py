"""策略二：价格突破（收盘价创 N 日新高买入 / 创 N 日新低卖出）。

合同对应：§四.2

    prior_high[T] = max(high[T-window .. T-1])       # 不含 T 日
    prior_low[T]  = min(low[T-window .. T-1])
    买入: close[T] > prior_high[T]        （严格大于）
    卖出: close[T] < prior_low[T]
    score: 突破幅度 (close/prior_high - 1)；跌破为负值

``exclude_today`` 必须为 True —— 若允许包含当日，就等于用当日最高价判断当日突破，
构造时会直接报错，避免把未来函数写进配置。
"""

from __future__ import annotations

from typing import Any, Sequence

from ..core.enums import SignalDirection
from ..core.exceptions import ConfigError
from ..core.models import SignalIntent
from .base import BaseStrategy, StrategyContext
from .indicators import is_finite, tail_max, tail_mean, tail_min

__all__ = ["BreakoutStrategy"]

_CLOSE_FIELDS = ("close_adj", "close")
_HIGH_FIELDS = ("high_adj", "high")
_LOW_FIELDS = ("low_adj", "low")


class BreakoutStrategy(BaseStrategy):
    """N 日价格突破策略。"""

    name = "breakout"
    PARAM_KEYS = (
        "window",
        "exclude_today",
        "price_field",
        "high_field",
        "low_field",
        "confirm_volume",
        "volume_window",
        "volume_ratio",
        "include_exits",
    )

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.window = self._int("window", 20, minimum=2)
        self.exclude_today = self._bool("exclude_today", True)
        if not self.exclude_today:
            raise ConfigError(
                "exclude_today=False 会用当日最高价判断当日突破（未来函数），不允许",
                path="strategy.breakout.params.exclude_today",
            )
        self.price_field = self._str("price_field", "close_adj", choices=_CLOSE_FIELDS)
        self.high_field = self._str("high_field", "high_adj", choices=_HIGH_FIELDS)
        self.low_field = self._str("low_field", "low_adj", choices=_LOW_FIELDS)
        self.confirm_volume = self._bool("confirm_volume", False)
        self.volume_window = self._int("volume_window", 20, minimum=2)
        self.volume_ratio = self._float("volume_ratio", 1.5, minimum=0.0)
        self.include_exits = self._bool("include_exits", True)

    # ------------------------------------------------------------------ #
    @property
    def min_history(self) -> int:
        extra = self.volume_window + 1 if self.confirm_volume else 0
        return max(self.window + 1, extra)

    def on_bar(self, ctx: StrategyContext) -> Sequence[SignalIntent]:
        symbols = list(ctx.universe)
        if not symbols:
            return []
        needed = self.min_history
        close_panel = ctx.panel(needed, self.price_field, symbols)
        if close_panel.shape[0] < needed or close_panel.shape[1] == 0:
            return []

        high_panel = ctx.panel(needed, self.high_field, symbols)
        low_panel = ctx.panel(needed, self.low_field, symbols)
        prior_high = tail_max(high_panel, self.window, offset=1)
        prior_low = tail_min(low_panel, self.window, offset=1)

        volume_ok = None
        if self.confirm_volume:
            volume_panel = ctx.panel(needed, "volume", symbols)
            vol_base = tail_mean(volume_panel, self.volume_window, offset=1)
            volume_ok = (volume_panel.iloc[-1] > vol_base * self.volume_ratio) & vol_base.gt(0)

        signals: list[SignalIntent] = []
        for symbol in close_panel.columns:
            close = float(close_panel[symbol].iloc[-1])
            ph, pl = prior_high.get(symbol), prior_low.get(symbol)
            if not is_finite(close):
                continue
            if volume_ok is not None:
                flag = volume_ok.get(symbol)
                if flag is None or not bool(flag):
                    continue
            if is_finite(ph) and close > ph:
                signals.append(
                    SignalIntent(
                        symbol=symbol,
                        direction=SignalDirection.ENTRY,
                        signal_date=ctx.date,
                        score=close / ph - 1.0,
                        reason="breakout_high",
                        meta={"prior_high": ph, "close": close, "window": self.window},
                    )
                )
            elif self.include_exits and is_finite(pl) and close < pl:
                signals.append(
                    SignalIntent(
                        symbol=symbol,
                        direction=SignalDirection.EXIT,
                        signal_date=ctx.date,
                        score=close / pl - 1.0,
                        reason="breakdown_low",
                        meta={"prior_low": pl, "close": close, "window": self.window},
                    )
                )
        return signals
