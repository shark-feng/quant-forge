"""合成数据 provider（M4）：把 `generate_market_data` 包装成统一取数协议。

用途：测试与演示的无网络、确定性数据源。它也是**唯一声明全部日频能力**的 provider
（含历史指数成分、公告日财务、退市名单），因此可作为「契约测试的基准实现」。

确定性：同一组 ``(n_symbols, seed, start, end, index_size)`` 必然生成同一份数据
（缺陷 #14 修复后已由 `tests/test_determinism.py` 跨进程守护）。
生成结果按 provider 实例缓存在内存中，重复 ``fetch_*`` 不会重新生成。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import pandas as pd

from ..config.schema import DataConfig, SyntheticDataConfig, as_config
from ..core.dates import DateLike
from .loader import MarketDataBundle
from .provider import (
    ProviderCapabilities,
    ProviderHealth,
    Provenance,
    filter_effective_window,
    utc_now,
)

__all__ = ["SyntheticProvider"]

#: 能力探测用：合成数据固定提供这些列
_META_COLUMNS = ("list_date", "delist_date", "industry", "board", "name", "total_mv")


@dataclass(slots=True)
class _Cached:
    bundle: MarketDataBundle | None = None


class SyntheticProvider:
    """合成行情数据源（确定性、零网络）。"""

    name = "synthetic"

    def __init__(
        self,
        config: DataConfig | Mapping[str, Any] | None = None,
        *,
        n_symbols: int | None = None,
        seed: int | None = None,
        index_size: int | None = None,
        start: DateLike | None = None,
        end: DateLike | None = None,
        bundle: MarketDataBundle | None = None,
    ) -> None:
        cfg = as_config(config, DataConfig) if config is not None else DataConfig()
        self.config = cfg
        syn: SyntheticDataConfig = cfg.synthetic
        self.n_symbols = int(n_symbols if n_symbols is not None else syn.n_symbols)
        self.seed = int(seed if seed is not None else syn.seed)
        self.index_size = int(index_size if index_size is not None else syn.index_size)
        self.index_code = syn.index_code
        self._start = str(pd.Timestamp(start).date()) if start is not None else None
        self._end = str(pd.Timestamp(end).date()) if end is not None else None
        self._cache = _Cached(bundle=bundle)

    # ------------------------------------------------------------------ #
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities.full(
            default_adjust="hfq",
            notes=(
                "合成数据：确定性生成，含停牌/涨跌停/ST/退市/复权因子/历史指数成分",
                "仅供测试与演示，不代表真实市场",
            ),
        )

    def health_check(self) -> ProviderHealth:
        started = utc_now()
        try:
            bundle = self._bundle()
            ok = len(bundle.bars) > 0
            details = {
                "rows": int(len(bundle.bars)),
                "symbols": int(bundle.bars["symbol"].nunique()) if len(bundle.bars) else 0,
                "deterministic": True,
                "seed": self.seed,
            }
            errors: tuple[str, ...] = () if ok else ("合成数据为空",)
        except Exception as exc:  # noqa: BLE001 - 健康检查不应抛出
            ok, details, errors = False, {}, (str(exc),)
        latency = (utc_now() - started).total_seconds() * 1000.0
        return ProviderHealth(ok=ok, checked_at=utc_now(), latency_ms=latency,
                              details=details, errors=errors)

    # ------------------------------------------------------------------ #
    def _bundle(self) -> MarketDataBundle:
        if self._cache.bundle is None:
            from .synthetic import generate_market_data

            self._cache.bundle = generate_market_data(
                n_symbols=self.n_symbols,
                seed=self.seed,
                start=self._start,
                end=self._end,
                index_size=self.index_size,
                index_code=self.index_code,
            )
        return self._cache.bundle

    def _provenance(self, frame: pd.DataFrame, *, source: str = "synthetic",
                    warnings: tuple[str, ...] = ()) -> Provenance:
        symbols = int(frame["symbol"].nunique()) if len(frame) and "symbol" in frame.columns else 0
        return Provenance(
            source=source,
            fetched_at=utc_now(),
            cache_hit=False,
            rows=int(len(frame)),
            symbols=symbols,
            warnings=warnings,
        )

    @staticmethod
    def _slice(frame: pd.DataFrame, start: DateLike | None, end: DateLike | None,
               column: str = "date") -> pd.DataFrame:
        if frame is None or len(frame) == 0 or column not in frame.columns:
            return frame if frame is not None else pd.DataFrame()
        ts = pd.to_datetime(frame[column], errors="coerce")
        mask = pd.Series(True, index=frame.index)
        if start is not None:
            mask &= ts >= pd.Timestamp(start)
        if end is not None:
            mask &= ts <= pd.Timestamp(end)
        return frame.loc[mask].reset_index(drop=True)

    # ------------------------------------------------------------------ #
    # 协议实现
    # ------------------------------------------------------------------ #
    def fetch_bars(
        self,
        symbols: Sequence[str],
        start: DateLike,
        end: DateLike,
        *,
        adjust: str = "none",
    ) -> tuple[pd.DataFrame, Provenance]:
        bundle = self._bundle()
        frame = self._slice(bundle.bars, start, end)
        wanted = {str(s).upper() for s in symbols} if symbols else None
        if wanted is not None:
            frame = frame[frame["symbol"].astype(str).str.upper().isin(wanted)]
        frame = frame.reset_index(drop=True)
        warnings: tuple[str, ...] = ()
        missing = (wanted - set(frame["symbol"].astype(str))) if wanted else set()
        if missing:
            warnings = (f"{len(missing)} 个请求标的在合成数据中不存在：{sorted(missing)[:5]}",)
        # adjust 只影响上层是否使用 *_adj 列；provide 原始价 + adj_factor（与真实源一致）
        _ = adjust
        return frame.copy(), self._provenance(frame, warnings=warnings)

    def fetch_symbol_meta(self, symbols: Sequence[str]) -> tuple[pd.DataFrame, Provenance]:
        bundle = self._bundle()
        bars = bundle.bars
        columns = [c for c in ("symbol", *_META_COLUMNS) if c in bars.columns]
        frame = bars[columns].drop_duplicates(subset=["symbol"]).reset_index(drop=True)
        wanted = {str(s).upper() for s in symbols} if symbols else None
        if wanted is not None:
            frame = frame[frame["symbol"].astype(str).str.upper().isin(wanted)].reset_index(drop=True)
        frame = frame.copy()
        for col in ("list_date", "delist_date"):
            if col in frame.columns:
                frame[col] = pd.to_datetime(frame[col], errors="coerce")
        return frame, self._provenance(frame)

    def fetch_index_members(
        self, index_code: str, start: DateLike, end: DateLike
    ) -> tuple[pd.DataFrame, Provenance]:
        bundle = self._bundle()
        members = bundle.index_members
        if members is None or len(members) == 0:
            empty = pd.DataFrame(columns=["index_code", "symbol", "effective_from", "effective_to"])
            return empty, self._provenance(empty, warnings=("合成数据未启用指数成分历史",))
        frame = members[members["index_code"].astype(str).str.upper() == str(index_code).upper()]
        # 区间相交语义（同 file_provider）：窗口开始前生效的成分必须保留
        frame = filter_effective_window(frame, start, end).copy()
        warnings: tuple[str, ...] = ()
        if len(frame) == 0:
            warnings = (f"指数 {index_code} 在 [{start}, {end}] 无成分记录",)
        return frame, self._provenance(frame, warnings=warnings)

    def fetch_fundamentals(
        self, symbols: Sequence[str], start: DateLike, end: DateLike
    ) -> tuple[pd.DataFrame, Provenance]:
        bundle = self._bundle()
        frame = bundle.fundamentals
        if frame is None or len(frame) == 0:
            empty = pd.DataFrame(columns=["symbol", "report_period", "announce_date"])
            return empty, self._provenance(empty, warnings=("合成数据未启用财务数据",))
        frame = self._slice(frame, start, end, column="announce_date")
        wanted = {str(s).upper() for s in symbols} if symbols else None
        if wanted is not None:
            frame = frame[frame["symbol"].astype(str).str.upper().isin(wanted)]
        frame = frame.reset_index(drop=True)
        return frame.copy(), self._provenance(frame)

    def fetch_trading_calendar(
        self, start: DateLike, end: DateLike
    ) -> tuple[list[Any], Provenance]:
        bundle = self._bundle()
        frame = self._slice(bundle.bars, start, end)
        days = sorted({pd.Timestamp(d).date() for d in pd.to_datetime(frame["date"])})
        prov = Provenance(
            source="synthetic", fetched_at=utc_now(), cache_hit=False,
            rows=len(days), symbols=1,
        )
        return days, prov

    def fetch_industry(self, symbols: Sequence[str]) -> tuple[pd.DataFrame, Provenance]:
        bundle = self._bundle()
        bars = bundle.bars
        if "industry" not in bars.columns:
            empty = pd.DataFrame(columns=["symbol", "industry"])
            return empty, self._provenance(empty, warnings=("合成数据无行业列",))
        frame = bars[["symbol", "industry"]].drop_duplicates(subset=["symbol"]).reset_index(drop=True)
        wanted = {str(s).upper() for s in symbols} if symbols else None
        if wanted is not None:
            frame = frame[frame["symbol"].astype(str).str.upper().isin(wanted)].reset_index(drop=True)
        return frame.copy(), self._provenance(frame)
