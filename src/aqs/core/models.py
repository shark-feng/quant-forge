"""核心数据结构（纯数据，不含业务逻辑）。

时间语义（重要）：
- ``signal_date`` —— 信号产生的交易日（当日收盘后计算）；
- ``decision_ts`` —— 做出决策的时间戳（= signal_date 收盘后）；
- ``submit_date`` —— 订单进入撮合的交易日（T+1 规则下必须 > signal_date）。

价格语义（重要）：
- **原始价**（open/high/low/close）：用于成交、涨跌停判定、费用计算；
- **后复权价**（*_adj）：用于信号计算与收益归因。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Mapping

import pandas as pd

from .dates import DateLike, to_date
from .enums import (
    Board,
    FillSource,
    OrderStatus,
    OrderType,
    RejectReason,
    RiskAction,
    SessionPhase,
    Side,
    SignalDirection,
    TimeInForce,
    TradingStatus,
)

__all__ = [
    "Bar",
    "SymbolMeta",
    "CostBreakdown",
    "Order",
    "Fill",
    "SignalIntent",
    "RiskDecision",
    "MarketSnapshot",
]

_EPS = 1e-12


def _round_cent(x: float) -> float:
    """四舍五入到分（A 股报价精度）。"""
    return math.floor(x * 100 + 0.5) / 100.0


# --------------------------------------------------------------------------- #
# 行情
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SymbolMeta:
    """标的基础信息。"""

    symbol: str
    name: str = ""
    board: Board = Board.MAIN
    list_date: _date | None = None
    delist_date: _date | None = None
    industry: str | None = None

    def is_listed_on(self, day: DateLike) -> bool:
        d = to_date(day)
        if self.list_date is not None and d < self.list_date:
            return False
        if self.delist_date is not None and d > self.delist_date:
            return False
        return True


@dataclass(frozen=True, slots=True)
class Bar:
    """单标的单日行情（原始价 + 后复权因子）。

    Attributes:
        base_adj_factor: 该标的首个交易日的复权因子，用于后复权归一
            （使首日后复权价 = 原始价）。后复权价 = 原始价 × adj_factor / base_adj_factor。
    """

    symbol: str
    date: _date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    adj_factor: float = 1.0
    prev_adj_factor: float = math.nan
    base_adj_factor: float = 1.0
    prev_close: float = math.nan
    limit_up: float = math.nan
    limit_down: float = math.nan
    is_suspended: bool = False
    is_st: bool = False
    listed_days: int = 0
    delist_date: _date | None = None
    board: Board = Board.MAIN

    # ------------------------------ 复权价 ------------------------------ #
    @property
    def _adj_ratio(self) -> float:
        if self.base_adj_factor <= 0 or self.adj_factor <= 0:
            return 1.0
        return self.adj_factor / self.base_adj_factor

    @property
    def open_adj(self) -> float:
        return self.open * self._adj_ratio

    @property
    def high_adj(self) -> float:
        return self.high * self._adj_ratio

    @property
    def low_adj(self) -> float:
        return self.low * self._adj_ratio

    @property
    def close_adj(self) -> float:
        return self.close * self._adj_ratio

    @property
    def prev_close_adj(self) -> float:
        if math.isnan(self.prev_close):
            return math.nan
        return self.prev_close * self._adj_ratio

    # ------------------------------ 工具属性 ------------------------------ #
    @property
    def vwap(self) -> float:
        """成交均价（成交额 / 成交量）。量不足时退化为收盘价。"""
        if self.volume > 0 and self.amount > 0:
            return self.amount / self.volume
        return self.close

    @property
    def dividend_per_share(self) -> float:
        """当日每股现金分红（由复权因子变化推算）。

        推导：除权日原始价 P_t = P_{t-1} - D，且复权因子满足 f_t = f_{t-1} · P_{t-1} / (P_{t-1} - D)，
        因此 ``D = P_{t-1} · (1 - f_{t-1} / f_t)``。

        局限（在报告中必须声明）：送股/转增/配股等非现金的公司行为也会改变复权因子，
        本公式会把这些折算成等值现金计入账户 —— **总收益仍然正确**，但现金与持股结构会有偏差。
        """
        if math.isnan(self.prev_adj_factor) or self.prev_adj_factor <= 0 or self.adj_factor <= 0:
            return 0.0
        if math.isnan(self.prev_close) or self.prev_close <= 0:
            return 0.0
        ratio = self.prev_adj_factor / self.adj_factor
        if ratio >= 1.0:
            return 0.0
        return self.prev_close * (1.0 - ratio)

    @property
    def date_ts(self) -> pd.Timestamp:
        return pd.Timestamp(self.date)

    def price(self, field_name: str = "close", *, adjusted: bool = False) -> float:
        """取价格字段。``adjusted=True`` 取后复权价。"""
        if adjusted:
            field_name = f"{field_name}_adj" if not field_name.endswith("_adj") else field_name
        return float(getattr(self, field_name))

    # ------------------------------ 涨跌停 ------------------------------ #
    def is_limit_up(self, price: float | None = None) -> bool:
        """给定价格（默认开盘价）是否处于涨停。"""
        if math.isnan(self.limit_up):
            return False
        p = self.open if price is None else price
        return p >= self.limit_up - 1e-9

    def is_limit_down(self, price: float | None = None) -> bool:
        if math.isnan(self.limit_down):
            return False
        p = self.open if price is None else price
        return p <= self.limit_down + 1e-9

    def trading_status(self, price: float | None = None) -> TradingStatus:
        """综合停牌/退市/涨跌停后的可交易状态。"""
        if self.is_suspended or self.volume <= 0:
            return TradingStatus.SUSPENDED
        if self.is_limit_up(price):
            return TradingStatus.LIMIT_UP
        if self.is_limit_down(price):
            return TradingStatus.LIMIT_DOWN
        return TradingStatus.NORMAL

    def block_reason_for(self, side: Side, price: float | None = None) -> RejectReason | None:
        """该方向在该时点是否被硬约束阻断；返回阻断原因或 ``None``。"""
        if self.delist_date is not None and self.date > self.delist_date:
            return RejectReason.DELISTED
        if self.is_suspended or self.volume <= 0:
            return RejectReason.SUSPENDED
        if side is Side.BUY and self.is_limit_up(price):
            return RejectReason.LIMIT_UP
        if side is Side.SELL and self.is_limit_down(price):
            return RejectReason.LIMIT_DOWN
        return None


# --------------------------------------------------------------------------- #
# 成本
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """单笔交易的成本明细（单位：元，均为正数）。"""

    fixed_fee: float = 0.0
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    slippage: float = 0.0
    impact: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.fixed_fee
            + self.commission
            + self.stamp_tax
            + self.transfer_fee
            + self.slippage
            + self.impact
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "fixed_fee": self.fixed_fee,
            "commission": self.commission,
            "stamp_tax": self.stamp_tax,
            "transfer_fee": self.transfer_fee,
            "slippage": self.slippage,
            "impact": self.impact,
            "total": self.total,
        }

    def scaled(self, k: float) -> "CostBreakdown":
        """按倍数缩放（用于成本敏感性测试）。"""
        return CostBreakdown(
            fixed_fee=self.fixed_fee * k,
            commission=self.commission * k,
            stamp_tax=self.stamp_tax * k,
            transfer_fee=self.transfer_fee * k,
            slippage=self.slippage * k,
            impact=self.impact * k,
        )

    def __add__(self, other: "CostBreakdown") -> "CostBreakdown":
        return CostBreakdown(
            fixed_fee=self.fixed_fee + other.fixed_fee,
            commission=self.commission + other.commission,
            stamp_tax=self.stamp_tax + other.stamp_tax,
            transfer_fee=self.transfer_fee + other.transfer_fee,
            slippage=self.slippage + other.slippage,
            impact=self.impact + other.impact,
        )


# --------------------------------------------------------------------------- #
# 订单与成交
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Order:
    """订单（可变对象，随生命周期更新状态）。"""

    order_id: str
    symbol: str
    side: Side
    quantity: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    signal_date: _date | None = None
    decision_ts: pd.Timestamp | None = None
    submit_date: _date | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    max_defer_days: int = 5
    tag: str = ""
    parent_id: str | None = None
    status: OrderStatus = OrderStatus.CREATED
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0
    deferred_days: int = 0
    reject_reason: RejectReason = RejectReason.NONE
    created_seq: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    # ------------------------------ 派生属性 ------------------------------ #
    @property
    def remaining(self) -> float:
        return max(self.quantity - self.filled_quantity, 0.0)

    @property
    def is_active(self) -> bool:
        return self.status.is_active

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    def notional(self, price: float) -> float:
        return abs(self.remaining * price)

    # ------------------------------ 状态变更 ------------------------------ #
    def apply_fill(self, quantity: float, price: float) -> None:
        """登记一笔成交并维护加权平均成交价。"""
        qty = min(quantity, self.remaining)
        if qty <= 0:
            return
        total_qty = self.filled_quantity + qty
        self.avg_fill_price = (self.avg_fill_price * self.filled_quantity + price * qty) / total_qty
        self.filled_quantity = total_qty
        if self.remaining <= _EPS:
            self.filled_quantity = self.quantity
            self.status = OrderStatus.FILLED
        else:
            self.status = OrderStatus.PARTIALLY_FILLED

    def defer(self, reason: RejectReason) -> bool:
        """登记一次顺延；返回是否仍然有效。"""
        self.deferred_days += 1
        self.reject_reason = reason
        if self.deferred_days > self.max_defer_days:
            self.status = OrderStatus.EXPIRED
            return False
        self.status = OrderStatus.SUBMITTED
        return True

    def cancel(self, reason: RejectReason = RejectReason.NONE) -> None:
        if self.is_terminal:
            return
        self.status = OrderStatus.CANCELLED
        if reason is not RejectReason.NONE:
            self.reject_reason = reason

    def mark_rejected(self, reason: RejectReason, *, by_risk: bool = False) -> None:
        self.reject_reason = reason
        self.status = OrderStatus.RISK_REJECTED if by_risk else OrderStatus.REJECTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "order_type": self.order_type.value,
            "limit_price": self.limit_price,
            "status": self.status.value,
            "filled_quantity": self.filled_quantity,
            "avg_fill_price": self.avg_fill_price,
            "signal_date": str(self.signal_date) if self.signal_date else None,
            "submit_date": str(self.submit_date) if self.submit_date else None,
            "deferred_days": self.deferred_days,
            "reject_reason": self.reject_reason.value,
            "tag": self.tag,
        }


@dataclass(frozen=True, slots=True)
class Fill:
    """成交回报。"""

    fill_id: str
    order_id: str
    symbol: str
    side: Side
    quantity: float
    price: float
    trade_date: _date
    ts: pd.Timestamp | None = None
    gross_amount: float = 0.0
    cost: CostBreakdown = CostBreakdown()
    source: FillSource = FillSource.OPEN
    deferred_days: int = 0
    tag: str = ""

    @property
    def total_cost(self) -> float:
        return self.cost.total

    @property
    def cash_flow(self) -> float:
        """现金流：买入为负，卖出为正（均已扣除成本）。"""
        if self.side is Side.BUY:
            return -(self.gross_amount + self.total_cost)
        return self.gross_amount - self.total_cost

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "price": self.price,
            "trade_date": str(self.trade_date),
            "gross_amount": self.gross_amount,
            "total_cost": self.total_cost,
            "source": self.source.value,
            "deferred_days": self.deferred_days,
            "tag": self.tag,
        }
        d.update({f"cost_{k}": v for k, v in self.cost.as_dict().items()})
        return d


# --------------------------------------------------------------------------- #
# 信号与风控决策
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SignalIntent:
    """策略输出的信号意向（不是订单）。"""

    symbol: str
    direction: SignalDirection
    signal_date: _date
    score: float = 0.0
    target_weight: float | None = None
    reason: str = ""
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """风控决策结果（对应合同中的 CheckOrder 返回值）。"""

    action: RiskAction = RiskAction.ALLOW
    rule: str = ""
    message: str = ""
    modified_quantity: float | None = None
    reject_reason: RejectReason | None = None

    @property
    def accept(self) -> bool:
        return self.action in (RiskAction.ALLOW, RiskAction.REDUCE)

    @property
    def blocks_trading(self) -> bool:
        return self.action in (RiskAction.REJECT, RiskAction.PAUSE, RiskAction.FORCE_CLOSE)

    # ------------------------------ 构造快捷方式 ------------------------------ #
    @classmethod
    def allow(cls, rule: str = "none") -> "RiskDecision":
        return cls(action=RiskAction.ALLOW, rule=rule)

    @classmethod
    def reject(cls, rule: str, message: str = "", *, reason: RejectReason = RejectReason.RISK_REJECTED) -> "RiskDecision":
        return cls(action=RiskAction.REJECT, rule=rule, message=message, reject_reason=reason)

    @classmethod
    def reduce(cls, rule: str, quantity: float, message: str = "") -> "RiskDecision":
        return cls(action=RiskAction.REDUCE, rule=rule, message=message, modified_quantity=quantity)

    @classmethod
    def pause(cls, rule: str, message: str = "") -> "RiskDecision":
        return cls(action=RiskAction.PAUSE, rule=rule, message=message)

    @classmethod
    def force_close(cls, rule: str, message: str = "") -> "RiskDecision":
        return cls(action=RiskAction.FORCE_CLOSE, rule=rule, message=message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "rule": self.rule,
            "message": self.message,
            "modified_quantity": self.modified_quantity,
            "reject_reason": self.reject_reason.value if self.reject_reason else None,
        }


# --------------------------------------------------------------------------- #
# 市场快照
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """某一交易日某一阶段的市场数据快照（引擎撮合与风控的输入）。"""

    date: _date
    phase: SessionPhase
    bars: Mapping[str, Bar]
    adv_volume: Mapping[str, float] = field(default_factory=dict)
    adv_amount: Mapping[str, float] = field(default_factory=dict)
    ts: pd.Timestamp | None = None

    # ------------------------------ 查询 ------------------------------ #
    def bar(self, symbol: str) -> Bar | None:
        return self.bars.get(symbol)

    def price(self, symbol: str, field_name: str = "close", *, adjusted: bool = False) -> float | None:
        bar = self.bars.get(symbol)
        if bar is None:
            return None
        return bar.price(field_name, adjusted=adjusted)

    def status(self, symbol: str) -> TradingStatus:
        bar = self.bars.get(symbol)
        if bar is None:
            return TradingStatus.NO_QUOTE
        return bar.trading_status()

    def block_reason(self, symbol: str, side: Side, price: float | None = None) -> RejectReason | None:
        bar = self.bars.get(symbol)
        if bar is None:
            return RejectReason.NO_QUOTE
        return bar.block_reason_for(side, price)

    def is_tradable(self, symbol: str) -> bool:
        return self.status(symbol).is_tradable

    def adv_v(self, symbol: str, default: float = 0.0) -> float:
        val = self.adv_volume.get(symbol, default)
        return default if val is None else float(val)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self.bars.keys())

    def __len__(self) -> int:
        return len(self.bars)
