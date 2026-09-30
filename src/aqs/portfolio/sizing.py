"""仓位计算：等权、单票上限、最大持仓数、凯利公式简化版（半凯利）。

全部为**纯函数**：输入信号/统计量，输出权重与敞口，便于单独测试与复用。

凯利公式（合同 §五 要求第一阶段实现「简化版 + 半凯利」）：

    二元版：f* = p - q / b          p=胜率, q=1-p, b=赔率(平均盈利/平均亏损)
    连续版：f* = μ / σ²
    实际敞口 = clip(half_kelly × f*, 0, cap)

统计量来自账户的**已平仓交易**；样本不足时使用保守的初始敞口，
避免「用 3 笔交易算出满仓」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..core.exceptions import ConfigError

__all__ = [
    "KellySizing",
    "SizingConfig",
    "KellyStats",
    "KellyResult",
    "kelly_binary",
    "kelly_continuous",
    "kelly_from_trades",
    "kelly_stats_from_trades",
    "equal_weights",
    "score_weights",
    "cap_weights",
]

WEIGHTING_MODES = ("equal", "score_prop")
KELLY_MODES = ("binary", "continuous")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class KellySizing:
    """凯利仓位控制参数。"""

    enabled: bool = False
    mode: str = "binary"          # binary | continuous
    fraction: float = 0.5         # 半凯利系数
    cap: float = 1.0              # 总敞口上限（占总资产比例）
    min_trades: int = 20          # 启用凯利所需的最小平仓样本数
    initial_exposure: float = 1.0  # 样本不足时的敞口

    def __post_init__(self) -> None:
        if self.mode not in KELLY_MODES:
            raise ConfigError(f"kelly.mode 只能是 {list(KELLY_MODES)}", path="portfolio.kelly.mode", value=self.mode)
        if not 0.0 < self.fraction <= 1.0:
            raise ConfigError("kelly.fraction 必须在 (0, 1]", path="portfolio.kelly.fraction", value=self.fraction)
        if not 0.0 < self.cap <= 1.0:
            raise ConfigError("kelly.cap 必须在 (0, 1]", path="portfolio.kelly.cap", value=self.cap)
        if self.min_trades < 0:
            raise ConfigError("kelly.min_trades 不能为负", path="portfolio.kelly.min_trades", value=self.min_trades)
        if not 0.0 <= self.initial_exposure <= 1.0:
            raise ConfigError(
                "kelly.initial_exposure 必须在 [0, 1]", path="portfolio.kelly.initial_exposure", value=self.initial_exposure
            )


@dataclass(slots=True)
class SizingConfig:
    """组合仓位配置。"""

    weighting: str = "equal"                 # equal | score_prop
    max_positions: int = 10
    max_weight_per_symbol: float = 0.10
    cash_buffer: float = 0.02
    lot_size: int = 100
    allow_reentry: bool = True
    rebalance: bool = False
    kelly: KellySizing = field(default_factory=KellySizing)

    def __post_init__(self) -> None:
        if self.weighting not in WEIGHTING_MODES:
            raise ConfigError(
                f"portfolio.weighting 只能是 {list(WEIGHTING_MODES)}",
                path="portfolio.weighting",
                value=self.weighting,
            )
        if self.max_positions < 1:
            raise ConfigError("portfolio.max_positions 必须 >= 1", path="portfolio.max_positions", value=self.max_positions)
        if not 0.0 < self.max_weight_per_symbol <= 1.0:
            raise ConfigError(
                "portfolio.max_weight_per_symbol 必须在 (0, 1]",
                path="portfolio.max_weight_per_symbol",
                value=self.max_weight_per_symbol,
            )
        if not 0.0 <= self.cash_buffer < 1.0:
            raise ConfigError("portfolio.cash_buffer 必须在 [0, 1)", path="portfolio.cash_buffer", value=self.cash_buffer)
        if self.lot_size <= 0:
            raise ConfigError("portfolio.lot_size 必须为正", path="portfolio.lot_size", value=self.lot_size)


# --------------------------------------------------------------------------- #
# 凯利公式
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class KellyStats:
    """平仓样本统计。"""

    n_trades: int = 0
    win_rate: float = 0.0
    payoff_ratio: float = 1.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    mean_return: float = 0.0
    std_return: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_trades": self.n_trades,
            "win_rate": self.win_rate,
            "payoff_ratio": self.payoff_ratio,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "mean_return": self.mean_return,
            "std_return": self.std_return,
        }


@dataclass(frozen=True, slots=True)
class KellyResult:
    """凯利仓位计算结果。"""

    exposure: float
    f_star: float
    fraction: float
    ready: bool
    mode: str
    stats: KellyStats = field(default_factory=KellyStats)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "exposure": self.exposure,
            "f_star": self.f_star,
            "half_kelly_fraction": self.fraction,
            "ready": self.ready,
            "mode": self.mode,
            "note": self.note,
            **{f"stats_{k}": v for k, v in self.stats.as_dict().items()},
        }


def kelly_binary(win_rate: float, payoff_ratio: float) -> float:
    """二元凯利：``f* = p - q / b``。

    - ``p=0.5, b=1`` → 0（无优势不下注）；
    - ``p`` 越高或赔率越大，``f*`` 越大；
    - 无亏损样本时赔率取下限 1.0（不放大仓位）。
    """
    if not 0.0 <= win_rate <= 1.0:
        raise ConfigError("win_rate 必须在 [0, 1]", path="portfolio.kelly.win_rate", value=win_rate)
    b = max(float(payoff_ratio), 0.0)
    if b <= 0:
        return 0.0
    return float(win_rate - (1.0 - win_rate) / b)


def kelly_continuous(mean_return: float, std_return: float) -> float:
    """连续凯利：``f* = μ / σ²``（σ<=0 时返回 0）。"""
    if std_return <= 0:
        return 0.0
    return float(mean_return / (std_return**2))


def kelly_stats_from_trades(trades: Sequence[float]) -> KellyStats:
    """由已平仓交易的收益率序列计算胜率与赔率。"""
    values = [float(t) for t in trades if t is not None]
    if not values:
        return KellyStats()
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    n = len(values)
    win_rate = len(wins) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    payoff = (avg_win / abs(avg_loss)) if avg_loss < 0 else 1.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n if n > 1 else 0.0
    return KellyStats(
        n_trades=n,
        win_rate=win_rate,
        payoff_ratio=payoff,
        avg_win=avg_win,
        avg_loss=avg_loss,
        mean_return=mean,
        std_return=var**0.5,
    )


def kelly_from_trades(trades: Sequence[float], config: KellySizing | None = None) -> KellyResult:
    """按已平仓交易样本计算半凯利敞口。

    样本不足（少于 ``min_trades``）时返回 ``initial_exposure``，
    并置 ``ready=False``，报告中必须标注「凯利样本不足」。
    """
    cfg = config or KellySizing()
    stats = kelly_stats_from_trades(trades)
    if stats.n_trades < cfg.min_trades:
        return KellyResult(
            exposure=cfg.initial_exposure,
            f_star=0.0,
            fraction=cfg.fraction,
            ready=False,
            mode=cfg.mode,
            stats=stats,
            note=f"样本不足（{stats.n_trades}/{cfg.min_trades} 笔），使用初始敞口 {cfg.initial_exposure:.2%}",
        )

    if cfg.mode == "continuous":
        f_star = kelly_continuous(stats.mean_return, stats.std_return)
    else:
        f_star = kelly_binary(stats.win_rate, stats.payoff_ratio)
    exposure = max(0.0, min(cfg.fraction * f_star, cfg.cap))
    note = "" if f_star > 0 else "凯利公式给出非正仓位（无统计优势），敞口置 0"
    return KellyResult(
        exposure=exposure,
        f_star=f_star,
        fraction=cfg.fraction,
        ready=True,
        mode=cfg.mode,
        stats=stats,
        note=note,
    )


# --------------------------------------------------------------------------- #
# 权重分配
# --------------------------------------------------------------------------- #
def cap_weights(weights: Mapping[str, float], max_weight: float) -> dict[str, float]:
    """对单票权重施加上限（超出部分不重新分配，宁可留现金）。"""
    if max_weight <= 0:
        raise ConfigError("max_weight 必须为正", path="portfolio.max_weight_per_symbol", value=max_weight)
    return {sym: float(min(max(w, 0.0), max_weight)) for sym, w in weights.items()}


def equal_weights(symbols: Sequence[str], *, max_weight: float | None = None) -> dict[str, float]:
    """等权：``1/n``，可选单票上限。"""
    unique = list(dict.fromkeys(symbols))
    if not unique:
        return {}
    w = 1.0 / len(unique)
    weights = {sym: w for sym in unique}
    return cap_weights(weights, max_weight) if max_weight is not None else weights


def score_weights(
    entries: Sequence[tuple[str, float]],
    *,
    max_weight: float | None = None,
) -> dict[str, float]:
    """按 score 正比例分配权重；score 全为非正时退化为等权。"""
    if not entries:
        return {}
    scores = {sym: max(float(score), 0.0) for sym, score in entries}
    total = sum(scores.values())
    if total <= 0:
        return equal_weights([sym for sym, _ in entries], max_weight=max_weight)
    weights = {sym: score / total for sym, score in scores.items()}
    return cap_weights(weights, max_weight) if max_weight is not None else weights
