"""文件型 provider（M4）：CSV / Parquet 目录，包装既有 `BarDataLoader`。

能力**由实际数据推断**（而不是硬编码），并在推断结果上应用可选的显式覆盖：

- 有 ``adj_factor`` 列 → ``adjustment_factors``；
- 有 ``is_suspended`` → ``suspensions``；有 ``limit_up``/``limit_down`` → ``price_limits``；
- 有 ``is_st`` → ``st_flags``；有 ``list_date`` → ``listing_dates``；有 ``delist_date`` → ``delistings``；
- 有 ``industry`` → ``industry``；有 ``total_mv`` → ``market_cap``；
- 指数成分 / 财务文件存在 → ``index_members`` / ``fundamentals``。

推断的意义：上层据此决定校验强度与降级路径。**缺什么就如实说缺什么**，
而不是乐观地宣称「都有」然后在下游悄悄失真。

Parquet 需要 pyarrow；缺失时抛 ``DataError`` 并给出安装提示（**不静默降级为读不到数据**）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from ..config.schema import DataConfig, as_config
from ..core.dates import DateLike
from ..core.exceptions import DataError
from .cache import parquet_available
from .loader import BarDataLoader, CsvBarLoader, ParquetBarLoader
from .provider import (
    ProviderCapabilities,
    ProviderHealth,
    Provenance,
    filter_effective_window,
    utc_now,
)
from .schema import BARS_COLUMNS

__all__ = ["FileProvider", "CsvProvider", "ParquetProvider"]

#: 列 → 能力 的推断表
_COLUMN_CAPABILITIES: dict[str, str] = {
    "adj_factor": "adjustment_factors",
    "is_suspended": "suspensions",
    "limit_up": "price_limits",
    "is_st": "st_flags",
    "list_date": "listing_dates",
    "delist_date": "delistings",
    "industry": "industry",
    "total_mv": "market_cap",
}

#: 能力探测时最多抽样多少行（避免为读能力把整库读进内存）
_PROBE_ROWS = 500


class FileProvider:
    """CSV / Parquet 目录数据源。"""

    name = "file"

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        index_members_path: str | Path | None = None,
        fundamentals_path: str | Path | None = None,
        config: DataConfig | Mapping[str, Any] | None = None,
        loader: BarDataLoader | None = None,
        name: str | None = None,
        capabilities: ProviderCapabilities | None = None,
    ) -> None:
        self.config = as_config(config, DataConfig) if config is not None else DataConfig()
        self.root = Path(root if root is not None else self.config.root)
        self.index_members_path = Path(index_members_path) if index_members_path else None
        self.fundamentals_path = Path(fundamentals_path) if fundamentals_path else None
        if loader is None:
            loader = self._default_loader()
        self.loader = loader
        if name:
            self.name = name
        self._explicit_caps = capabilities
        self._caps: ProviderCapabilities | None = capabilities
        self._industry_cache: pd.DataFrame | None = None

    # ------------------------------------------------------------------ #
    def _default_loader(self) -> BarDataLoader:
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    def capabilities(self) -> ProviderCapabilities:
        """能力声明：优先用显式传入的，否则从数据抽样推断（并缓存）。"""
        if self._caps is None:
            self._caps = self._infer_capabilities()
        return self._caps

    def _infer_capabilities(self) -> ProviderCapabilities:
        notes: list[str] = []
        flags: dict[str, bool] = {v: False for v in _COLUMN_CAPABILITIES.values()}
        flags["daily_bars"] = False
        flags["index_members"] = self.index_members_path is not None and self.index_members_path.exists()
        flags["fundamentals"] = self.fundamentals_path is not None and self.fundamentals_path.exists()

        try:
            sample = self._probe_bars()
        except Exception as exc:  # noqa: BLE001 - 探测失败要在 capabilities 上体现，而不是抛出
            notes.append(f"行情抽样失败，能力按最低估计：{exc}")
            sample = None

        if sample is not None and len(sample.columns):
            flags["daily_bars"] = True
            for column, capability in _COLUMN_CAPABILITIES.items():
                if column in sample.columns:
                    flags[capability] = True
            notes.append(f"能力由行情列推断（抽样 {len(sample)} 行）")

        if not flags["listing_dates"]:
            notes.append("缺少 list_date 列：上市天数过滤将按 data.listing_date.policy 处理")
        if not flags["index_members"]:
            notes.append("未提供指数成分文件：universe.mode=index 会退化为全市场")
        if not flags["fundamentals"]:
            notes.append("未提供财务文件：基本面因子不可用")

        return ProviderCapabilities(
            daily_bars=flags["daily_bars"],
            adjustment_factors=flags["adjustment_factors"],
            suspensions=flags["suspensions"],
            price_limits=flags["price_limits"],
            st_flags=flags["st_flags"],
            listing_dates=flags["listing_dates"],
            delistings=flags["delistings"],
            index_members=flags["index_members"],
            fundamentals=flags["fundamentals"],
            industry=flags["industry"],
            market_cap=flags["market_cap"],
            intraday=False,
            default_adjust=self.config.adjustment,
            notes=tuple(notes),
        )

    def _probe_bars(self) -> pd.DataFrame:
        """抽样读取行情以推断能力（CSV 限行数；Parquet 读列名）。"""
        root = self.root
        if root.is_file():
            files = [root]
        elif root.exists():
            suffix = ".csv" if self.name == "csv" else ".parquet"
            files = sorted(p for p in root.glob(f"*{suffix}") if p.is_file())
        else:
            raise DataError(f"数据目录不存在：{root}")
        if not files:
            raise DataError(f"{root} 下没有可读数据文件")

        path = files[0]
        if path.suffix.lower() in (".csv", ".txt"):
            return pd.read_csv(path, nrows=_PROBE_ROWS, dtype={"symbol": str, "index_code": str})
        if not parquet_available():
            raise DataError(
                "读取 Parquet 需要 pyarrow：pip install pyarrow（或用 provider=csv）"
            )
        return pd.read_parquet(path)

    def health_check(self) -> ProviderHealth:
        started = utc_now()
        errors: list[str] = []
        details: dict[str, Any] = {"root": str(self.root), "name": self.name}
        try:
            sample = self._probe_bars()
            details["probe_rows"] = int(len(sample))
            details["probe_columns"] = sorted(map(str, sample.columns))[:20]
            ok = len(sample) > 0
            if not ok:
                errors.append("抽样行情为空")
        except Exception as exc:  # noqa: BLE001
            ok = False
            errors.append(str(exc))
        latency = (utc_now() - started).total_seconds() * 1000.0
        return ProviderHealth(
            ok=ok, checked_at=utc_now(), latency_ms=latency,
            details=details, errors=tuple(errors),
        )

    # ------------------------------------------------------------------ #
    def _prov(self, frame: pd.DataFrame, warnings: tuple[str, ...] = ()) -> Provenance:
        symbols = int(frame["symbol"].nunique()) if len(frame) and "symbol" in frame.columns else 0
        return Provenance(
            source=self.name, fetched_at=utc_now(), cache_hit=False,
            rows=int(len(frame)), symbols=symbols, warnings=warnings,
        )

    @staticmethod
    def _empty(columns: Sequence[str]) -> pd.DataFrame:
        return pd.DataFrame(columns=list(columns))

    def _source_exists(self) -> bool:
        """数据源本身是否存在（用于区分「请求没匹配到」与「源不可用」）。"""
        root = self.root
        if root.is_file():
            return True
        if not root.exists():
            return False
        return any(p.is_file() for p in root.iterdir())

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
        _ = adjust
        try:
            frame = self.loader.load_bars(symbols=symbols, start=start, end=end)
        except DataError as exc:
            # 两种情况必须分开（多标的取数约定第 1 条）：
            # - 明确请求了标的、但本地都没有行情 → **空表 + warning**，不抛异常；
            # - 数据源本身不可用（目录/文件缺失）→ 仍然抛：那是配置问题，不该被静默降级。
            if not symbols or not self._source_exists():
                raise
            empty = self._empty(BARS_COLUMNS)
            return empty, self._prov(empty, (f"请求的标的在本地数据中没有行情：{exc}",))
        warnings: tuple[str, ...] = ()
        wanted = {str(s).upper() for s in symbols} if symbols else set()
        if wanted:
            missing = wanted - set(frame["symbol"].astype(str).str.upper())
            if missing:
                warnings = (f"{len(missing)} 个请求标的无行情：{sorted(missing)[:5]}",)
        return frame.copy(), self._prov(frame, warnings)

    def fetch_symbol_meta(self, symbols: Sequence[str]) -> tuple[pd.DataFrame, Provenance]:
        try:
            frame = self.loader.load_bars(symbols=symbols)
        except DataError as exc:
            empty = self._empty(("symbol", "list_date", "delist_date"))
            return empty, self._prov(empty, (str(exc),))
        columns = [
            c for c in ("symbol", "name", "board", "list_date", "delist_date", "industry", "total_mv")
            if c in frame.columns
        ]
        if "symbol" not in columns:
            empty = self._empty(("symbol", "list_date", "delist_date"))
            return empty, self._prov(empty, ("行情表缺少 symbol 列，无法构造元信息",))
        meta = frame[columns].drop_duplicates(subset=["symbol"]).reset_index(drop=True).copy()
        for col in ("list_date", "delist_date"):
            if col in meta.columns:
                meta[col] = pd.to_datetime(meta[col], errors="coerce")
        return meta, self._prov(meta)

    def fetch_index_members(
        self, index_code: str, start: DateLike, end: DateLike
    ) -> tuple[pd.DataFrame, Provenance]:
        if self.index_members_path is None or not self.index_members_path.exists():
            empty = self._empty(("index_code", "symbol", "effective_from", "effective_to"))
            return empty, self._prov(empty, ("未配置/不存在指数成分文件",))
        frame = self.loader.load_index_members(index_code)
        if frame is None or len(frame) == 0:
            empty = self._empty(("index_code", "symbol", "effective_from", "effective_to"))
            return empty, self._prov(empty, (f"指数 {index_code} 无成分记录",))
        # 区间相交语义：窗口开始前就生效的成分**必须保留**，否则股票池会退化为全市场
        frame = filter_effective_window(frame, start, end)
        warnings: tuple[str, ...] = ()
        if len(frame) == 0:
            # 空结果必须**显式披露**（与 synthetic/akshare 一致）：否则调用方只看到空表，
            # 无法区分「该区间确实没有成分」与「读文件失败」
            warnings = (f"指数 {index_code} 在 [{start}, {end}] 无生效的成分记录",)
        return frame.copy(), self._prov(frame, warnings)

    def fetch_fundamentals(
        self, symbols: Sequence[str], start: DateLike, end: DateLike
    ) -> tuple[pd.DataFrame, Provenance]:
        if self.fundamentals_path is None or not self.fundamentals_path.exists():
            empty = self._empty(("symbol", "report_period", "announce_date"))
            return empty, self._prov(empty, ("未配置/不存在财务文件",))
        frame = self.loader.load_fundamentals(symbols=symbols)
        if frame is None or len(frame) == 0:
            empty = self._empty(("symbol", "report_period", "announce_date"))
            return empty, self._prov(empty, ("财务文件为空",))
        column = "announce_date" if "announce_date" in frame.columns else "report_period"
        ts = pd.to_datetime(frame[column], errors="coerce")
        mask = pd.Series(True, index=frame.index)
        if start is not None:
            mask &= ts >= pd.Timestamp(start)
        if end is not None:
            mask &= ts <= pd.Timestamp(end)
        frame = frame.loc[mask].reset_index(drop=True)
        return frame.copy(), self._prov(frame)

    def fetch_trading_calendar(
        self, start: DateLike, end: DateLike
    ) -> tuple[list[Any], Provenance]:
        frame = self.loader.load_bars(start=start, end=end)
        days = sorted({pd.Timestamp(d).date() for d in pd.to_datetime(frame["date"], errors="coerce").dropna()})
        # `Provenance.rows` 必须描述**返回的数据**（交易日历是日期列表，不是行情行数）
        prov = Provenance(
            source=self.name,
            fetched_at=utc_now(),
            cache_hit=False,
            rows=len(days),
            symbols=0,
        )
        return days, prov

    def fetch_industry(self, symbols: Sequence[str]) -> tuple[pd.DataFrame, Provenance]:
        if self._industry_cache is None:
            try:
                frame = self.loader.load_bars(symbols=symbols)
            except DataError as exc:
                empty = self._empty(("symbol", "industry"))
                return empty, self._prov(empty, (str(exc),))
            if "industry" not in frame.columns:
                empty = self._empty(("symbol", "industry"))
                return empty, self._prov(empty, ("行情表缺少 industry 列",))
            self._industry_cache = (
                frame[["symbol", "industry"]].drop_duplicates(subset=["symbol"]).reset_index(drop=True)
            )
        frame = self._industry_cache.copy()
        wanted = {str(s).upper() for s in symbols} if symbols else None
        if wanted is not None:
            frame = frame[frame["symbol"].astype(str).str.upper().isin(wanted)].reset_index(drop=True)
        return frame, self._prov(frame)


class CsvProvider(FileProvider):
    """CSV 目录数据源。"""

    name = "csv"

    def _default_loader(self) -> BarDataLoader:
        return CsvBarLoader(
            self.root,
            index_members_path=self.index_members_path,
            fundamentals_path=self.fundamentals_path,
        )


class ParquetProvider(FileProvider):
    """Parquet 目录数据源（需 pyarrow）。"""

    name = "parquet"

    def _default_loader(self) -> BarDataLoader:
        if not parquet_available():
            raise DataError("provider=parquet 需要 pyarrow：pip install pyarrow（或改用 provider=csv）")
        return ParquetBarLoader(
            self.root,
            index_members_path=self.index_members_path,
            fundamentals_path=self.fundamentals_path,
        )
