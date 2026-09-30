"""核心枚举定义。

约定：
- 比率一律用小数（0.00025 = 万 2.5）。
- 金额单位：元；数量单位：股。
"""

from __future__ import annotations

from enum import Enum, IntEnum

__all__ = [
    "Side",
    "OrderType",
    "OrderStatus",
    "TimeInForce",
    "EventType",
    "SessionPhase",
    "TradingStatus",
    "RejectReason",
    "RiskAction",
    "Board",
    "SignalDirection",
    "FillSource",
]


class Side(str, Enum):
    """买卖方向。A 股不支持裸卖空，SELL 仅表示卖出持仓。"""

    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        """买入 +1，卖出 -1。"""
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    """订单类型。TWAP/VWAP/POV 为算法子单（第三阶段完整实现）。"""

    MARKET = "market"
    LIMIT = "limit"
    TWAP = "twap"
    VWAP = "vwap"
    POV = "pov"


class OrderStatus(str, Enum):
    """订单生命周期状态。"""

    CREATED = "created"
    RISK_PENDING = "risk_pending"
    RISK_REJECTED = "risk_rejected"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    DROPPED = "dropped"
    """风控削减后数量不可执行（不足一手），**从未提交**即被丢弃（缺陷 #13）。

    与 ``risk_rejected`` 的区别：这不是风险拒单，而是「限额与整手约束冲突」产生的
    不可执行数量；因此不计入拒单统计，也不计入误杀率评估。
    """

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        """仍在撮合队列中（可继续成交）。"""
        return self in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)


_TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
        OrderStatus.REJECTED,
        OrderStatus.RISK_REJECTED,
        OrderStatus.DROPPED,
    }
)


class TimeInForce(str, Enum):
    """订单有效期。

    DAY   : 当日有效，未成交即撤销（A 股市价单的常见语义）。
    GTC   : 有效直到撤销，遇涨停/停牌/无报价时按 max_defer_days 顺延。
    """

    DAY = "day"
    GTC = "gtc"


class EventType(IntEnum):
    """事件类型，整数值同时作为同一时间戳下的默认排序优先级。

    顺序即设计文档 §5.1 的约定：
    Timer < MarketData < Signal < RiskCheck < Order < Fill
    """

    TIMER = 0
    MARKET_DATA = 10
    SIGNAL = 20
    RISK_CHECK = 30
    ORDER = 40
    FILL = 50


class SessionPhase(str, Enum):
    """日内阶段。日频回测下用于区分同一交易日的不同事件时刻。"""

    PRE_OPEN = "pre_open"
    OPEN = "open"
    CLOSE = "close"
    POST_CLOSE = "post_close"

    @property
    def time_of_day(self) -> tuple[int, int]:
        """A 股交易时段：09:30 开盘、15:00 收盘。"""
        return _PHASE_TIMES[self]


_PHASE_TIMES: dict[SessionPhase, tuple[int, int]] = {
    SessionPhase.PRE_OPEN: (9, 0),
    SessionPhase.OPEN: (9, 30),
    SessionPhase.CLOSE: (15, 0),
    SessionPhase.POST_CLOSE: (15, 10),
}


class TradingStatus(str, Enum):
    """单个标的在某日的可交易状态。"""

    NORMAL = "normal"
    SUSPENDED = "suspended"
    LIMIT_UP = "limit_up"
    LIMIT_DOWN = "limit_down"
    DELISTED = "delisted"
    NO_QUOTE = "no_quote"

    @property
    def is_tradable(self) -> bool:
        return self is TradingStatus.NORMAL


class RejectReason(str, Enum):
    """订单未能（完全）成交的原因。"""

    NONE = "none"
    NO_QUOTE = "no_quote"                      # 当日无行情（顺延）
    SUSPENDED = "suspended"                    # 停牌（顺延）
    LIMIT_UP = "limit_up"                      # 涨停买不进（顺延）
    LIMIT_DOWN = "limit_down"                  # 跌停卖不出（顺延）
    DELISTED = "delisted"                      # 已退市（拒绝）
    T1_LOCK = "t1_lock"                        # T+1 限制
    LOT_SIZE = "lot_size"                      # 不足 100 股整数倍
    BELOW_LOT = "below_lot"                    # 风控削减后不足一手，不可执行（缺陷 #13）
    LOT_SIZE_RESIDUE = "lot_size_residue"      # 部分成交后剩余不足一手（缺陷 #13）
    INSUFFICIENT_CASH = "insufficient_cash"
    INSUFFICIENT_POSITION = "insufficient_position"
    INSUFFICIENT_LIQUIDITY = "insufficient_liquidity"  # 参与率上限导致当日无可行成交量
    PRICE_OUT_OF_RANGE = "price_out_of_range"  # 限价单未触及
    SHORT_NOT_ALLOWED = "short_not_allowed"    # 禁止卖空
    RISK_REJECTED = "risk_rejected"

    @property
    def is_deferrable(self) -> bool:
        """是否属于「顺延到下一个可成交日」的情形。"""
        return self in _DEFERRABLE


_DEFERRABLE = frozenset(
    {
        RejectReason.NO_QUOTE,
        RejectReason.SUSPENDED,
        RejectReason.LIMIT_UP,
        RejectReason.LIMIT_DOWN,
        RejectReason.INSUFFICIENT_LIQUIDITY,
    }
)


class RiskAction(str, Enum):
    """RMS 决策动作。"""

    ALLOW = "allow"
    REJECT = "reject"
    REDUCE = "reduce"
    PAUSE = "pause"
    FORCE_CLOSE = "force_close"


class Board(str, Enum):
    """板块，决定涨跌停幅度与交易规则。"""

    MAIN = "main"      # 主板（沪 60xxxx / 深 000xxx、001xxx）
    GEM = "gem"        # 创业板 300xxx
    STAR = "star"      # 科创板 688xxx
    BSE = "bse"        # 北交所 8xxxxx / 4xxxxx


class SignalDirection(IntEnum):
    """策略信号方向。"""

    EXIT = -1
    FLAT = 0
    ENTRY = 1


class FillSource(str, Enum):
    """成交来源，用于报告层归因。"""

    OPEN = "open"
    VWAP = "vwap"
    CLOSE = "close"
    LIMIT = "limit"
    TWAP_SLICE = "twap_slice"
    VWAP_SLICE = "vwap_slice"
    FORCE_CLOSE = "force_close"
