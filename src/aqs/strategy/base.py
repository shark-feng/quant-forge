"""策略层接口与基类（M3 实现：均线交叉 / 价格突破 / 成交量配合）。

契约要点（写代码前必读）：
- 策略**只能**通过 ``ctx.data``（:class:`~aqs.data.store.PITView`）读取数据，
  物理上不可能取到决策时刻之后的数据；
- 策略输出 :class:`~aqs.core.models.SignalIntent` 信号意向，**不得**下单、不得读取资金/持仓做交易决策；
- 信号在 T 日**收盘后**产生，由引擎统一在 T+1 交付撮合；
- 股票池过滤（ST/停牌/上市不足/流动性）由数据层完成，策略只遍历 ``ctx.universe``；
- 参数必须显式声明：**未知参数键直接报错**，防止拼写错误导致参数静默失效。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date as _date
from typing import TYPE_CHECKING, Any, ClassVar, Mapping, Protocol, Sequence, runtime_checkable

import pandas as pd

from ..core.calendar import TradingCalendar
from ..core.enums import SessionPhase
from ..core.exceptions import ConfigError
from ..core.models import MarketSnapshot, SignalIntent

if TYPE_CHECKING:  # pragma: no cover
    from ..config.schema import BaseConfig
    from ..data.store import PITView
    from ..engine.account import Account

__all__ = ["StrategyContext", "Strategy", "BaseStrategy"]


@dataclass(slots=True)
class StrategyContext:
    """策略计算上下文（每个交易日收盘后构造一次）。

    Attributes:
        data: 固化了 ``as_of=date`` 的数据视图 —— **策略唯一的数据入口**。
        account: 账户快照（策略应视为只读；交易决策请基于信号而非资金）。
        universe: 当日可选股票池（已剔除 ST/停牌/上市不足/流动性不足）。
    """

    date: _date
    phase: SessionPhase
    data: "PITView"
    account: "Account"
    snapshot: MarketSnapshot
    calendar: TradingCalendar
    universe: list[str] = field(default_factory=list)
    config: "BaseConfig | None" = None
    strategy_name: str = ""
    extras: dict[str, Any] = field(default_factory=dict)
    pending_buy_symbols: tuple[str, ...] = ()
    """已提交但尚未成交的买单标的（涨停/停牌顺延中）—— 组合层必须把它们算进仓位名额。"""
    pending_sell_symbols: tuple[str, ...] = ()
    """已提交但尚未成交的卖单标的。"""

    # ------------------------------ 便捷取数 ------------------------------ #
    def bars(self, symbols: Sequence[str] | None = None) -> Mapping[str, Any]:
        syms = list(symbols) if symbols is not None else self.universe
        return self.data.bars_on(self.date, syms)

    def history(self, symbol: str, window: int, field_name: str = "close_adj") -> pd.Series:
        """该标的截至今日的最近 ``window`` 个交易日字段序列。"""
        df = self.data.history(symbol, self.date, window, fields=["date", field_name])
        if df.empty:
            return pd.Series(dtype="float64")
        return pd.Series(df[field_name].to_numpy(), index=pd.DatetimeIndex(df["date"]))

    def panel(
        self,
        window: int,
        field_name: str = "close_adj",
        symbols: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        """多标的字段面板（index=日期，columns=标的），只含 ≤ 今日的行。"""
        syms = list(symbols) if symbols is not None else self.universe
        if not syms:
            return pd.DataFrame()
        return self.data.history_panel(syms, self.date, window, field_name)

    def adv(self, symbol: str, window: int = 20) -> float:
        return self.data.adv(symbol, self.date, window)

    def price(self, symbol: str, adjusted: bool = False) -> float | None:
        bar = self.snapshot.bar(symbol)
        return None if bar is None else bar.price("close", adjusted=adjusted)

    @property
    def symbols(self) -> list[str]:
        return list(self.universe)


@runtime_checkable
class Strategy(Protocol):
    """策略协议（引擎只需 ``on_bar``）。"""

    name: str

    def on_bar(self, ctx: StrategyContext) -> Sequence[SignalIntent]:
        """T 日收盘后计算信号。"""
        ...


class BaseStrategy(ABC):
    """策略基类。

    子类必须声明 :attr:`PARAM_KEYS`（允许的参数键）；传入了未声明的参数键会抛
    :class:`ConfigError`。取值校验请使用本类提供的 ``_int`` / ``_float`` / ``_bool`` / ``_str``。
    """

    name: str = "base"
    PARAM_KEYS: ClassVar[tuple[str, ...]] = ()
    """允许的参数键（未知键报错）。"""

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        self.params: dict[str, Any] = dict(params or {})
        unknown = sorted(set(self.params) - set(self.PARAM_KEYS))
        if unknown:
            raise ConfigError(
                f"策略 {self.name} 存在未知参数 {unknown}；可用参数：{sorted(self.PARAM_KEYS)}",
                path=f"strategy.{self.name}.params",
            )

    # ------------------------------------------------------------------ #
    # 参数读取与校验
    # ------------------------------------------------------------------ #
    def _int(self, key: str, default: int, *, minimum: int | None = None) -> int:
        if key not in self.params or self.params[key] is None:
            return default
        raw = self.params[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ConfigError(f"参数 {key} 必须为整数", path=f"strategy.{self.name}.params.{key}", value=raw)
        if isinstance(raw, float) and not float(raw).is_integer():
            raise ConfigError(f"参数 {key} 必须为整数", path=f"strategy.{self.name}.params.{key}", value=raw)
        value = int(raw)
        if minimum is not None and value < minimum:
            raise ConfigError(
                f"参数 {key} 必须 >= {minimum}", path=f"strategy.{self.name}.params.{key}", value=value
            )
        return value

    def _float(self, key: str, default: float, *, minimum: float | None = None) -> float:
        if key not in self.params or self.params[key] is None:
            return default
        raw = self.params[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ConfigError(f"参数 {key} 必须为数值", path=f"strategy.{self.name}.params.{key}", value=raw)
        value = float(raw)
        if minimum is not None and value < minimum:
            raise ConfigError(
                f"参数 {key} 必须 >= {minimum}", path=f"strategy.{self.name}.params.{key}", value=value
            )
        return value

    def _bool(self, key: str, default: bool) -> bool:
        if key not in self.params or self.params[key] is None:
            return default
        raw = self.params[key]
        if not isinstance(raw, bool):
            raise ConfigError(f"参数 {key} 必须为布尔值", path=f"strategy.{self.name}.params.{key}", value=raw)
        return raw

    def _str(self, key: str, default: str, *, choices: Sequence[str] | None = None) -> str:
        if key not in self.params or self.params[key] is None:
            return default
        raw = self.params[key]
        if not isinstance(raw, str):
            raise ConfigError(f"参数 {key} 必须为字符串", path=f"strategy.{self.name}.params.{key}", value=raw)
        if choices is not None and raw not in choices:
            raise ConfigError(
                f"参数 {key} 只能是 {list(choices)}", path=f"strategy.{self.name}.params.{key}", value=raw
            )
        return raw

    # ------------------------------------------------------------------ #
    # 子类实现
    # ------------------------------------------------------------------ #
    @property
    def min_history(self) -> int:
        """产生信号所需的最小历史 K 线数。"""
        return 2

    @abstractmethod
    def on_bar(self, ctx: StrategyContext) -> Sequence[SignalIntent]:  # pragma: no cover - 抽象方法
        raise NotImplementedError

    def prepare(self, ctx: StrategyContext) -> None:  # noqa: B027 - 可选钩子
        """可选：在每个交易日信号计算前调用（例如预热指标）。"""

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "params": dict(self.params), "min_history": self.min_history}

    def __repr__(self) -> str:  # pragma: no cover
        return f"{type(self).__name__}(name={self.name!r}, params={self.params})"
