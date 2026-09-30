"""数据存储：时间点正确（PIT）的历史数据访问。

**本模块是整个系统防未来函数的第一道防线。**

三重保护：
1. 每个查询方法都接受 ``as_of``；若请求区间越过 ``as_of``，抛 :class:`LookaheadError`；
2. :meth:`DataStore.as_of` 返回 :class:`PITView`，把 ``as_of`` 固化为视图上界，
   策略层只拿到视图，物理上无法取到未来数据；
3. :attr:`PITView.violations` 记录所有违规尝试，供测试与审计使用。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from ..config.schema import DataConfig, PriceLimitConfig, UniverseConfig, as_config
from ..core.calendar import TradingCalendar
from ..core.dates import DateLike, to_date, to_timestamp
from ..core.enums import Board
from ..core.exceptions import DataError, DataQualityError, LookaheadError
from ..core.logging import get_logger
from ..core.models import Bar, SymbolMeta
from .schema import (
    DataQualityReport,
    normalize_bars,
    normalize_fundamentals,
    normalize_index_members,
    raise_if_bad,
    select_fundamentals_asof,
    select_index_members,
    validate_bars,
)

__all__ = ["DataStore", "PITView"]

logger = get_logger("data.store")

_BAR_FIELDS = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adj_factor",
    "prev_close",
    "limit_up",
    "limit_down",
    "is_suspended",
    "is_st",
    "delist_date",
    "board",
)

_BAR_CACHE_SIZE = 32


@dataclass(slots=True)
class _Slice:
    start: int
    stop: int


class DataStore:
    """A 股日频数据仓库。

    Args:
        bars: 已归一（或可被归一）的行情表。
        calendar: 交易日历；缺省由行情日期派生。
        index_members: 指数成分变更表（历史成分）。
        fundamentals: 财务数据（必须含公告日）。
        config: 数据层配置（股票池过滤阈值等）。
        validate: 是否执行质量校验。
    """

    def __init__(
        self,
        bars: pd.DataFrame,
        calendar: TradingCalendar | None = None,
        index_members: pd.DataFrame | None = None,
        fundamentals: pd.DataFrame | None = None,
        *,
        config: DataConfig | Mapping[str, Any] | None = None,
        universe_config: UniverseConfig | Mapping[str, Any] | None = None,
        validate: bool = True,
        name: str = "store",
    ) -> None:
        self.name = name
        self.config = as_config(config, DataConfig)
        if "close_adj" not in bars.columns or "board" not in bars.columns:
            bars = normalize_bars(bars, price_limit=PriceLimitConfig())
        self._df: pd.DataFrame = bars.sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)

        self.quality: DataQualityReport | None = None
        if validate:
            self.quality = validate_bars(self._df, check_price_limits=self.config.quality.check_price_limits)
            if self.config.quality.strict:
                raise_if_bad(self.quality)
            elif not self.quality.ok:
                logger.warning("数据存在错误但 strict=False，继续运行：\n%s", self.quality.summary())

        # ---- 上市日簿记（缺陷修复 #8：必须在上市日策略检查之前初始化） ----
        self._listed_days: dict[str, dict[_date, int]] = {}
        self._listed_days_source: dict[str, str] = {}
        self._proxy_symbols: set[str] = set()
        self._window_symbols: set[str] = set()
        self._proxy_warned: set[str] = set()

        # ---- 上市日策略（缺陷修复 #8：不允许静默用 bar_seq 兜底） ----
        missing_list_date = self._missing_list_date_symbols()
        if self.config.listing_date.policy == "strict":
            if missing_list_date:
                preview = missing_list_date[:10]
                raise DataQualityError(
                    f"{len(missing_list_date)} 个标的缺少 list_date（示例：{preview}）；"
                    "上市交易日数是股票池「上市不足 N 日」过滤的依据，缺失会导致过滤失效。"
                    "请在数据源中提供真实上市日，或设置 data.listing_date.policy=proxy 显式接受代理口径",
                    report=self.quality,
                )
        elif missing_list_date:
            # proxy：构造时即登记代理标的，使 describe()/诊断无需等到首次查询就能披露
            self._proxy_symbols.update(missing_list_date)
            for symbol in missing_list_date:
                self._listed_days_source[symbol] = "bar_seq_proxy"
            if self.quality is not None:
                self.quality.stats["listed_days_proxy_symbols"] = len(self._proxy_symbols)
                self.quality.stats["listed_days_proxy_sample"] = sorted(self._proxy_symbols)[:20]
                self.quality.add_warning(
                    f"{len(self._proxy_symbols)} 个标的缺少 list_date，"
                    "上市交易日数退化为 bar_seq 代理口径（listing_date.policy=proxy）"
                )

        # ---- 日历 ----
        self.calendar = calendar or TradingCalendar.from_bars(self._df, name=f"{name}_calendar")
        self.calendar.bind_tradable(self.is_tradable)
        self._calendar_index: dict[_date, int] = {d: i for i, d in enumerate(self.calendar.days)}

        # ---- 索引结构 ----
        date_values = self._df["date"].values.astype("datetime64[ns]")
        self._date_values = date_values
        uniq_dates, starts = np.unique(date_values, return_index=True)
        stops = np.append(starts[1:], len(date_values))
        self._slices: dict[_date, _Slice] = {
            pd.Timestamp(d).date(): _Slice(int(s), int(e)) for d, s, e in zip(uniq_dates, starts, stops)
        }
        self._per_symbol: dict[str, pd.DataFrame] = {
            str(sym): grp.set_index("date", drop=False) for sym, grp in self._df.groupby("symbol", sort=False, observed=True)
        }
        self._cache: OrderedDict[_date, dict[str, Bar]] = OrderedDict()
        self._rolling_cache: dict[tuple[str, str, int], pd.Series] = {}

        # ---- 元信息 ----
        self._meta = self._build_symbol_meta()

        # ---- 指数成分 / 财务 ----
        self._members = normalize_index_members(index_members) if index_members is not None and len(index_members) else None
        self._fundamentals = (
            normalize_fundamentals(fundamentals) if fundamentals is not None and len(fundamentals) else None
        )

        # ---- 股票池 ----
        from .universe import UniverseBuilder

        self.universe_config = as_config(universe_config, UniverseConfig) if universe_config is not None else UniverseConfig(mode="all")
        self.universe_builder = UniverseBuilder(self, self.config)

    # ------------------------------------------------------------------ #
    # 元信息
    # ------------------------------------------------------------------ #
    def _missing_list_date_symbols(self) -> list[str]:
        """缺少 ``list_date`` 的标的（按名称排序）。"""
        if "list_date" not in self._df.columns:
            return sorted(self._df["symbol"].astype(str).unique().tolist())
        frame = self._df.groupby("symbol", observed=True)["list_date"].first()
        return sorted(frame[frame.isna()].index.astype(str).tolist())

    def _build_symbol_meta(self) -> dict[str, SymbolMeta]:
        meta: dict[str, SymbolMeta] = {}
        industry = self._df["industry"] if "industry" in self._df.columns else None
        for sym, grp in self._df.groupby("symbol", sort=False, observed=True):
            row = grp.iloc[0]
            board_val = row.get("board", Board.MAIN.value)
            try:
                board = Board(str(board_val))
            except ValueError:
                board = Board.MAIN
            list_date = row.get("list_date")
            delist_date = row.get("delist_date")
            meta[str(sym)] = SymbolMeta(
                symbol=str(sym),
                name=str(row.get("name", "") or ""),
                board=board,
                list_date=None if pd.isna(list_date) else pd.Timestamp(list_date).date(),
                delist_date=None if pd.isna(delist_date) else pd.Timestamp(delist_date).date(),
                industry=None if industry is None or pd.isna(row.get("industry")) else str(row.get("industry")),
            )
        return meta

    def symbol_meta(self, symbol: str) -> SymbolMeta:
        try:
            return self._meta[symbol]
        except KeyError as exc:
            raise DataError(f"未知标的：{symbol}") from exc

    def symbols(self, *, include_delisted: bool = True) -> list[str]:
        if include_delisted:
            return sorted(self._per_symbol)
        last = self.calendar.last_day
        if last is None:
            return []
        return sorted(self._bars_on_date(last).keys())

    @property
    def bars_frame(self) -> pd.DataFrame:
        """返回底层行情表（只读用途；请勿修改）。"""
        return self._df

    def trading_days(self, start: DateLike | None = None, end: DateLike | None = None) -> list[_date]:
        return self.calendar.sessions(start, end)

    # ------------------------------------------------------------------ #
    # PIT 保护
    # ------------------------------------------------------------------ #
    @staticmethod
    def _guard(as_of: DateLike | None, requested: DateLike, what: str) -> None:
        if as_of is None:
            return
        if to_timestamp(requested) > to_timestamp(as_of):
            raise LookaheadError(
                f"{what} 请求了 {to_date(requested)} 的数据",
                as_of=to_date(as_of),
                requested=to_date(requested),
            )

    def as_of(self, ts: DateLike) -> "PITView":
        """返回固化了时间上界的只读视图（策略层唯一允许的数据入口）。"""
        return PITView(self, to_date(ts))

    # ------------------------------------------------------------------ #
    # 行情
    # ------------------------------------------------------------------ #
    def _bars_on_date(self, day: _date) -> dict[str, Bar]:
        cached = self._cache.get(day)
        if cached is not None:
            self._cache.move_to_end(day)
            return cached
        sl = self._slices.get(day)
        if sl is None:
            return {}
        sub = self._df.iloc[sl.start : sl.stop]
        out: dict[str, Bar] = {}
        listed = self._listed_days_for(sub["symbol"].tolist(), day)
        for row in sub.itertuples(index=False):
            out[row.symbol] = self._make_bar(row, day, listed_days=listed.get(row.symbol, 0))
        self._cache[day] = out
        if len(self._cache) > _BAR_CACHE_SIZE:
            self._cache.popitem(last=False)
        return out

    def _make_bar(self, row: Any, day: _date, *, listed_days: int = 0) -> Bar:
        delist = getattr(row, "delist_date", None)
        board_val = getattr(row, "board", Board.MAIN.value)
        try:
            board = Board(str(board_val))
        except ValueError:
            board = Board.MAIN
        return Bar(
            symbol=str(row.symbol),
            date=day,
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
            amount=float(row.amount),
            adj_factor=float(getattr(row, "adj_factor", 1.0)),
            prev_adj_factor=float(getattr(row, "prev_adj_factor", np.nan)),
            base_adj_factor=float(getattr(row, "base_adj_factor", 1.0) or 1.0),
            prev_close=float(getattr(row, "prev_close", np.nan)),
            limit_up=float(getattr(row, "limit_up", np.nan)),
            limit_down=float(getattr(row, "limit_down", np.nan)),
            is_suspended=bool(getattr(row, "is_suspended", False)),
            is_st=bool(getattr(row, "is_st", False)),
            listed_days=int(listed_days),
            delist_date=None if delist is None or pd.isna(delist) else pd.Timestamp(delist).date(),
            board=board,
        )

    def _listed_days_for(
        self,
        symbols: Sequence[str],
        day: _date,
        *,
        policy: str | None = None,
    ) -> dict[str, int]:
        """按标的惰性计算「上市以来第几个交易日」并缓存（缺陷修复 #8）。

        - 有 ``list_date`` → 按交易日历精确计算，来源标记为 ``list_date``；
        - 无 ``list_date`` 且 ``policy="strict"`` → 抛 :class:`DataQualityError`（不静默兜底）；
        - 无 ``list_date`` 且 ``policy="proxy"`` → 用 ``bar_seq`` 代理，来源标记为
          ``bar_seq_proxy``，并记入质量报告与诊断（**显式降级，而非静默**）。
        """
        pol = policy or self.config.listing_date.policy
        need = [s for s in dict.fromkeys(symbols) if s not in self._listed_days]
        for sym in need:
            grp = self._per_symbol.get(sym)
            if grp is None:
                continue
            mapping: dict[_date, int] = {}
            seq = grp["bar_seq"].to_numpy() if "bar_seq" in grp.columns else np.arange(1, len(grp) + 1)
            list_date = grp["list_date"].iloc[0] if "list_date" in grp.columns else None
            has_list_date = list_date is not None and not pd.isna(list_date)
            if not has_list_date and pol == "strict":
                raise DataQualityError(
                    f"标的 {sym} 缺少 list_date，无法计算上市交易日数；请在数据源提供真实上市日，"
                    "或设置 data.listing_date.policy=proxy 显式接受代理口径",
                    report=self.quality,
                )
            if not has_list_date:
                self._proxy_symbols.add(sym)
                self._listed_days_source[sym] = "bar_seq_proxy"
                if self.config.listing_date.proxy_warn_once and sym not in self._proxy_warned:
                    self._proxy_warned.add(sym)
                    logger.warning(
                        "标的 %s 缺少 list_date，上市交易日数退化为 bar_seq 代理口径"
                        "（listing_date.policy=proxy）",
                        sym,
                    )
            li: int | None = None
            if has_list_date:
                li = self._calendar_index.get(pd.Timestamp(list_date).date())
                if li is not None:
                    self._listed_days_source[sym] = "list_date"
                else:
                    # list_date 早于数据起点：日历内无法精确计数，按工作日近似并显式标记
                    self._listed_days_source[sym] = "list_date_window"
                    self._window_symbols.add(sym)
            for i, ts in enumerate(grp.index):
                d = pd.Timestamp(ts).date()
                di = self._calendar_index.get(d)
                if li is not None and di is not None:
                    mapping[d] = di - li + 1
                elif has_list_date:
                    # 工作日近似（不含节假日修正）：只会高估，不会把老股误判为新股
                    mapping[d] = int(
                        np.busday_count(
                            np.datetime64(pd.Timestamp(list_date).date()), np.datetime64(d)
                        )
                    ) + 1
                else:
                    mapping[d] = int(seq[i])
            self._listed_days[sym] = mapping
        if self._proxy_symbols:
            self.quality.stats["listed_days_proxy_symbols"] = len(self._proxy_symbols)
            self.quality.stats["listed_days_proxy_sample"] = sorted(self._proxy_symbols)[:20]
        if self._window_symbols:
            self.quality.stats["listed_days_window_symbols"] = len(self._window_symbols)
        return {s: self._listed_days.get(s, {}).get(day, 0) for s in dict.fromkeys(symbols)}

    def listed_days_source(self, symbol: str) -> str:
        """上市交易日数的来源（缺陷修复 #8）。

        - ``list_date``：list_date 落在数据日历内，**精确**按交易日计数；
        - ``list_date_window``：list_date 早于数据起点，按工作日**近似**（只高估不低估）；
        - ``bar_seq_proxy``：**缺 list_date**，用 K 线序号代理（仅 policy=proxy 时允许）；
        - ``unknown``：无法判定。
        """
        if symbol not in self._listed_days_source:
            first = self.calendar.first_day
            if first is not None:
                try:
                    self._listed_days_for([symbol], first)
                except DataQualityError:
                    return "unknown"
        return self._listed_days_source.get(symbol, "unknown")

    @property
    def proxy_listed_symbols(self) -> list[str]:
        """使用 ``bar_seq`` 代理上市日口径的标的（供诊断与报告披露）。"""
        return sorted(self._proxy_symbols)

    @property
    def window_listed_symbols(self) -> list[str]:
        """list_date 早于数据起点、按工作日近似的标的。"""
        return sorted(self._window_symbols)

    def listed_days(self, symbol: str, day: DateLike, *, policy: str | None = None) -> int:
        """标的截至某日的上市交易日数。"""
        d = to_date(day)
        return self._listed_days_for([symbol], d, policy=policy).get(symbol, 0)

    def bar(self, symbol: str, day: DateLike, *, as_of: DateLike | None = None) -> Bar | None:
        self._guard(as_of, day, f"bar({symbol})")
        d = to_date(day)
        bars = self._bars_on_date(d)
        return bars.get(symbol)

    def bars_on(
        self,
        day: DateLike,
        symbols: Sequence[str] | None = None,
        *,
        as_of: DateLike | None = None,
    ) -> dict[str, Bar]:
        """返回某交易日的全部（或指定）标的行情。"""
        self._guard(as_of, day, "bars_on")
        d = to_date(day)
        bars = self._bars_on_date(d)
        if symbols is None:
            return dict(bars)
        return {s: bars[s] for s in symbols if s in bars}

    def history(
        self,
        symbol: str,
        end: DateLike,
        window: int,
        *,
        fields: Sequence[str] | None = None,
        as_of: DateLike | None = None,
    ) -> pd.DataFrame:
        """取截至 ``end``（含）的最近 ``window`` 根 K 线。

        **保证**：返回值中最大日期 ≤ end（时间点正确）。
        """
        self._guard(as_of, end, f"history({symbol})")
        grp = self._per_symbol.get(symbol)
        if grp is None:
            return pd.DataFrame(columns=list(fields) if fields else None)
        ts = to_timestamp(end)
        sub = grp.loc[grp.index <= ts]
        if window > 0:
            sub = sub.tail(window)
        cols = list(fields) if fields else None
        out = sub.reset_index(drop=True)
        return out[cols] if cols else out

    def history_panel(
        self,
        symbols: Sequence[str],
        end: DateLike,
        window: int,
        field_name: str = "close_adj",
        *,
        as_of: DateLike | None = None,
    ) -> pd.DataFrame:
        """多标的字段面板（index=日期，columns=标的）。"""
        frames: dict[str, pd.Series] = {}
        for sym in symbols:
            hist = self.history(sym, end, window, fields=["date", field_name], as_of=as_of)
            if hist.empty:
                continue
            frames[sym] = pd.Series(hist[field_name].to_numpy(), index=pd.DatetimeIndex(hist["date"]))
        if not frames:
            return pd.DataFrame()
        return pd.DataFrame(frames).sort_index()

    def rolling_value(self, symbol: str, field_name: str, day: DateLike, window: int = 20) -> float | None:
        """取 ``day`` 当日可见的滚动均值（O(log n) 查找，PIT 安全）。"""
        series = self.rolling_field(symbol, field_name, window)
        if series.empty:
            return None
        ts = to_timestamp(day)
        idx = series.index.searchsorted(ts, side="right") - 1
        if idx < 0:
            return None
        val = series.iloc[int(idx)]
        return None if pd.isna(val) else float(val)

    def adv(
        self,
        symbol: str,
        end: DateLike,
        window: int = 20,
        *,
        as_of: DateLike | None = None,
        field_name: str = "amount",
    ) -> float:
        """日均成交额 / 日均成交量（默认成交额，单位元）。"""
        self._guard(as_of, end, f"adv({symbol})")
        value = self.rolling_value(symbol, field_name, end, window)
        return 0.0 if value is None else value

    def rolling_field(self, symbol: str, field_name: str, window: int = 20) -> pd.Series:
        """按标的缓存的滚动均值（**只使用历史数据**，可安全用于 PIT 决策）。

        用于 ADV（成交额/成交量）等流动性指标，避免逐日重复计算。
        """
        key = (symbol, field_name, window)
        cached = self._rolling_cache.get(key)
        if cached is not None:
            return cached
        grp = self._per_symbol.get(symbol)
        if grp is None or field_name not in grp.columns:
            series = pd.Series(dtype="float64")
        else:
            values = grp[field_name].astype("float64")
            series = values.rolling(window, min_periods=window).mean()
            series.index = grp.index
        self._rolling_cache[key] = series
        return series

    def adv_volume(self, symbol: str, end: DateLike, window: int = 20, *, as_of: DateLike | None = None) -> float:
        return self.adv(symbol, end, window, as_of=as_of, field_name="volume")

    def last_close(self, symbol: str, end: DateLike, *, as_of: DateLike | None = None) -> float:
        hist = self.history(symbol, end, 1, fields=["close"], as_of=as_of)
        return float(hist["close"].iloc[-1]) if len(hist) else float("nan")

    # ------------------------------------------------------------------ #
    # 可交易性
    # ------------------------------------------------------------------ #
    def is_tradable(self, symbol: str, day: DateLike) -> bool:
        """该标的在该日是否可成交（有行情、未停牌、未退市）。"""
        d = to_date(day)
        grp = self._per_symbol.get(symbol)
        if grp is None:
            return False
        try:
            row = grp.loc[pd.Timestamp(d)]
        except KeyError:
            return False
        if isinstance(row, pd.DataFrame):  # 理论上不会发生（主键唯一）
            row = row.iloc[0]
        if bool(row.get("is_suspended", False)):
            return False
        if float(row.get("volume", 0.0)) <= 0:
            return False
        delist = row.get("delist_date")
        if delist is not None and not pd.isna(delist) and d > pd.Timestamp(delist).date():
            return False
        return True

    def is_suspended(self, symbol: str, day: DateLike) -> bool:
        grp = self._per_symbol.get(symbol)
        if grp is None:
            return True
        try:
            row = grp.loc[pd.Timestamp(to_date(day))]
        except KeyError:
            return True
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        return bool(row.get("is_suspended", False)) or float(row.get("volume", 0.0)) <= 0

    def listing_status(self, symbol: str, day: DateLike) -> str:
        """返回 listed / suspended / delisted / unknown。"""
        d = to_date(day)
        grp = self._per_symbol.get(symbol)
        if grp is None:
            return "unknown"
        meta = self._meta.get(symbol)
        if meta is not None and meta.list_date is not None and d < meta.list_date:
            return "unknown"
        if meta is not None and meta.delist_date is not None and d > meta.delist_date:
            return "delisted"
        if self.is_suspended(symbol, d):
            return "suspended"
        return "listed"

    # ------------------------------------------------------------------ #
    # 指数成分与财务
    # ------------------------------------------------------------------ #
    def index_members(self, index_code: str, day: DateLike) -> list[str]:
        """返回某指数在某日**实际生效**的历史成分股。"""
        if self._members is None:
            return []
        return select_index_members(self._members, index_code, day)

    @property
    def has_index_history(self) -> bool:
        return self._members is not None and len(self._members) > 0

    def all_listed(self, day: DateLike) -> list[str]:
        """某日全部有行情的标的（**包含已退市股票**，用于规避幸存者偏差）。"""
        return sorted(self._bars_on_date(to_date(day)).keys())

    def fundamentals(
        self,
        as_of: DateLike,
        symbols: Sequence[str] | None = None,
        fields: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        """按**公告日**取 ``as_of`` 时点可见的最新财务数据。"""
        if self._fundamentals is None:
            return pd.DataFrame()
        return select_fundamentals_asof(self._fundamentals, as_of, symbols=symbols, fields=fields)

    # ------------------------------------------------------------------ #
    # 股票池
    # ------------------------------------------------------------------ #
    def universe(
        self,
        day: DateLike,
        *,
        config: UniverseConfig | None = None,
        index_code: str | None = None,
        as_of: DateLike | None = None,
    ) -> list[str]:
        """按配置构建当日股票池（动态调整：剔除 ST/停牌/上市不足/流动性不足）。"""
        if as_of is not None:
            self._guard(as_of, day, "universe")
        cfg = config if config is not None else self.universe_config
        if index_code is not None:
            cfg = UniverseConfig(
                mode=cfg.mode,
                index_code=index_code,
                history_file=cfg.history_file,
                fallback_to_all=cfg.fallback_to_all,
            )
        return self.universe_builder.build(day, config=cfg)

    @classmethod
    def from_config(
        cls,
        config: Any,
        bundle: Any = None,
        **kwargs: Any,
    ) -> "DataStore":
        """按 :class:`BaseConfig` 构造（``provider`` 决定数据来源）。"""
        from ..config.loader import load_base_config
        from ..config.schema import BaseConfig

        base: BaseConfig = config if isinstance(config, BaseConfig) else load_base_config(**config)
        if bundle is None:
            from .loader import load_market_data

            bundle = load_market_data(base.data)
        return cls(
            bundle.bars,
            index_members=bundle.index_members,
            fundamentals=bundle.fundamentals,
            config=base.data,
            universe_config=base.universe,
            name=base.project.name,
            **kwargs,
        )

    # ------------------------------------------------------------------ #
    # 诊断
    # ------------------------------------------------------------------ #
    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rows": int(len(self._df)),
            "symbols": len(self._per_symbol),
            "trading_days": len(self.calendar),
            "start": str(self.calendar.first_day),
            "end": str(self.calendar.last_day),
            "has_index_history": self.has_index_history,
            "has_fundamentals": self._fundamentals is not None,
            "listing_date_policy": self.config.listing_date.policy,
            "listed_days_proxy_symbols": len(self._proxy_symbols),
            "listed_days_window_symbols": len(self._window_symbols),
            "quality_errors": len(self.quality.errors) if self.quality else 0,
            "quality_warnings": len(self.quality.warnings) if self.quality else 0,
        }


# --------------------------------------------------------------------------- #
# PIT 视图
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class PITView:
    """把 ``as_of`` 固化的只读数据视图。

    策略层只能拿到本视图，任何越过 ``as_of`` 的请求都会抛 :class:`LookaheadError`
    并记录到 :attr:`violations`。
    """

    store: DataStore
    as_of: _date
    violations: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------ 内部 ------------------------------ #
    def _check(self, requested: DateLike, what: str) -> None:
        if to_timestamp(requested) > to_timestamp(self.as_of):
            self.violations.append({"what": what, "requested": str(to_date(requested)), "as_of": str(self.as_of)})
            raise LookaheadError(f"PITView({self.as_of}) 上的 {what}", as_of=self.as_of, requested=to_date(requested))

    # ------------------------------ 委托接口 ------------------------------ #
    @property
    def calendar(self) -> TradingCalendar:
        return self.store.calendar

    def trading_days(self, start: DateLike | None = None, end: DateLike | None = None) -> list[_date]:
        if end is not None:
            self._check(end, "trading_days")
        days = self.store.trading_days(start, end)
        return [d for d in days if d <= self.as_of]

    def bar(self, symbol: str, day: DateLike) -> Bar | None:
        self._check(day, f"bar({symbol})")
        return self.store.bar(symbol, day)

    def bars_on(self, day: DateLike, symbols: Sequence[str] | None = None) -> dict[str, Bar]:
        self._check(day, "bars_on")
        return self.store.bars_on(day, symbols)

    def history(
        self,
        symbol: str,
        end: DateLike,
        window: int,
        *,
        fields: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        self._check(end, f"history({symbol})")
        return self.store.history(symbol, end, window, fields=fields)

    def history_panel(
        self, symbols: Sequence[str], end: DateLike, window: int, field_name: str = "close_adj"
    ) -> pd.DataFrame:
        self._check(end, "history_panel")
        return self.store.history_panel(symbols, end, window, field_name)

    def adv(self, symbol: str, end: DateLike, window: int = 20, *, field_name: str = "amount") -> float:
        self._check(end, f"adv({symbol})")
        return self.store.adv(symbol, end, window, field_name=field_name)

    def adv_volume(self, symbol: str, end: DateLike, window: int = 20) -> float:
        self._check(end, f"adv_volume({symbol})")
        return self.store.adv_volume(symbol, end, window)

    def fundamentals(
        self, symbols: Sequence[str] | None = None, fields: Sequence[str] | None = None
    ) -> pd.DataFrame:
        """注意：本视图下财务数据永远以 ``as_of`` 为公告日上界。"""
        return self.store.fundamentals(self.as_of, symbols=symbols, fields=fields)

    def universe(self, day: DateLike | None = None, *, config: UniverseConfig | None = None) -> list[str]:
        d = self.as_of if day is None else day
        self._check(d, "universe")
        return self.store.universe(d, config=config)

    def all_listed(self, day: DateLike | None = None) -> list[str]:
        d = self.as_of if day is None else day
        self._check(d, "all_listed")
        return self.store.all_listed(d)

    def listed_days(self, symbol: str, day: DateLike) -> int:
        self._check(day, f"listed_days({symbol})")
        return self.store.listed_days(symbol, day)

    # ------------------------------ 只读透传 ------------------------------ #
    def symbol_meta(self, symbol: str) -> SymbolMeta:
        return self.store.symbol_meta(symbol)

    @property
    def symbols(self) -> list[str]:
        return self.store.symbols()

    def __repr__(self) -> str:  # pragma: no cover
        return f"PITView(as_of={self.as_of}, violations={len(self.violations)})"
