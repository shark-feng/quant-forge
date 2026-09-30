"""VaR / ES（历史模拟法）+ 滚动突破率回溯。

合同 §五 要求第一阶段实现「VaR/ES 历史模拟法」，验收标准是
**突破率接近 1 - 置信度**（例如 95% VaR 的突破率应接近 5%）。

约定：
- 收益率序列为**简单日收益**（小数）；
- VaR/ES 以**正数**表示损失幅度（0.03 表示 3%）；
- 持有期 ``horizon > 1`` 时按平方根法则缩放（``σ ∝ √h``），这是日频研究的常规近似。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from ..core.exceptions import ConfigError, InsufficientDataError

__all__ = [
    "VaRResult",
    "BreachResult",
    "historical_var_es",
    "quantile_loss",
    "expected_shortfall",
    "portfolio_returns",
    "var_breach_rate",
]


@dataclass(frozen=True, slots=True)
class VaRResult:
    """VaR / ES 结果（损失为正数）。"""

    var: float
    es: float
    confidence: float
    horizon: int
    n_obs: int
    quantile_return: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "var": self.var,
            "es": self.es,
            "confidence": self.confidence,
            "horizon": self.horizon,
            "n_obs": self.n_obs,
            "quantile_return": self.quantile_return,
        }


@dataclass(frozen=True, slots=True)
class BreachResult:
    """滚动 VaR 回溯结果。"""

    confidence: float
    n_obs: int
    n_breach: int
    breach_rate: float
    expected_rate: float

    @property
    def within_tolerance(self) -> bool:
        """突破率是否落在 1±50% 的相对区间内（粗略但可用的验收口径）。"""
        expected = self.expected_rate
        return abs(self.breach_rate - expected) <= 0.5 * expected

    def as_dict(self) -> dict[str, float | int]:
        return {
            "confidence": self.confidence,
            "n_obs": self.n_obs,
            "n_breach": self.n_breach,
            "breach_rate": self.breach_rate,
            "expected_rate": self.expected_rate,
        }


def _clean(returns: Sequence[float], *, min_obs: int = 2) -> np.ndarray:
    arr = np.asarray([float(r) for r in returns], dtype="float64")
    arr = arr[np.isfinite(arr)]
    if arr.size < min_obs:
        raise InsufficientDataError(f"VaR 需要至少 {min_obs} 个有效收益样本，实际 {arr.size} 个")
    return arr


def quantile_loss(returns: Sequence[float], confidence: float = 0.95) -> float:
    """历史模拟的分位数损失（正数）。"""
    if not 0.0 < confidence < 1.0:
        raise ConfigError("confidence 必须在 (0, 1)", path="risk.var.confidence", value=confidence)
    arr = _clean(returns)
    q = float(np.quantile(arr, 1.0 - confidence))
    return -q


def expected_shortfall(returns: Sequence[float], confidence: float = 0.95) -> float:
    """ES / CVaR：尾部（<= VaR 分位点）的平均损失（正数）。"""
    if not 0.0 < confidence < 1.0:
        raise ConfigError("confidence 必须在 (0, 1)", path="risk.var.confidence", value=confidence)
    arr = _clean(returns)
    q = float(np.quantile(arr, 1.0 - confidence))
    tail = arr[arr <= q]
    if tail.size == 0:
        return -q
    return float(-tail.mean())


def historical_var_es(
    returns: Sequence[float],
    *,
    confidence: float = 0.95,
    horizon: int = 1,
    min_obs: int = 20,
) -> VaRResult:
    """历史模拟法 VaR / ES。

    Args:
        returns: 历史（日）收益率序列。
        confidence: 置信度，例如 0.95 / 0.99。
        horizon: 持有期（交易日）；>1 时按 ``sqrt(horizon)`` 缩放。
        min_obs: 最少样本数，不足时抛 :class:`InsufficientDataError`。
    """
    if horizon < 1:
        raise ConfigError("horizon 必须 >= 1", path="risk.var.horizon", value=horizon)
    arr = _clean(returns, min_obs=min_obs)
    q = float(np.quantile(arr, 1.0 - confidence))
    tail = arr[arr <= q]
    scale = float(np.sqrt(horizon))
    var = -q * scale
    es = (-float(tail.mean()) if tail.size else -q) * scale
    return VaRResult(
        var=float(var),
        es=float(es),
        confidence=confidence,
        horizon=horizon,
        n_obs=int(arr.size),
        quantile_return=q,
    )


def portfolio_returns(
    weights: Sequence[float] | np.ndarray,
    returns_matrix: np.ndarray,
    *,
    normalize: bool = True,
) -> np.ndarray:
    """由权重与资产收益矩阵计算组合收益序列。

    Args:
        weights: 各资产权重（按列顺序）。
        returns_matrix: 形状 ``(T, N)`` 的资产收益矩阵。
        normalize: 权重和不为 1 时是否按剩余现金折算（默认 True：组合收益 = Σ w_i r_i，
            权重和 < 1 表示部分现金，其收益为 0）。
    """
    w = np.asarray([float(x) for x in weights], dtype="float64")
    r = np.asarray(returns_matrix, dtype="float64")
    if r.ndim != 2:
        raise ConfigError("returns_matrix 必须是二维矩阵", value=r.shape)
    if w.shape[0] != r.shape[1]:
        raise ConfigError(
            f"权重数量 {w.shape[0]} 与收益矩阵列数 {r.shape[1]} 不一致", path="risk.var.weights"
        )
    total = float(w.sum())
    if total > 1.0 + 1e-9:
        raise ConfigError(f"权重之和不能大于 1（禁止杠杆），实际 {total:.4f}", path="risk.var.weights")
    if normalize and total > 0:
        # 权重和 <1 的部分视为现金（收益 0），不放大剩余仓位
        return r @ w
    return r @ w


def var_breach_rate(
    returns: Sequence[float],
    *,
    confidence: float = 0.95,
    window: int = 250,
    min_window: int = 60,
) -> BreachResult:
    """滚动 VaR 回溯：用过去 ``window`` 日估计 VaR，检验次日收益是否突破。

    验收口径：突破率应接近 ``1 - confidence``。
    """
    arr = _clean(returns, min_obs=min_window + 2)
    if window < min_window:
        raise ConfigError(f"window 必须 >= {min_window}", path="risk.var.window", value=window)
    breaches = 0
    observations = 0
    for t in range(window, arr.size):
        hist = arr[t - window : t]
        var = -float(np.quantile(hist, 1.0 - confidence))
        observations += 1
        if arr[t] < -var:
            breaches += 1
    rate = breaches / observations if observations else 0.0
    return BreachResult(
        confidence=confidence,
        n_obs=observations,
        n_breach=breaches,
        breach_rate=rate,
        expected_rate=1.0 - confidence,
    )
