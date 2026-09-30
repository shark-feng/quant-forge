"""交易成本模型。

对应合同 §六「成本模型」：

    总成本 = 固定费用 + 佣金 + 印花税 + 过户费 + 滑点 + 冲击成本
    滑点   = 固定滑点 + 比例滑点
    冲击成本 = 成交额 × η × (Q / ADV)^θ

实现口径（避免重复计费的关键约定）：
- **滑点**通过**成交价偏移**体现（买入抬价、卖出压价），其金额 =
  ``|qty| × |成交价 − 参考价|``；当 ``slippage_price_adjust=False`` 时改为按比例直接计费。
- **冲击成本**作为独立的成本科目计费（不再改变成交价）。
- ``config.scale`` 对所有成本科目（含最低佣金）统一缩放，用于「成本加倍」敏感性测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from typing import Any, Mapping

from ..config.schema import CostConfig, as_config
from ..core.dates import DateLike, to_date
from ..core.enums import Side
from ..core.logging import get_logger
from ..core.models import CostBreakdown

__all__ = ["CostModel", "CostResult"]

logger = get_logger("engine.cost")


@dataclass(frozen=True, slots=True)
class CostResult:
    """一次撮合的成本计算结果。"""

    executed_price: float
    reference_price: float
    gross_amount: float
    breakdown: CostBreakdown
    slippage_rate: float = 0.0
    impact_fraction: float = 0.0

    @property
    def total_cost(self) -> float:
        return self.breakdown.total

    def as_dict(self) -> dict[str, Any]:
        d = {
            "executed_price": self.executed_price,
            "reference_price": self.reference_price,
            "gross_amount": self.gross_amount,
            "total_cost": self.total_cost,
            "slippage_rate": self.slippage_rate,
            "impact_fraction": self.impact_fraction,
        }
        d.update(self.breakdown.as_dict())
        return d


class CostModel:
    """按配置计算佣金、印花税、过户费、滑点与冲击成本。"""

    def __init__(self, config: CostConfig | Mapping[str, Any] | None = None) -> None:
        self.config = as_config(config, CostConfig)
        self.missing_adv_count = 0
        """ADV 缺失导致冲击成本无法计算的次数（回测诊断用，非 0 时应核查股票池与 ADV 窗口）。"""

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    @property
    def scale(self) -> float:
        return float(self.config.scale)

    def stamp_tax_rate(self, day: DateLike) -> float:
        """返回某日生效的印花税率（卖方单边）。"""
        return self.config.stamp_tax.rate_on(to_date(day))

    def impact_fraction(self, quantity: float, adv_volume: float | None) -> float:
        """冲击成本比例 η·(Q/ADV)^θ；ADV 不可用时返回 0 并计入诊断计数。"""
        cfg = self.config.impact
        if not cfg.enabled or quantity <= 0:
            return 0.0
        if not adv_volume or adv_volume <= 0:
            self.missing_adv_count += 1
            return 0.0
        ratio = quantity / adv_volume
        return float(cfg.eta * (ratio**cfg.theta))

    # ------------------------------------------------------------------ #
    # 计算
    # ------------------------------------------------------------------ #
    def compute(
        self,
        *,
        side: Side,
        quantity: float,
        reference_price: float,
        trade_date: DateLike,
        adv_volume: float | None = None,
        apply_price_adjust: bool = True,
    ) -> CostResult:
        """计算成交价与成本明细。

        Args:
            side: 买卖方向（决定滑点方向与印花税）。
            quantity: 成交数量（正数，单位股）。
            reference_price: 未考虑滑点的参考价（T+1 开盘价 / VWAP 等）。
            trade_date: 成交日（决定印花税率）。
            adv_volume: 近 N 日日均成交量（股），用于冲击成本；缺失则该科目为 0。
            apply_price_adjust: 是否用成交价偏移体现滑点。
        """
        qty = abs(float(quantity))
        ref = float(reference_price)
        cfg = self.config
        scale = self.scale
        # 滑点率同样受 scale 影响（成本加倍时成交价也更差），使「成本加倍」语义自洽
        slip_rate = cfg.slippage.rate * scale

        if qty <= 0 or ref <= 0:
            return CostResult(ref, ref, 0.0, CostBreakdown())

        if apply_price_adjust:
            executed = ref * (1.0 + slip_rate * side.sign)
            slippage_cost = qty * abs(executed - ref)  # 已包含 scale
        else:
            executed = ref
            slippage_cost = qty * ref * slip_rate

        gross = qty * executed
        commission = max(gross * cfg.commission_rate, cfg.min_commission)
        stamp = gross * self.stamp_tax_rate(trade_date) if side is Side.SELL else 0.0
        transfer = gross * cfg.transfer_fee_rate
        impact_frac = self.impact_fraction(qty, adv_volume)
        impact_cost = gross * impact_frac

        breakdown = CostBreakdown(
            fixed_fee=cfg.fixed_fee_per_order * scale,
            commission=commission * scale,
            stamp_tax=stamp * scale,
            transfer_fee=transfer * scale,
            slippage=slippage_cost,
            impact=impact_cost * scale,
        )
        return CostResult(
            executed_price=executed,
            reference_price=ref,
            gross_amount=gross,
            breakdown=breakdown,
            slippage_rate=slip_rate,
            impact_fraction=impact_frac,
        )

    # ------------------------------------------------------------------ #
    # 参考信息
    # ------------------------------------------------------------------ #
    def round_trip_rate(self, *, adv_volume: float | None = None, notional: float = 1e6, day: DateLike | None = None) -> float:
        """估算一次买卖往返的成本率（用于容量评估与成本敏感性分析）。"""
        day = day or _date(2024, 1, 1)
        price = 10.0
        qty = notional / price
        buy = self.compute(
            side=Side.BUY, quantity=qty, reference_price=price, trade_date=day, adv_volume=adv_volume
        )
        sell = self.compute(
            side=Side.SELL, quantity=qty, reference_price=price, trade_date=day, adv_volume=adv_volume
        )
        return (buy.total_cost + sell.total_cost) / notional

    def describe(self) -> dict[str, Any]:
        cfg = self.config
        return {
            "scale": cfg.scale,
            "commission_rate": cfg.commission_rate,
            "min_commission": cfg.min_commission,
            "transfer_fee_rate": cfg.transfer_fee_rate,
            "stamp_tax_schedule": [
                {"effective_from": str(r.effective_from), "rate": r.rate} for r in cfg.stamp_tax.schedule
            ],
            "slippage_bps": {"fixed": cfg.slippage.fixed_bps, "proportional": cfg.slippage.prop_bps},
            "impact": {"enabled": cfg.impact.enabled, "eta": cfg.impact.eta, "theta": cfg.impact.theta},
        }

    def scaled(self, k: float) -> "CostModel":
        """返回成本缩放后的新模型（示例：``CostModel(cfg).scaled(2.0)`` = 成本加倍）。"""
        return CostModel(self.config.scaled(k))
