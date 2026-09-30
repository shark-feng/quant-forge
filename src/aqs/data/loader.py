"""行情装载：CSV / Parquet / 合成数据。

约定（``docs/01_data_layer.md`` §6）：loader 只做字段映射与类型归一，
**禁止**在 loader 内做复权、填充、过滤等“智能”操作 —— 那些属于数据层/store 的职责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

import pandas as pd

from ..config.schema import DataConfig
from ..core.exceptions import DataError, SchemaError
from ..core.logging import get_logger
from .schema import normalize_bars, normalize_fundamentals, normalize_index_members

__all__ = [
    "MarketDataBundle",
    "BarDataLoader",
    "CsvBarLoader",
    "ParquetBarLoader",
    "load_market_data",
    "read_table",
    "describe_data_scope",
]

logger = get_logger("data.loader")


@dataclass(slots=True)
class MarketDataBundle:
    """一组原始市场数据（已归一为 canonical schema）。"""

    bars: pd.DataFrame
    index_members: pd.DataFrame | None = None
    fundamentals: pd.DataFrame | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def symbols(self) -> list[str]:
        return sorted(self.bars["symbol"].unique().tolist())

    def date_range(self) -> tuple[Any, Any]:
        d = pd.to_datetime(self.bars["date"])
        return d.min(), d.max()


def describe_data_scope(
    bundle: MarketDataBundle,
    store: Any,
    *,
    sample_days: int = 5,
) -> dict[str, Any]:
    """说明「数据口径」的四个数量（缺陷修复 #12）。

    这四个数经常被混为一谈，导致「我明明生成了 30 只，怎么只有 12 只在跑」的困惑：

    - ``generated``：**生成/请求**的标的数（合成数据）；
    - ``with_bars``：**实际有行情**的标的数；
    - ``index_members``：**进入指数成分**的标的数（股票池候选上限）；
    - ``universe_avg``：**每日入池**的平均标的数（再经 ST/停牌/上市天数/流动性过滤）。
    """
    generated = int(bundle.meta.get("n_symbols", len(bundle.symbols())))
    with_bars = len(bundle.symbols())
    union: set[str] = set()
    index_code: str | None = None
    if bundle.index_members is not None and len(bundle.index_members):
        members_frame = bundle.index_members
        union = set(members_frame["symbol"].astype(str))
        if "index_code" in members_frame.columns:
            index_code = str(members_frame["index_code"].iloc[0])
    days = store.trading_days()
    step = max(len(days) // max(int(sample_days), 1), 1)
    sample = list(days[::step])[: max(int(sample_days), 1)]
    sizes = [len(store.universe(d)) for d in sample]
    member_counts = (
        [len(store.index_members(index_code, d)) for d in sample] if index_code is not None else []
    )
    return {
        "generated": generated,
        "with_bars": with_bars,
        "index_code": index_code,
        "index_members_union": len(union),
        "index_members_avg": (sum(member_counts) / len(member_counts)) if member_counts else 0.0,
        "index_members_sizes": member_counts,
        "universe_avg": (sum(sizes) / len(sizes)) if sizes else 0.0,
        "universe_min": min(sizes) if sizes else 0,
        "universe_max": max(sizes) if sizes else 0,
        "universe_sample_days": [str(d) for d in sample],
        "universe_sizes": sizes,
    }


def read_table(path: str | Path, *, columns: Sequence[str] | None = None) -> pd.DataFrame:
    """按扩展名读取 CSV / Parquet。"""
    p = Path(path)
    if not p.exists():
        raise DataError(f"数据文件不存在：{p}")
    suffix = p.suffix.lower()
    if suffix in (".parquet", ".pq"):
        try:
            return pd.read_parquet(p, columns=list(columns) if columns else None)
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise DataError("读取 Parquet 需要安装 pyarrow：pip install pyarrow") from exc
    if suffix in (".csv", ".txt"):
        return pd.read_csv(p, dtype={"symbol": str, "index_code": str})
    raise DataError(f"不支持的文件类型：{p.suffix}（支持 .csv/.parquet）")


@runtime_checkable
class BarDataLoader(Protocol):
    """数据源适配协议：接入真实数据源（Tushare/Wind/本地库）只需实现本协议。"""

    def load_bars(
        self, *, symbols: Sequence[str] | None = None, start: Any = None, end: Any = None
    ) -> pd.DataFrame: ...

    def load_index_members(self, index_code: str | None = None) -> pd.DataFrame: ...

    def load_fundamentals(self, *, symbols: Sequence[str] | None = None) -> pd.DataFrame: ...


@dataclass(slots=True)
class _FileLoaderBase:
    """文件型数据源基类：支持「单文件含 symbol 列」与「一标的一文件」两种布局。"""

    bars_path: str | Path
    index_members_path: str | Path | None = None
    fundamentals_path: str | Path | None = None
    glob: str = "*"
    symbol_from_filename: bool = True
    name: str = "file"

    # ------------------------------ 内部 ------------------------------ #
    def _iter_files(self) -> list[Path]:
        root = Path(self.bars_path)
        if root.is_file():
            return [root]
        if not root.exists():
            raise DataError(f"行情目录不存在：{root}")
        files = sorted(p for p in root.glob(self.glob) if p.is_file() and p.suffix.lower() in (".csv", ".parquet", ".txt", ".pq"))
        if not files:
            raise DataError(f"目录 {root} 下未找到匹配 {self.glob} 的数据文件")
        return files

    def _symbol_from_path(self, path: Path) -> str:
        return path.stem

    # ------------------------------ 协议实现 ------------------------------ #
    def load_bars(
        self, *, symbols: Sequence[str] | None = None, start: Any = None, end: Any = None
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        wanted = {str(s).upper() for s in symbols} if symbols else None
        for path in self._iter_files():
            df = read_table(path)
            if self.symbol_from_filename and "symbol" not in df.columns:
                df = df.assign(symbol=self._symbol_from_path(path))
            if "symbol" not in df.columns:
                raise SchemaError(f"数据文件缺少 symbol 列且无法从文件名推断：{path}")
            if wanted is not None:
                df = df[df["symbol"].astype(str).str.upper().isin(wanted)]
            if len(df):
                frames.append(df)
        if not frames:
            raise DataError("未读取到任何行情数据（请检查 symbols / 目录 / 文件内容）")
        bars = pd.concat(frames, ignore_index=True)
        if start is not None:
            bars = bars[pd.to_datetime(bars["date"]) >= pd.Timestamp(start)]
        if end is not None:
            bars = bars[pd.to_datetime(bars["date"]) <= pd.Timestamp(end)]
        logger.info("已载入 %s 条行情（%s 个标的）", len(bars), bars["symbol"].nunique())
        return bars

    def load_index_members(self, index_code: str | None = None) -> pd.DataFrame:
        if self.index_members_path is None:
            return pd.DataFrame()
        df = read_table(self.index_members_path)
        if index_code is not None and "index_code" in df.columns:
            df = df[df["index_code"].astype(str).str.upper() == str(index_code).upper()]
        return df

    def load_fundamentals(self, *, symbols: Sequence[str] | None = None) -> pd.DataFrame:
        if self.fundamentals_path is None:
            return pd.DataFrame()
        df = read_table(self.fundamentals_path)
        if symbols is not None and "symbol" in df.columns:
            wanted = {str(s).upper() for s in symbols}
            df = df[df["symbol"].astype(str).str.upper().isin(wanted)]
        return df


class CsvBarLoader(_FileLoaderBase):
    """CSV 数据源。"""

    def __init__(
        self,
        bars_path: str | Path,
        index_members_path: str | Path | None = None,
        fundamentals_path: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("glob", "*.csv")
        kwargs.setdefault("name", "csv")
        super().__init__(bars_path, index_members_path, fundamentals_path, **kwargs)


class ParquetBarLoader(_FileLoaderBase):
    """Parquet 数据源（需 pyarrow）。"""

    def __init__(
        self,
        bars_path: str | Path,
        index_members_path: str | Path | None = None,
        fundamentals_path: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("glob", "*.parquet")
        kwargs.setdefault("name", "parquet")
        super().__init__(bars_path, index_members_path, fundamentals_path, **kwargs)


def load_market_data(
    config: DataConfig,
    *,
    symbols: Sequence[str] | None = None,
    start: Any = None,
    end: Any = None,
    loader: BarDataLoader | None = None,
    **synthetic_kwargs: Any,
) -> MarketDataBundle:
    """按配置装载市场数据，返回归一化后的 :class:`MarketDataBundle`。

    ``provider: synthetic`` 用于测试与演示（确定性生成，可复现）。
    """
    if loader is None and config.provider == "synthetic":
        from .synthetic import generate_market_data

        return generate_market_data(start=start, end=end, symbols=symbols, **synthetic_kwargs)

    if loader is None:
        root = Path(config.root)
        indexes = root.parent / "index" / "index_members.csv"
        fundamentals = root.parent / "fundamental" / "fundamentals.csv"
        cls = CsvBarLoader if config.provider == "csv" else ParquetBarLoader
        loader = cls(
            root,
            index_members_path=indexes if indexes.exists() else None,
            fundamentals_path=fundamentals if fundamentals.exists() else None,
        )

    raw_bars = loader.load_bars(symbols=symbols, start=start, end=end)
    bars = normalize_bars(raw_bars)

    members = loader.load_index_members() if hasattr(loader, "load_index_members") else pd.DataFrame()
    members = normalize_index_members(members) if members is not None and len(members) else None

    fundamentals = loader.load_fundamentals(symbols=symbols) if hasattr(loader, "load_fundamentals") else pd.DataFrame()
    fundamentals = normalize_fundamentals(fundamentals) if fundamentals is not None and len(fundamentals) else None

    return MarketDataBundle(bars=bars, index_members=members, fundamentals=fundamentals, meta={"provider": config.provider})
