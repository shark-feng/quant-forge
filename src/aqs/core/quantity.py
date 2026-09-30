"""数量归一化工具（缺陷 #13）。

**唯一实现**：风控层（削减量）、引擎层（下单前校验）、组合控制层（减仓量）都从这里取整，
避免「每个消费点各自记一遍取整」而漏掉某处。

A 股物理约束（本项目严格遵守）：

- **不存在小数股** —— 任何订单数量、成交数量、持仓数量都必须是整数股；
- **买入必须整手** —— 买入量必须是 ``lot_size``（默认 100）的正整数倍；
- **卖出可以是零股** —— 但股数仍为整数；零股的常见合法场景是「一次性卖掉不足一手的全部余额」。
"""

from __future__ import annotations

__all__ = ["floor_lot", "whole_shares", "sell_quantity"]

DEFAULT_LOT_SIZE = 100
"""A 股主板每手股数。业务代码请从 ``EngineConfig.lot_size`` 取值，不要直接引用本常量。"""


def floor_lot(quantity: float, lot: int | None = None) -> float:
    """向下取整到整手；``lot`` 为 ``None``/``<= 0`` 时退化为「整数股」向下取整。

    ``quantity <= 0`` 返回 ``0.0``。
    """
    q = float(quantity)
    if q <= 0:
        return 0.0
    if lot is None or lot <= 0:
        return float(int(q))
    return float(int(q // lot) * lot)


def whole_shares(quantity: float) -> float:
    """向下取整为整数股（不做整手约束）。``quantity < 1`` 返回 ``0.0``。"""
    q = float(quantity)
    if q < 1.0:
        return 0.0
    return float(int(q))


def sell_quantity(quantity: float, lot: int | None = None) -> float:
    """卖出量归一化：结果为**整数股**。

    - ``quantity >= lot``：按整手向下取整（例如 250 → 200）；
    - ``0 < quantity < lot``：保留整数零股（例如 40.3 → 40），满足「卖掉不足一手余额」的合法场景；
    - ``quantity < 1``：``0.0``（无可行股数）。
    """
    q = float(quantity)
    if q <= 0:
        return 0.0
    if lot is not None and lot > 0 and q >= lot:
        return float(int(q // lot) * lot)
    return whole_shares(q)
