"""数据层：PIT 存储、复权、股票池、质量校验，以及统一取数抽象（M4）。

分工：

- ``provider`` / ``cache`` / ``ratelimit`` / ``quality`` / ``registry`` —— **取数层**（M4）：
  能力声明、溯源、本地缓存、限流重试、质量检查、注册表；
- ``loader`` / ``store`` / ``universe`` / ``schema`` —— **使用层**：把取到的数据变成
  可直接回测的 PIT 视图。取数层不改使用层的对外语义。
"""

from __future__ import annotations

from .cache import CacheLookup, CacheMeta, DataCache, parquet_available
from .akshare_provider import AKShareProvider
from .file_provider import CsvProvider, FileProvider, ParquetProvider
from .loader import (
    INGEST_STEPS,
    STEP_STATUSES,
    BarDataLoader,
    CsvBarLoader,
    IngestReport,
    ParquetBarLoader,
    ingest_from_provider,
    load_market_data,
)
from .provider import (
    BACKTEST_REQUIRED,
    CONDITIONAL_REQUIRED,
    DataProvider,
    ProviderCapabilities,
    ProviderHealth,
    Provenance,
    degradation_notes,
    filter_effective_window,
    utc_now,
)
from .quality import (
    QualityChecker,
    QualityFinding,
    QualityThresholds,
    ProviderQualityReport,
)
from .ratelimit import RateLimiter, resolve_exceptions, retry_call
from .registry import (
    PENDING_PROVIDERS,
    PROVIDER_REGISTRY,
    ProviderContext,
    ProviderFactory,
    available_providers,
    build_provider,
    get_provider_factory,
    provider_capabilities,
    register_provider,
)
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
from .synthetic_provider import SyntheticProvider
from .universe import UniverseBuilder, UniverseStats

__all__ = [
    # ---- 取数抽象（M4-2）----
    "DataProvider",
    "ProviderCapabilities",
    "Provenance",
    "ProviderHealth",
    "BACKTEST_REQUIRED",
    "CONDITIONAL_REQUIRED",
    "degradation_notes",
    "filter_effective_window",
    "utc_now",
    # ---- 缓存（M4-3）----
    "DataCache",
    "CacheMeta",
    "CacheLookup",
    "parquet_available",
    # ---- 限流与重试（M4-4）----
    "RateLimiter",
    "retry_call",
    "resolve_exceptions",
    # ---- 质量检查（M4-5）----
    "QualityChecker",
    "QualityFinding",
    "QualityThresholds",
    "ProviderQualityReport",
    # ---- provider 实现（M4-6 / M4-8）----
    "SyntheticProvider",
    "FileProvider",
    "CsvProvider",
    "ParquetProvider",
    "AKShareProvider",
    # ---- 注册表（M4-7）----
    "PROVIDER_REGISTRY",
    "PENDING_PROVIDERS",
    "ProviderContext",
    "ProviderFactory",
    "register_provider",
    "available_providers",
    "get_provider_factory",
    "build_provider",
    "provider_capabilities",
    # ---- 使用层（第一轮交付，语义不变）----
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
    # ---- provider → store 适配层（M4-10）----
    "IngestReport",
    "ingest_from_provider",
    "INGEST_STEPS",
    "STEP_STATUSES",
]
