"""股票池动态调整。

规则（第一阶段）：
1. 剔除 ST / *ST；
2. 剔除停牌（含无成交）；
3. 剔除上市不足 ``min_list_days``（默认 60）个交易日；
4. 剔除流动性不足：近 ``window``（默认 20）日日均成交额 < ``min_amount``（默认 5000 万）；
5. 标的范围由 ``universe.mode`` 决定：
   - ``index``：使用**历史**指数成分（规避幸存者偏差）；
   - ``all``  ：全部有行情的标的（**包含已退市股票**）；
   - ``file`` ：外部名单。

所有窗口仅使用 ``day`` 当日及之前的数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from typing import TYPE_CHECKING, Any, Iterable, Sequence

import pandas as pd

from ..config.schema import DataConfig, UniverseConfig, as_config
from ..core.dates import DateLike, to_date
from ..core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from .store import DataStore

__all__ = ["UniverseBuilder", "UniverseStats"]

logger = get_logger("data.universe")


@dataclass(slots=True)
class UniverseStats:
    """股票池构建过程的分步统计（用于报告与调参）。"""

    date: _date
    mode: str = ""
    candidates: int = 0
    after_st: int = 0
    after_suspended: int = 0
    after_listing: int = 0
    after_liquidity: int = 0
    final: int = 0
    excluded: dict[str, list[str]] = field(default_factory=dict)

    @property
    def dropped(self) -> int:
        return self.candidates - self.final

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": str(self.date),
            "mode": self.mode,
            "candidates": self.candidates,
            "after_st": self.after_st,
            "after_suspended": self.after_suspended,
            "after_listing": self.after_listing,
            "after_liquidity": self.after_liquidity,
            "final": self.final,
            "dropped": self.dropped,
            "excluded_counts": {k: len(v) for k, v in self.excluded.items()},
        }


class UniverseBuilder:
    """按配置构建每日股票池（纯函数：同一日期 + 同一配置 → 同一结果）。"""

    def __init__(self, store: "DataStore", config: DataConfig | None = None) -> None:
        self.store = store
        self.config = as_config(config, DataConfig) if config is not None else store.config
        self._adv_cache: dict[tuple[str, int], pd.Series] = {}
        self._file_members_cached: pd.DataFrame | None = None
        self._stats: dict[_date, UniverseStats] = {}
        self._last_result: list[str] = []
        self._last_stats: UniverseStats | None = None

    # ------------------------------------------------------------------ #
    # 流动性指标（惰性滚动计算并缓存；只使用历史数据）
    # ------------------------------------------------------------------ #
    def _rolling_amount(self, symbol: str, window: int) -> pd.Series:
        """复用 :meth:`DataStore.rolling_field` 的滚动成交额（PIT 安全）。"""
        return self.store.rolling_field(symbol, "amount", window)

    def adv_on(self, symbol: str, day: DateLike, window: int) -> float | None:
        """``day`` 当日可见的近 ``window`` 日日均成交额；不足窗口期返回 ``None``。"""
        series = self._rolling_amount(symbol, window)
        if series.empty:
            return None
        ts = pd.Timestamp(to_date(day))
        sub = series.loc[series.index <= ts]
        if sub.empty:
            return None
        val = sub.iloc[-1]
        return None if pd.isna(val) else float(val)

    # ------------------------------------------------------------------ #
    # 候选集
    # ------------------------------------------------------------------ #
    def _file_members(self, config: UniverseConfig) -> pd.DataFrame:
        if self._file_members_cached is None:
            path = config.history_file
            if not path:
                logger.warning("universe.mode=file 但未配置 history_file，退化为全市场")
                self._file_members_cached = pd.DataFrame(columns=["symbol", "effective_from", "effective_to"])
            else:
                df = pd.read_csv(path, dtype={"symbol": str})
                from .schema import normalize_index_members

                df = df.assign(index_code=config.index_code) if "index_code" not in df.columns else df
                self._file_members_cached = normalize_index_members(df)
        return self._file_members_cached

    def candidates(self, day: _date, config: UniverseConfig) -> tuple[list[str], str]:
        store = self.store
        if config.mode == "index":
            members = store.index_members(config.index_code, day)
            if members:
                return members, "index"
            if not config.fallback_to_all:
                return [], "index(empty)"
            logger.warning(
                "%s 在 %s 无历史指数成分记录，按配置退化为全市场（可能引入幸存者偏差，请核查数据）",
                config.index_code,
                day,
            )
            return store.all_listed(day), "index->all"
        if config.mode == "file":
            from .schema import select_index_members

            df = self._file_members(config)
            members = select_index_members(df, config.index_code, day) if len(df) else []
            if members:
                return members, "file"
            if not config.fallback_to_all:
                return [], "file(empty)"
            return store.all_listed(day), "file->all"
        return store.all_listed(day), "all"

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def build(
        self,
        day: DateLike,
        *,
        config: UniverseConfig | None = None,
        index_code: str | None = None,
        explain: bool = False,
    ) -> list[str]:
        """返回当日通过全部过滤的股票池（已排序）。"""
        final, stats = self._filter(day, config=config, index_code=index_code)
        self._last_result = final
        self._last_stats = stats
        if explain:
            self._stats[to_date(day)] = stats
        return list(final)

    def build_stats(
        self,
        day: DateLike,
        *,
        config: UniverseConfig | None = None,
        index_code: str | None = None,
    ) -> UniverseStats:
        """返回构建过程的分步统计（同时更新 :attr:`last_result`）。"""
        final, stats = self._filter(day, config=config, index_code=index_code)
        self._last_result = final
        self._last_stats = stats
        self._stats[to_date(day)] = stats
        return stats

    # 语义化别名
    explain = build_stats

    def _filter(
        self,
        day: DateLike,
        *,
        config: UniverseConfig | None = None,
        index_code: str | None = None,
    ) -> tuple[list[str], UniverseStats]:
        cfg = config or UniverseConfig(mode="all")
        if index_code is not None:
            cfg = UniverseConfig(
                mode=cfg.mode,
                index_code=index_code,
                history_file=cfg.history_file,
                fallback_to_all=cfg.fallback_to_all,
            )

        d = to_date(day)
        data_cfg: DataConfig = self.config
        cand, mode_name = self.candidates(d, cfg)
        stats = UniverseStats(date=d, mode=mode_name, candidates=len(cand))
        excluded: dict[str, list[str]] = {"st": [], "suspended": [], "listing": [], "liquidity": []}

        survivors: list[str] = []
        for sym in cand:
            bar = self.store.bar(sym, d)
            if bar is None:
                excluded["suspended"].append(sym)
                continue
            if data_cfg.exclude_st and bar.is_st:
                excluded["st"].append(sym)
                continue
            if data_cfg.exclude_suspended and (bar.is_suspended or bar.volume <= 0):
                excluded["suspended"].append(sym)
                continue
            survivors.append(sym)
        stats.after_st = stats.candidates - len(excluded["st"])
        stats.after_suspended = stats.after_st - len(excluded["suspended"])

        after_listing: list[str] = []
        for sym in survivors:
            listed = self.store.listed_days(sym, d)
            if listed < data_cfg.min_list_days:
                excluded["listing"].append(sym)
                continue
            after_listing.append(sym)
        stats.after_listing = len(after_listing)

        window = data_cfg.liquidity.window
        min_amount = data_cfg.liquidity.min_amount
        final: list[str] = []
        if min_amount <= 0 or window <= 0:
            # 阈值关闭：不做流动性过滤（历史长度不足的标的不会被误杀）
            final = list(after_listing)
        else:
            for sym in after_listing:
                adv = self.adv_on(sym, d, window)
                if adv is None or adv < min_amount:
                    excluded["liquidity"].append(sym)
                    continue
                final.append(sym)

        stats.after_liquidity = len(final)
        stats.final = len(final)
        stats.excluded = excluded
        return sorted(final), stats

    # ------------------------------------------------------------------ #
    @property
    def last_stats(self) -> UniverseStats | None:
        """最近一次构建的统计（供报告层使用）。"""
        return self._last_stats

    @property
    def last_result(self) -> list[str]:
        """最近一次构建的股票池。"""
        return list(self._last_result)

    def stats_history(self) -> list[UniverseStats]:
        return [self._stats[d] for d in sorted(self._stats)]
