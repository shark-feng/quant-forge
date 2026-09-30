"""策略三：成交量配合（放量上涨买入 / 放量下跌卖出）。

合同对应：§四.3

    vol_ma[T]  = mean(volume[T-window .. T-1])        # 过去 window 日均量，不含 T 日
    放量       : volume[T] > vol_ma[T] × ratio
    买入       : close[T] > close[T-1] 且 放量
    卖出       : close[T] < close[T-1] 且 放量
    score      = volume[T] / vol_ma[T]                 # 量比

口径说明：合同写「过去 20 日平均成交量」，因此基准**不含当日**
（否则放量日会抬高自身基准，削弱信号）。该口径由 ``volume_ma_exclude_today`` 控制。
"""

from __future__ import annotations

from typing import Any, Sequence

from ..core.enums import SignalDirection
from ..core.models import SignalIntent
from .base import BaseStrategy, StrategyContext
from .indicators import is_finite, tail_mean

__all__ = ["VolumeStrategy"]

_PRICE_FIELDS = ("close_adj", "close")


class VolumeStrategy(BaseStrategy):
    """放量配合价格方向的策略。"""

    name = "volume"
    PARAM_KEYS = (
        "volume_window",
        "volume_ratio",
        "volume_ma_exclude_today",
        "volume_min_base",
        "price_field",
        "include_exits",
    )

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.volume_window = self._int("volume_window", 20, minimum=2)
        self.volume_ratio = self._float("volume_ratio", 1.5, minimum=0.0)
        self.exclude_today = self._bool("volume_ma_exclude_today", True)
        # 缺陷修复 #9：均量基准过低（长期停牌/新股）时量比会爆炸 → 低于阈值视为无信号
        self.volume_min_base = self._float("volume_min_base", 10_000.0, minimum=0.0)
        self.price_field = self._str("price_field", "close_adj", choices=_PRICE_FIELDS)
        self.include_exits = self._bool("include_exits", True)

    # ------------------------------------------------------------------ #
    @property
    def min_history(self) -> int:
        # 需要「过去 window 日均量」（不含当日）+ 当日 + 前一日收盘价
        return self.volume_window + (1 if self.exclude_today else 0) + 1

    def on_bar(self, ctx: StrategyContext) -> Sequence[SignalIntent]:
        symbols = list(ctx.universe)
        if not symbols:
            return []
        needed = self.min_history
        close_panel = ctx.panel(needed, self.price_field, symbols)
        volume_panel = ctx.panel(needed, "volume", symbols)
        if close_panel.shape[0] < needed or close_panel.shape[1] == 0:
            return []
        if volume_panel.shape[0] < needed or volume_panel.shape[1] == 0:
            return []

        offset = 1 if self.exclude_today else 0
        vol_base = tail_mean(volume_panel, self.volume_window, offset=offset)
        volume_now = volume_panel.iloc[-1].astype("float64")
        close_now = close_panel.iloc[-1].astype("float64")
        close_prev = close_panel.iloc[-2].astype("float64")

        signals: list[SignalIntent] = []
        for symbol in close_panel.columns:
            vol, base = volume_now.get(symbol), vol_base.get(symbol)
            c_now, c_prev = close_now.get(symbol), close_prev.get(symbol)
            if not all(is_finite(v) for v in (vol, base, c_now, c_prev)) or not base or base <= 0:
                continue
            if base < self.volume_min_base:
                # 均量基准过低（长期停牌/新股）：量比不可信，直接放弃该标的（缺陷修复 #9）
                continue
            ratio = vol / base
            if ratio <= self.volume_ratio:
                continue  # 未放量
            meta = {
                "volume": vol,
                "volume_ma": base,
                "volume_ratio": ratio,
                "close": c_now,
                "close_prev": c_prev,
                "price_field": self.price_field,
            }
            if c_now > c_prev:
                signals.append(
                    SignalIntent(
                        symbol=symbol,
                        direction=SignalDirection.ENTRY,
                        signal_date=ctx.date,
                        score=ratio,
                        reason="volume_up",
                        meta=meta,
                    )
                )
            elif self.include_exits and c_now < c_prev:
                signals.append(
                    SignalIntent(
                        symbol=symbol,
                        direction=SignalDirection.EXIT,
                        signal_date=ctx.date,
                        score=ratio,
                        reason="volume_down",
                        meta=meta,
                    )
                )
        return signals
