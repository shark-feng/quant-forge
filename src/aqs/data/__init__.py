"""数据层：PIT 存储、复权、股票池、质量校验。"""

from __future__ import annotations

from .loader import BarDataLoader, CsvBarLoader, ParquetBarLoader, load_market_data
from .schema import (
    BARS_COLUMNS,
    FUNDAMENTAL_COLUMNS,
    INDEX_MEMBER_COLUMNS,
    DataQualityReport,
    add_adjusted_prices,
    compute_limit_prices,
    infer_board,
    normalize_bars,
    normalize_fundamentals,
    normalize_index_members,
    select_fundamentals_asof,
    validate_bars,
)
from .store import DataStore, PITView
from .universe import UniverseBuilder, UniverseStats

__all__ = [
    "BARS_COLUMNS",
    "FUNDAMENTAL_COLUMNS",
    "INDEX_MEMBER_COLUMNS",
    "DataQualityReport",
    "add_adjusted_prices",
    "compute_limit_prices",
    "infer_board",
    "normalize_bars",
    "normalize_fundamentals",
    "normalize_index_members",
    "select_fundamentals_asof",
    "validate_bars",
    "DataStore",
    "PITView",
    "UniverseBuilder",
    "UniverseStats",
    "BarDataLoader",
    "CsvBarLoader",
    "ParquetBarLoader",
    "load_market_data",
]
