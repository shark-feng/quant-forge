"""撮合引擎：A 股硬约束判定 + 成交价 + 成交量。

判定顺序（短路，先判先拒）见 ``docs/02_engine_skeleton.md`` §3.1：

    数据存在 → 未退市 → 未停牌 → 涨跌停 → 限价 → T+1/持仓 → 整手 → 资金 → 参与率

**重要口径**：涨跌停、成交价、费用一律使用**原始价**（后复权价仅用于信号与收益）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Mapping, Protocol

import numpy as np

from ..config.schema import EngineConfig, as_config
from ..core.enums import OrderType, RejectReason, Side, TradingStatus
from ..core.exceptions import EngineError
from ..core.logging import get_logger
from ..core.models import Bar, CostBreakdown, MarketSnapshot, Order
from ..core.quantity import sell_quantity
from .cost import CostModel

__all__ = ["MatchResult", "MatchingEngine", "AccountView"]

logger = get_logger("engine.matching")

_FALLBACK_DAY = _date(2000, 1, 1)
"""``affordable_quantity`` 在未指定交易日时使用的占位日期（买入不涉印花税，不影响结果）。"""


class AccountView(Protocol):
    """撮合所需的账户只读视图。"""

    def sellable(self, symbol: str) -> float: ...

    def available_cash(self) -> float: ...

    def total_quantity(self, symbol: str) -> float: ...


@dataclass(frozen=True, slots=True)
class MatchResult:
    """一次撮合尝试的结果。"""

    filled_quantity: float = 0.0
    price: float = math.nan
    reason: RejectReason = RejectReason.NONE
    can_retry: bool = True
    status: TradingStatus = TradingStatus.NO_QUOTE
    partial: bool = False
    cost: CostBreakdown = field(default_factory=CostBreakdown)
    liquidity_cap: float = math.nan
    queue_remaining: float = 0.0
    residue_quantity: float = 0.0
    """因不足一手而**永远无法成交**的剩余量（缺陷 #13）。

    只在「部分成交后剩余 < 1 手」时非零；由 :class:`~aqs.engine.broker.Broker`
    据此把订单判为 EXPIRED 而不是 REJECTED。
    """

    @property
    def filled(self) -> bool:
        return self.filled_quantity > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "filled_quantity": self.filled_quantity,
            "price": self.price,
            "reason": self.reason.value,
            "can_retry": self.can_retry,
            "status": self.status.value,
            "partial": self.partial,
            "cost": self.cost.as_dict(),
        }


class MatchingEngine:
    """日频撮合引擎（可替换：第三阶段将替换为订单簿撮合）。"""

    def __init__(
        self,
        engine_config: EngineConfig | Mapping[str, Any] | None = None,
        *,
        cost_model: CostModel | None = None,
        seed: int = 20240101,
    ) -> None:
        self.config = as_config(engine_config, EngineConfig)
        self.cost_model = cost_model or CostModel()
        self._rng = np.random.default_rng(seed)
        self.stats: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    @property
    def lot_size(self) -> int:
        return int(self.config.lot_size)

    def floor_lot(self, quantity: float, lot: int | None = None) -> float:
        """向下取整到整手（买入用）。"""
        lot = lot or self.lot_size
        if quantity <= 0:
            return 0.0
        return float(int(quantity // lot) * lot)

    def affordable_quantity(
        self,
        cash: float,
        price: float,
        lot: int | None = None,
        *,
        adv_volume: float | None = None,
        trade_date: Any = None,
        buffer: float = 0.0,
    ) -> float:
        """给定现金可买入的整手数量。

        **必须与成本模型完全一致**：包括滑点、佣金（含最低佣金）、过户费与冲击成本。
        先用保守估计取上界，再用 :meth:`CostModel.compute` 精确校验并逐手回退，
        保证 `成交量 × 成交价 + 全部成本 <= 现金`，杜绝现金透支。

        Args:
            buffer: 额外预留的现金缓冲（比例），例如 0.005 表示多留 0.5%。
        """
        lot = lot or self.lot_size
        if cash <= 0 or price <= 0:
            return 0.0
        budget = cash * (1.0 - max(buffer, 0.0))
        cfg = self.cost_model.config
        scale = self.cost_model.scale
        slip = cfg.slippage.rate * scale
        # 冲击成本按「全部现金买入」的最坏情形估计，作为上界
        impact_guess = self.cost_model.impact_fraction(budget / max(price, 1e-9), adv_volume)
        unit = price * (1.0 + slip) * (1.0 + cfg.commission_rate + cfg.transfer_fee_rate + impact_guess)
        lots = int(budget / (unit * lot))
        day = trade_date if trade_date is not None else _FALLBACK_DAY
        while lots > 0:
            qty = float(lots * lot)
            result = self.cost_model.compute(
                side=Side.BUY,
                quantity=qty,
                reference_price=price,
                trade_date=day,
                adv_volume=adv_volume,
                apply_price_adjust=self.config.matching.slippage_price_adjust,
            )
            if result.gross_amount + result.total_cost <= budget + 1e-9:
                return qty
            lots -= 1
        return 0.0

    def liquidity_cap(self, bar: Bar) -> float:
        """当日可成交量上限（参与率约束）。"""
        if bar.volume <= 0:
            return 0.0
        return self.floor_lot(bar.volume * self.config.matching.max_participation)

    # ------------------------------------------------------------------ #
    # 参考价
    # ------------------------------------------------------------------ #
    def _reference_price(self, bar: Bar, order: Order) -> tuple[float, RejectReason | None]:
        """确定未含滑点的参考成交价；不可成交时返回原因。"""
        mode = self.config.execution_price
        if mode == "vwap":
            price = bar.vwap
        elif mode == "close":
            price = bar.close
        else:
            price = bar.open
        if price <= 0:
            return math.nan, RejectReason.NO_QUOTE

        if order.order_type is OrderType.LIMIT and order.limit_price is not None:
            limit = float(order.limit_price)
            if order.side is Side.BUY:
                if bar.low > limit:
                    return math.nan, RejectReason.PRICE_OUT_OF_RANGE
                price = min(price, limit)
            else:
                if bar.high < limit:
                    return math.nan, RejectReason.PRICE_OUT_OF_RANGE
                price = max(price, limit)
        return price, None

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def try_match(
        self,
        order: Order,
        snapshot: MarketSnapshot,
        *,
        account: AccountView | None = None,
    ) -> MatchResult:
        """尝试撮合一笔订单，返回 :class:`MatchResult`（不修改订单状态）。"""
        bar = snapshot.bar(order.symbol)
        if bar is None:
            return self._bump(MatchResult(reason=RejectReason.NO_QUOTE, can_retry=True, status=TradingStatus.NO_QUOTE))

        if bar.delist_date is not None and bar.date > bar.delist_date:
            return self._bump(
                MatchResult(reason=RejectReason.DELISTED, can_retry=False, status=TradingStatus.DELISTED)
            )
        if bar.is_suspended or bar.volume <= 0:
            return self._bump(
                MatchResult(reason=RejectReason.SUSPENDED, can_retry=True, status=TradingStatus.SUSPENDED)
            )

        # 涨跌停：开盘即封板则当日无法成交（可按配置给一个小概率“抢到”）
        prob = float(self.config.matching.limit_up_fill_prob)
        if order.side is Side.BUY and bar.is_limit_up(bar.open) and self._rng.random() >= prob:
            return self._bump(MatchResult(reason=RejectReason.LIMIT_UP, can_retry=True, status=TradingStatus.LIMIT_UP))
        if order.side is Side.SELL and bar.is_limit_down(bar.open) and self._rng.random() >= prob:
            return self._bump(
                MatchResult(reason=RejectReason.LIMIT_DOWN, can_retry=True, status=TradingStatus.LIMIT_DOWN)
            )

        price, reason = self._reference_price(bar, order)
        if reason is not None:
            return self._bump(
                MatchResult(reason=reason, can_retry=True, status=bar.trading_status(bar.open))
            )

        lot = self.lot_size
        qty = order.remaining
        if qty <= 0:
            # 缺陷 #13：没有可撮合数量。旧实现返回默认 MatchResult（can_retry=True），
            # 会让订单每个交易日 defer 一次、直到 max_defer_days 才 EXPIRED —— 形成
            # 「僵尸订单」。这里显式声明不可重试，交由 Broker 立即结算。
            return MatchResult(can_retry=False, status=TradingStatus.NORMAL)

        # ---- 持仓 / T+1 / 资金 ----
        if order.side is Side.SELL:
            available = account.sellable(order.symbol) if account is not None else math.inf
            if available <= 0:
                held = account.total_quantity(order.symbol) if account is not None else 0.0
                reason_ = RejectReason.T1_LOCK if held > 0 else RejectReason.INSUFFICIENT_POSITION
                return self._bump(MatchResult(reason=reason_, can_retry=held > 0, status=TradingStatus.NORMAL))
            if qty > available:
                # 缺陷 #13：可卖量取整为整数股，绝不产生小数股卖单
                qty = sell_quantity(available)
        else:
            qty = self.floor_lot(qty, lot)
            if qty < lot:
                # 不足一手：若此前已部分成交，则剩余部分是**永远无法成交的零头**，
                # 应记为 residue（→ EXPIRED）而不是硬拒单（→ REJECTED + 拒单率虚增）。
                residue = order.remaining if order.filled_quantity > 0 else 0.0
                return self._bump(
                    MatchResult(
                        reason=RejectReason.LOT_SIZE,
                        can_retry=False,
                        status=TradingStatus.NORMAL,
                        residue_quantity=float(residue),
                    )
                )
            if account is not None:
                affordable = self.affordable_quantity(
                    account.available_cash(),
                    price,
                    lot,
                    adv_volume=snapshot.adv_v(order.symbol),
                    trade_date=snapshot.date,
                )
                if affordable <= 0:
                    return self._bump(
                        MatchResult(reason=RejectReason.INSUFFICIENT_CASH, can_retry=True, status=TradingStatus.NORMAL)
                    )
                if qty > affordable:
                    qty = affordable

        # ---- 参与率上限（部分成交） ----
        cap = self.liquidity_cap(bar)
        if cap <= 0:
            return self._bump(
                MatchResult(
                    reason=RejectReason.INSUFFICIENT_LIQUIDITY,
                    can_retry=True,
                    status=TradingStatus.NORMAL,
                    liquidity_cap=0.0,
                )
            )
        partial = qty > cap
        if partial:
            if not self.config.matching.allow_partial_fill:
                return self._bump(
                    MatchResult(
                        reason=RejectReason.INSUFFICIENT_LIQUIDITY,
                        can_retry=True,
                        status=TradingStatus.NORMAL,
                        liquidity_cap=cap,
                    )
                )
            qty = cap

        # ---- 成本 ----
        cost_result = self.cost_model.compute(
            side=order.side,
            quantity=qty,
            reference_price=price,
            trade_date=snapshot.date,
            adv_volume=snapshot.adv_v(order.symbol),
            apply_price_adjust=self.config.matching.slippage_price_adjust,
        )
        result = MatchResult(
            filled_quantity=qty,
            price=cost_result.executed_price,
            reason=RejectReason.NONE,
            can_retry=qty < order.remaining,
            status=bar.trading_status(bar.open),
            partial=qty < order.remaining,
            cost=cost_result.breakdown,
            liquidity_cap=cap,
            queue_remaining=order.remaining - qty,
        )
        return self._bump(result)

    # ------------------------------------------------------------------ #
    def _bump(self, result: MatchResult) -> MatchResult:
        key = result.reason.value if result.reason is not RejectReason.NONE else "filled"
        self.stats[key] = self.stats.get(key, 0) + 1
        return result

    def describe(self) -> dict[str, Any]:
        cfg = self.config
        return {
            "execution_price": cfg.execution_price,
            "lot_size": cfg.lot_size,
            "t_plus_one": cfg.t_plus_one,
            "max_participation": cfg.matching.max_participation,
            "allow_partial_fill": cfg.matching.allow_partial_fill,
            "limit_up_fill_prob": cfg.matching.limit_up_fill_prob,
            "price_limit": {
                "enabled": cfg.price_limit.enabled,
                "main": cfg.price_limit.main_board_pct,
                "gem_star": cfg.price_limit.gem_pct,
                "bse": cfg.price_limit.bse_pct,
                "st": cfg.price_limit.st_pct,
            },
            "match_stats": dict(self.stats),
        }


def ensure_positive_lot(quantity: float, lot: int = 100) -> float:
    """校验数量为整手（买入场景的硬约束）。"""
    if quantity <= 0 or quantity % lot != 0:
        raise EngineError(f"买入数量必须为 {lot} 的正整数倍，收到 {quantity}")
    return float(quantity)
