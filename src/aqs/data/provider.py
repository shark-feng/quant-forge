"""数据源抽象：能力声明、溯源、健康检查与统一取数协议（M4）。

设计目标：把「数据从哪来」与「数据怎么用」解耦。上层（`loader.ingest_from_provider`、
`DataStore`）只面对 canonical schema，不关心背后是合成数据、CSV、Parquet 还是 AKShare。

三条不可让步的约束：

1. **能力必须显式声明**（:class:`ProviderCapabilities`）——
   数据源缺什么，上层就要据此决定「报错」还是「降级并披露」，
   **绝不允许静默兜底**（与缺陷 #8 的上市日处理同一原则）；
2. **每次取数都要有溯源**（:class:`Provenance`）—— 报告里的数字要能追到「哪个源、何时抓的、
   是否命中缓存、参数哈希是什么」；
3. **缺失能力要能翻译成人话**（:func:`degradation_notes`）—— 供 `diagnostics` 直接披露。

本模块**只依赖标准库与 pandas 类型标注**，不 import 任何 provider 实现（避免循环依赖）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, Sequence, runtime_checkable

import pandas as pd

from ..core.dates import DateLike

__all__ = [
    "ProviderCapabilities",
    "Provenance",
    "ProviderHealth",
    "DataProvider",
    "BACKTEST_REQUIRED",
    "degradation_notes",
    "filter_effective_window",
]

#: 任何市场数据源都必须具备的能力（缺一不可，否则无法回测）
BACKTEST_REQUIRED: tuple[str, ...] = ("daily_bars", "listing_dates")

#: 条件必需能力：只有在上层确实需要时才要求（缺则必须降级并披露）
CONDITIONAL_REQUIRED: dict[str, str] = {
    "index_members": "universe.mode=index 需要历史指数成分；缺失时股票池会退化为全市场（幸存者偏差风险）",
    "fundamentals": "启用财务因子/基本面过滤时需要；缺失时财务数据整体不可用",
    "adjustment_factors": "需要后复权口径时必需；缺失时只能用原始价（收益口径会失真）",
    "price_limits": "需要精确判定涨跌停时必需；缺失时按板块规则推算",
    "suspensions": "需要精确剔除停牌时必需；缺失时只能靠 volume==0 近似",
    "st_flags": "需要剔除 ST 时必需；缺失时只能按名称快照近似（ST 历史会失真）",
    "delistings": "需要规避幸存者偏差时必需；缺失时退市标的可能整体缺席",
}


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """数据源能力声明：上层据此决定校验强度与降级路径。

    ``index_members`` 特指**历史**成分（能回答「某日成分是谁」）。
    若数据源只能给出**当前**成分快照，该字段必须为 ``False`` ——
    因为把当前成分回溯到历史正是幸存者偏差的来源。
    """

    daily_bars: bool = False
    adjustment_factors: bool = False
    suspensions: bool = False
    price_limits: bool = False
    st_flags: bool = False
    listing_dates: bool = False
    delistings: bool = False
    index_members: bool = False
    fundamentals: bool = False
    industry: bool = False
    market_cap: bool = False
    intraday: bool = False
    default_adjust: str = "none"
    notes: tuple[str, ...] = ()

    # ------------------------------------------------------------------ #
    @classmethod
    def full(cls, *, default_adjust: str = "hfq", notes: tuple[str, ...] = ()) -> "ProviderCapabilities":
        """声明「全部能力可用」（合成数据源与完备的本地快照用）。"""
        return cls(
            daily_bars=True,
            adjustment_factors=True,
            suspensions=True,
            price_limits=True,
            st_flags=True,
            listing_dates=True,
            delistings=True,
            index_members=True,
            fundamentals=True,
            industry=True,
            market_cap=True,
            # intraday 保持 False：日频引擎不消费日内数据，声明为 True 是虚假能力
            intraday=False,
            default_adjust=default_adjust,
            notes=notes,
        )

    def missing_required(
        self,
        *,
        need_index_members: bool = False,
        need_fundamentals: bool = False,
        need_adjustment_factors: bool = False,
        need_price_limits: bool = False,
    ) -> tuple[str, ...]:
        """返回**必需但缺失**的能力名（按固定顺序，便于断言与展示）。

        ``daily_bars`` / ``listing_dates`` 始终必需；其余按上层实际需求判定。
        """
        needed: list[str] = list(BACKTEST_REQUIRED)
        if need_index_members:
            needed.append("index_members")
        if need_fundamentals:
            needed.append("fundamentals")
        if need_adjustment_factors:
            needed.append("adjustment_factors")
        if need_price_limits:
            needed.append("price_limits")
        return tuple(name for name in needed if not getattr(self, name))

    def as_dict(self) -> dict[str, Any]:
        return {
            "daily_bars": self.daily_bars,
            "adjustment_factors": self.adjustment_factors,
            "suspensions": self.suspensions,
            "price_limits": self.price_limits,
            "st_flags": self.st_flags,
            "listing_dates": self.listing_dates,
            "delistings": self.delistings,
            "index_members": self.index_members,
            "fundamentals": self.fundamentals,
            "industry": self.industry,
            "market_cap": self.market_cap,
            "intraday": self.intraday,
            "default_adjust": self.default_adjust,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class Provenance:
    """一次取数的溯源记录（写入 `summary.json` / 数据清单）。"""

    source: str
    fetched_at: datetime
    cache_hit: bool
    rows: int
    symbols: int
    params_hash: str = ""
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "fetched_at": self.fetched_at.isoformat(),
            "cache_hit": self.cache_hit,
            "rows": self.rows,
            "symbols": self.symbols,
            "params_hash": self.params_hash,
            "warnings": list(self.warnings),
        }


@dataclass(slots=True)
class ProviderHealth:
    """健康检查结果。

    ``ok=False`` 不等于「不可用」：可能是可选接口不通而核心接口正常，
    细节放在 ``details`` 与 ``errors`` 里，由上层决定是否调用
    ``data.failure_policy``（fallback / fail）。
    """

    ok: bool
    checked_at: datetime
    latency_ms: float
    details: dict[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at.isoformat(),
            "latency_ms": round(float(self.latency_ms), 3),
            "details": dict(self.details),
            "errors": list(self.errors),
        }


@runtime_checkable
class DataProvider(Protocol):
    """统一取数协议。四个实现（synthetic / csv / parquet / akshare）必须全部满足。

    返回约定（契约测试逐条断言，见 `tests/contracts/provider_contract.py`）：

    - 所有 ``fetch_*`` 返回 ``(DataFrame, Provenance)``，**列名是 canonical 名**；
    - 能力缺失时返回**空表 + 说明性 warning**，而**不是**抛异常
      （是否致命由上层的 ``capabilities().missing_required()`` 决定）；
    - ``fetch_trading_calendar`` 返回升序去重的 ``list[date]``；
    - ``volume`` 单位统一为**股**、``amount`` 统一为**元**（换算由 provider 按
      ``data.unit_conversion`` 完成，见 Q3）。
    """

    name: str

    def capabilities(self) -> ProviderCapabilities: ...

    def health_check(self) -> ProviderHealth: ...

    def fetch_bars(
        self,
        symbols: Sequence[str],
        start: DateLike,
        end: DateLike,
        *,
        adjust: str = "none",
    ) -> tuple[pd.DataFrame, Provenance]: ...

    def fetch_symbol_meta(self, symbols: Sequence[str]) -> tuple[pd.DataFrame, Provenance]: ...

    def fetch_index_members(
        self, index_code: str, start: DateLike, end: DateLike
    ) -> tuple[pd.DataFrame, Provenance]: ...

    def fetch_fundamentals(
        self, symbols: Sequence[str], start: DateLike, end: DateLike
    ) -> tuple[pd.DataFrame, Provenance]: ...

    def fetch_trading_calendar(
        self, start: DateLike, end: DateLike
    ) -> tuple[list[Any], Provenance]: ...

    def fetch_industry(self, symbols: Sequence[str]) -> tuple[pd.DataFrame, Provenance]: ...


def filter_effective_window(
    frame: pd.DataFrame,
    start: DateLike | None,
    end: DateLike | None,
    *,
    from_col: str = "effective_from",
    to_col: str = "effective_to",
) -> pd.DataFrame:
    """按「生效区间与 ``[start, end]`` **有交集**」筛选（指数成分等区间型数据）。

    这**不是** ``from_col >= start`` —— 那样会把所有「窗口开始前就已生效」的记录全部丢掉，
    导致指数成分为空 → 股票池退化为全市场 → **幸存者偏差**。
    正确语义是区间相交：

    ``effective_from <= end`` 且（``effective_to`` 为空 或 ``effective_to >= start``）。

    ``effective_to`` 为空表示「仍在生效」。
    """
    if frame is None or len(frame) == 0:
        return frame if frame is not None else pd.DataFrame()

    mask = pd.Series(True, index=frame.index)
    if from_col in frame.columns and end is not None:
        ef = pd.to_datetime(frame[from_col], errors="coerce")
        mask &= ef.notna() & (ef <= pd.Timestamp(end))
    if to_col in frame.columns and start is not None:
        et = pd.to_datetime(frame[to_col], errors="coerce")
        mask &= et.isna() | (et >= pd.Timestamp(start))
    return frame.loc[mask].reset_index(drop=True)


def degradation_notes(
    caps: ProviderCapabilities,
    *,
    need_index_members: bool = False,
    need_fundamentals: bool = False,
    need_adjustment_factors: bool = False,
    need_price_limits: bool = False,
) -> tuple[str, ...]:
    """把「缺失的能力」翻译成可直接写进 `diagnostics` 的披露文本。

    原则同缺陷 #8：**降级必须显式**。返回空元组表示没有任何降级。
    """
    needs = {
        "index_members": need_index_members,
        "fundamentals": need_fundamentals,
        "adjustment_factors": need_adjustment_factors,
        "price_limits": need_price_limits,
    }
    notes: list[str] = []
    for name, needed in needs.items():
        if needed and not getattr(caps, name):
            notes.append(f"数据源缺少能力 {name}：{CONDITIONAL_REQUIRED[name]}")
    for name in BACKTEST_REQUIRED:
        if not getattr(caps, name):
            notes.append(f"数据源缺少**必需**能力 {name}，无法完整回测")
    # 退市信息缺失本身就是一个独立风险点（与是否使用指数股票池无关）：
    # 没有退市名单，已退市标的可能整体缺席 → 幸存者偏差。
    if not getattr(caps, "delistings"):
        notes.append("数据源缺少退市信息：已退市标的可能整体缺席，存在幸存者偏差风险")
    return tuple(notes)
