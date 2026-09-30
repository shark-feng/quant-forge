"""账户与持仓账务。

A 股口径要点：
1. **T+1**：买入成交当日不计入可卖数量；每个交易日开盘前统一解锁（``unlock_t1``）。
2. **现金分红**：除权日按复权因子变化把现金计入账户（``accrue_dividends``），
   使「原始价估值 + 分红入账」与「后复权价收益」在总收益上等价；
   分红在**开盘前**计提（当日买入者不享受当日分红）。
3. **估值**：用原始价（真实市价）估值；信号仍用后复权价。
4. 买入成本（佣金/印花税/过户费/滑点/冲击）全部计入持仓成本，卖出时计算已实现盈亏。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Mapping

from ..core.dates import DateLike, to_date
from ..core.enums import Side
from ..core.exceptions import EngineError
from ..core.logging import get_logger
from ..core.models import Fill, MarketSnapshot

__all__ = ["Position", "DailyRecord", "Account", "ClosedTrade"]

logger = get_logger("engine.account")

_EPS = 1e-9


@dataclass(slots=True)
class Position:
    """单个标的的持仓。"""

    symbol: str
    total_quantity: float = 0.0
    sellable_quantity: float = 0.0
    avg_cost: float = 0.0
    last_price: float = math.nan
    realized_pnl: float = 0.0
    cost_paid: float = 0.0
    opened_on: _date | None = None
    last_update: _date | None = None

    @property
    def market_value(self) -> float:
        if math.isnan(self.last_price):
            return 0.0
        return self.total_quantity * self.last_price

    @property
    def cost_basis(self) -> float:
        return self.total_quantity * self.avg_cost

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_basis

    @property
    def unrealized_return(self) -> float:
        if self.cost_basis <= _EPS:
            return 0.0
        return self.unrealized_pnl / self.cost_basis

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "total_quantity": self.total_quantity,
            "sellable_quantity": self.sellable_quantity,
            "avg_cost": self.avg_cost,
            "last_price": self.last_price,
            "market_value": self.market_value,
            "unrealized_pnl": self.unrealized_pnl,
            "realized_pnl": self.realized_pnl,
            "unrealized_return": self.unrealized_return,
        }


@dataclass(slots=True)
class ClosedTrade:
    """一笔已平仓交易的记录（供组合层估计胜率/赔率、供报告层做归因）。"""

    symbol: str
    quantity: float
    cost_basis: float
    proceeds: float
    pnl: float
    return_pct: float
    closed_on: _date
    holding_days: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "cost_basis": self.cost_basis,
            "proceeds": self.proceeds,
            "pnl": self.pnl,
            "return_pct": self.return_pct,
            "closed_on": self.closed_on,
            "holding_days": self.holding_days,
        }


@dataclass(slots=True)
class DailyRecord:
    """每日账户快照（净值曲线的原子记录）。"""

    date: _date
    cash: float
    positions_value: float
    total_value: float
    daily_return: float = 0.0
    cumulative_return: float = 0.0
    n_positions: int = 0
    gross_exposure: float = 0.0
    turnover: float = 0.0
    costs: float = 0.0
    dividends: float = 0.0
    realized_pnl: float = 0.0
    drawdown: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "cash": self.cash,
            "positions_value": self.positions_value,
            "total_value": self.total_value,
            "daily_return": self.daily_return,
            "cumulative_return": self.cumulative_return,
            "n_positions": self.n_positions,
            "gross_exposure": self.gross_exposure,
            "turnover": self.turnover,
            "costs": self.costs,
            "dividends": self.dividends,
            "drawdown": self.drawdown,
        }


class Account:
    """账户状态：现金、持仓、成本与净值记录。"""

    def __init__(self, initial_cash: float, *, name: str = "account") -> None:
        if initial_cash <= 0:
            raise EngineError(f"初始资金必须为正，收到 {initial_cash}")
        self.name = name
        self.initial_cash = float(initial_cash)
        self.cash = float(initial_cash)
        self.positions: dict[str, Position] = {}
        self.history: list[DailyRecord] = []
        self.closed_trades: list[ClosedTrade] = []
        """已平仓交易（卖出即记录一笔，供组合层做凯利统计与报告层做归因）。"""

        # 统计口径
        self.total_costs = 0.0
        self.cost_detail: dict[str, float] = {}
        self.turnover_amount = 0.0
        self.dividends_received = 0.0
        self.realized_pnl = 0.0
        self.peak_value = float(initial_cash)
        self._last_value = float(initial_cash)
        self._day_start_value = float(initial_cash)
        self._day_turnover = 0.0
        self._day_costs = 0.0
        self._day_dividends = 0.0

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def position(self, symbol: str) -> Position | None:
        return self.positions.get(symbol)

    def total_quantity(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        return pos.total_quantity if pos else 0.0

    def sellable(self, symbol: str) -> float:
        """可卖数量（已扣除 T+1 冻结）。"""
        pos = self.positions.get(symbol)
        return pos.sellable_quantity if pos else 0.0

    def available_cash(self) -> float:
        return self.cash

    @property
    def positions_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    @property
    def total_value(self) -> float:
        return self.cash + self.positions_value

    @property
    def gross_exposure(self) -> float:
        total = self.total_value
        return self.positions_value / total if total > _EPS else 0.0

    @property
    def holding_symbols(self) -> list[str]:
        return sorted(self.positions)

    @property
    def last_value(self) -> float:
        return self._last_value

    # ------------------------------------------------------------------ #
    # 状态变更
    # ------------------------------------------------------------------ #
    def unlock_t1(self) -> None:
        """开盘前解锁：昨日及以前买入的股票今日可卖。"""
        for pos in self.positions.values():
            pos.sellable_quantity = pos.total_quantity

    def apply_fill(self, fill: Fill) -> Position | None:
        """把成交回报应用到账户。"""
        symbol = fill.symbol
        qty = float(fill.quantity)
        gross = float(fill.gross_amount)
        costs = float(fill.total_cost)
        if qty <= 0:
            return self.positions.get(symbol)
        # 缺陷 #13：成交股数必须是整数股（A 股不存在小数股）。
        # 这是内部一致性约束，说明上游（风控削减 / 组合层 / 减仓控制）产生了不可执行的数量，
        # 属于 bug，因此显式报错而不是静默取整。
        if abs(qty - round(qty)) > 1e-9:
            raise EngineError(f"成交股数必须为整数股，收到 {qty!r}（订单 {fill.order_id}）")

        if fill.side is Side.BUY:
            pos = self.positions.get(symbol)
            if pos is None:
                pos = Position(symbol=symbol, opened_on=fill.trade_date)
                self.positions[symbol] = pos
            prev_qty = pos.total_quantity
            new_qty = prev_qty + qty
            pos.avg_cost = (pos.avg_cost * prev_qty + gross + costs) / new_qty if new_qty > _EPS else 0.0
            pos.total_quantity = new_qty
            pos.last_price = fill.price
            pos.cost_paid += costs
            pos.last_update = fill.trade_date
            # T+1：当日买入不增加可卖数量
            self.cash -= gross + costs
        else:
            pos = self.positions.get(symbol)
            if pos is None or pos.total_quantity + _EPS < qty:
                raise EngineError(
                    f"卖出数量超过持仓：{symbol} 请求 {qty}，持有 {0 if pos is None else pos.total_quantity}"
                )
            proceeds = gross - costs
            basis = pos.avg_cost * qty
            pnl = proceeds - basis
            pos.realized_pnl += pnl
            holding_days = 0
            if pos.opened_on is not None and fill.trade_date is not None:
                holding_days = max((fill.trade_date - pos.opened_on).days, 0)
            self.closed_trades.append(
                ClosedTrade(
                    symbol=symbol,
                    quantity=qty,
                    cost_basis=basis,
                    proceeds=proceeds,
                    pnl=pnl,
                    return_pct=(pnl / basis) if basis > _EPS else 0.0,
                    closed_on=fill.trade_date,
                    holding_days=holding_days,
                )
            )
            pos.total_quantity -= qty
            pos.sellable_quantity = max(0.0, pos.sellable_quantity - qty)
            pos.cost_paid += costs
            pos.last_update = fill.trade_date
            self.realized_pnl += pnl
            self.cash += proceeds
            if pos.total_quantity <= _EPS:
                del self.positions[symbol]
                pos = None

        self.total_costs += costs
        for key, value in fill.cost.as_dict().items():
            if key != "total":
                self.cost_detail[key] = self.cost_detail.get(key, 0.0) + value
        self.turnover_amount += gross
        self._day_costs += costs
        self._day_turnover += gross

        if self.cash < -1e-6:
            raise EngineError(
                f"现金透支：成交后现金 {self.cash:.6f}（{symbol} {fill.side.value} {qty}@{fill.price:.4f}）"
            )
        self.cash = max(self.cash, 0.0)
        return self.positions.get(symbol)

    def accrue_dividends(self, snapshot: MarketSnapshot) -> float:
        """开盘前计提现金分红（除权日），返回本次入账金额。"""
        total = 0.0
        for symbol, pos in list(self.positions.items()):
            bar = snapshot.bar(symbol)
            if bar is None or pos.total_quantity <= 0:
                continue
            dps = bar.dividend_per_share
            if dps > 0:
                amount = pos.total_quantity * dps
                self.cash += amount
                total += amount
        if total > 0:
            self.dividends_received += total
            self._day_dividends += total
        return total

    def mark_to_market(self, snapshot: MarketSnapshot) -> float:
        """按当日收盘价更新持仓市值，返回持仓市值。"""
        total = 0.0
        for symbol, pos in self.positions.items():
            bar = snapshot.bar(symbol)
            if bar is not None and bar.close > 0:
                pos.last_price = bar.close
                pos.last_update = bar.date
            total += pos.market_value
        return total

    def record_daily(self, day: DateLike, *, n_positions: int | None = None) -> DailyRecord:
        """记录当日净值并重置日内累计量。"""
        d = to_date(day)
        value = self.total_value
        prev = self._last_value
        daily_return = (value / prev - 1.0) if prev > _EPS else 0.0
        self.peak_value = max(self.peak_value, value)
        drawdown = (value / self.peak_value - 1.0) if self.peak_value > _EPS else 0.0
        record = DailyRecord(
            date=d,
            cash=self.cash,
            positions_value=self.positions_value,
            total_value=value,
            daily_return=daily_return,
            cumulative_return=value / self.initial_cash - 1.0,
            n_positions=n_positions if n_positions is not None else len(self.positions),
            gross_exposure=self.gross_exposure,
            turnover=self._day_turnover,
            costs=self._day_costs,
            dividends=self._day_dividends,
            realized_pnl=self.realized_pnl,
            drawdown=drawdown,
        )
        self.history.append(record)
        self._last_value = value
        self._day_turnover = 0.0
        self._day_costs = 0.0
        self._day_dividends = 0.0
        return record

    # ------------------------------------------------------------------ #
    # 导出
    # ------------------------------------------------------------------ #
    def trade_returns(self) -> list[float]:
        """已平仓交易的收益率序列（供组合层估计凯利参数）。"""
        return [t.return_pct for t in self.closed_trades]

    def kelly_sizing(self) -> Any:
        """返回基于已平仓交易的凯利统计（需要 ``aqs.portfolio.sizing``）。"""
        from ..portfolio.sizing import kelly_stats_from_trades

        return kelly_stats_from_trades(self.trade_returns())

    def equity_curve(self) -> Any:
        """返回净值曲线 DataFrame（需要 pandas）。"""
        import pandas as pd

        if not self.history:
            return pd.DataFrame(columns=list(DailyRecord(_date(2000, 1, 1), 0, 0, 0).as_dict().keys()))
        return pd.DataFrame([r.as_dict() for r in self.history]).set_index("date")

    def positions_frame(self) -> Any:
        """返回当前持仓表。"""
        import pandas as pd

        rows = [p.to_dict() for p in self.positions.values()]
        return pd.DataFrame(rows) if rows else pd.DataFrame(
            columns=["symbol", "total_quantity", "sellable_quantity", "avg_cost", "last_price", "market_value",
                     "unrealized_pnl", "realized_pnl", "unrealized_return"]
        )

    def summary(self) -> dict[str, Any]:
        return {
            "initial_cash": self.initial_cash,
            "cash": self.cash,
            "positions_value": self.positions_value,
            "total_value": self.total_value,
            "total_return": self.total_value / self.initial_cash - 1.0,
            "n_positions": len(self.positions),
            "total_costs": self.total_costs,
            "cost_detail": dict(self.cost_detail),
            "turnover_amount": self.turnover_amount,
            "dividends_received": self.dividends_received,
            "realized_pnl": self.realized_pnl,
            "gross_exposure": self.gross_exposure,
        }
