# M4 设计：DataProvider 抽象层（接口 / 数据结构 / 伪代码 / 测试计划）

> 状态：**设计待确认**。按合同 §八.1「先给接口、伪代码、测试用例，确认后再写实现」。
>
> 依据：[`docs/11_akshare_provider.md`](11_akshare_provider.md) §3~§6、
> [`docs/10_round2_design.md`](10_round2_design.md) §3.1。
>
> 本文件不含实现代码；确认后按 §7 的模块顺序逐个交付。

---

## 1. 目标与边界

### 1.1 目标

把「数据从哪来」与「数据怎么用」彻底解耦，使 `DataStore` 只面对 canonical schema：

```
Provider（synthetic / csv / parquet / akshare）
   → 统一的 canonical 表（bars / index_members / fundamentals / symbol_meta）
   → ingest_from_provider（校验 + 质量报告 + 溯源）
   → DataStore（PIT 守卫 / 双价格 / 单位换算，语义不变）
```

### 1.2 硬约束（逐条可验收）

| # | 约束 | 验收方式 |
| --- | --- | --- |
| C1 | **不改 `src/aqs/data/store.py` 的对外接口** | 现有 605 用例全绿；`git diff` 中 store.py 无接口签名变更 |
| C2 | 四 provider 走**同一 schema** | 契约测试对四个实现各跑一遍（见 §8.1） |
| C3 | 缓存按**参数哈希**去重；schema 变更即 **+1 版本** | `tests/test_data_cache.py` 断言 |
| C4 | 限流与重试**必须有测试**，含「空结果不重试」负面用例 | `tests/test_ratelimit.py` |
| C5 | **不引入 pyarrow 作为硬依赖**；不可用时退化 CSV + warning | 契约/缓存用例通过 monkeypatch 模拟 pyarrow 缺失 |
| C6 | 不改双价格体系与 PIT 语义 | `normalize_bars` 复用；契约测试断言 `*_adj` 列由 store 侧生成 |

### 1.3 明确不做（本模块）

- ❌ 不联网、不调用真实 AKShare（M5 的 `probe_akshare.py` / `fetch_data.py` 才联网，且由使用者执行）；
- ❌ 不做 M6/M7 的指标与报告（只把质量报告与 manifest 产出为**数据结构 + 可选落盘**）；
- ❌ 不引入任何第三方回测框架（`tests/test_packaging.py` 已机械禁止）。

---

## 2. 模块清单与依赖方向

```
config/schema.py        新增 ProviderConfig / CacheConfig / RateLimitConfig / RetryConfig
        │
        ▼
data/provider.py        DataProvider 协议 + ProviderCapabilities + Provenance + ProviderHealth
        │               （纯契约，零依赖，不 import pandas 之外的东西）
        ├── data/cache.py         DataCache + CacheMeta（pyarrow 可选，退化 CSV）
        ├── data/ratelimit.py     RateLimiter（令牌桶，时钟可注入）+ retry_call（退避可注入）
        ├── data/quality.py       Q1~Q12 检查（纯函数；输入 canonical 表，输出报告）
        │
        ├── data/synthetic_provider.py   SyntheticProvider（包装 generate_market_data）
        ├── data/file_provider.py        CsvProvider / ParquetProvider（包装 BarDataLoader）
        └── data/akshare_provider.py     AKShareProvider（client 可注入；M4 映射骨架 / M5 完整）
        │
        ▼
data/registry.py        register_provider / build_provider / available_providers
        │
        ▼
data/loader.py          ingest_from_provider（适配层，落到现有 DataStore）
        │
        ▼
data/__init__.py        导出新 API（不改既有导出）
```

**依赖方向单向**：`registry → providers → provider/cache/ratelimit`；
`loader(ingest) → provider + quality`；**`provider.py` 不 import 任何 provider 实现**（避免环）。

---

## 3. 数据结构

### 3.1 能力与溯源（`data/provider.py`）

```python
@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """数据源能力声明：上层据此决定校验强度与降级路径。"""
    daily_bars: bool = False
    adjustment_factors: bool = False
    suspensions: bool = False
    price_limits: bool = False
    st_flags: bool = False
    listing_dates: bool = False
    delistings: bool = False
    index_members: bool = False            # 是否提供**历史**成分（False → 只能快照近似）
    fundamentals: bool = False             # True 表示含 announce_date（公告日口径可用）
    industry: bool = False
    market_cap: bool = False
    intraday: bool = False
    default_adjust: str = "none"           # none | hfq | qfq
    notes: tuple[str, ...] = ()

    def missing_required(self) -> tuple[str, ...]:
        """回测必需能力中缺失的部分（供 ingest 决定报错还是降级）。"""

    def as_dict(self) -> dict[str, Any]: ...
```

```python
@dataclass(frozen=True, slots=True)
class Provenance:
    """一次取数的溯源（写进 summary.json / 数据清单）。"""
    source: str                    # "synthetic" | "csv" | "parquet" | "akshare" | "fallback:parquet"
    fetched_at: datetime
    cache_hit: bool
    rows: int
    symbols: int
    params_hash: str = ""
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]: ...
```

```python
@dataclass(slots=True)
class ProviderHealth:
    """健康检查结果。"""
    ok: bool
    checked_at: datetime
    latency_ms: float
    details: dict[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
```

### 3.2 缓存（`data/cache.py`）

```python
@dataclass(frozen=True, slots=True)
class CacheMeta:
    dataset: str            # bars | index_members | fundamentals | symbol_meta | calendar
    key: str                # 600000.SH | 000300.SH | calendar | __all__
    provider: str
    schema_version: int     # 来自 CacheConfig.version；schema 变更即 +1
    rows: int
    start: str | None
    end: str | None
    fetched_at: str         # ISO8601
    params_hash: str
    fmt: str                # parquet | csv（实际落盘格式，用于 pyarrow 缺失时自解释）
    path: str

    def as_dict(self) -> dict[str, Any]: ...

@dataclass(frozen=True, slots=True)
class CacheLookup:
    """一次缓存查询的结果。"""
    frame: pd.DataFrame | None
    meta: CacheMeta | None
    hit: bool               # 命中且新鲜
    reason: str             # miss_no_file | miss_stale | miss_params_hash | miss_version | hit
```

### 3.3 配置（`config/schema.py` 新增，全部进 `configs/base.yaml`）

```python
@dataclass(slots=True)
class RetryConfig:
    max_attempts: int = 5
    backoff: float = 1.5
    jitter: bool = True
    retry_on: tuple[str, ...] = ("ConnectionError", "TimeoutError", "OSError", "ValueError")
    # 注：用**类名字符串**而非异常类，避免配置层 import 网络异常类型（保持纯数据）

@dataclass(slots=True)
class RateLimitConfig:
    enabled: bool = True
    requests_per_minute: int = 300
    burst: int = 10
    min_interval_ms: float = 0.0

@dataclass(slots=True)
class CacheConfig:
    enabled: bool = True
    root: str = "data/cache"
    version: int = 1
    fmt: str = "parquet"          # parquet | csv；pyarrow 不可用时自动降级
    ttl_hours: float = 24.0
    refresh: bool = False

@dataclass(slots=True)
class ProviderConfig:
    """四个 provider 共用的取数行为配置。"""
    cache: CacheConfig = field(default_factory=CacheConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    failure_policy: str = "fallback"   # fallback | fail（接口整体不可用时）
    max_missing_ratio: float = 0.01    # 单标的失败比例阈值，超过升级为 error
    unit: str = "share"                # share（股）| lot（手）—— Q3 的换算依据
    paths: PathsConfig = field(default_factory=PathsConfig)   # csv/parquet provider 的目录
```

并在 `DataConfig` 增加 `providers: dict[str, ProviderConfig]`？**不建议**（配置会过深）。
改为：`DataConfig.cache / rate_limit / retry` 三个平级字段 + `DataConfig.adjustment` 复用现有字段。
理由：限制嵌套深度，且四 provider 的取数行为本就应当一致（差异只在映射）。

> `data.provider` 现有取值 `synthetic|csv|parquet|akshare` 不变；`data.root` 继续给 csv/parquet 用。

---

## 4. 接口定义

### 4.1 `data/provider.py`

```python
@runtime_checkable
class DataProvider(Protocol):
    name: str

    def capabilities(self) -> ProviderCapabilities: ...
    def health_check(self) -> ProviderHealth: ...

    def fetch_bars(self, symbols: Sequence[str], start: DateLike, end: DateLike, *,
                   adjust: str = "none") -> tuple[pd.DataFrame, Provenance]: ...
    def fetch_symbol_meta(self, symbols: Sequence[str]) -> tuple[pd.DataFrame, Provenance]: ...
    def fetch_index_members(self, index_code: str, start: DateLike, end: DateLike
                            ) -> tuple[pd.DataFrame, Provenance]: ...
    def fetch_fundamentals(self, symbols: Sequence[str], start: DateLike, end: DateLike
                           ) -> tuple[pd.DataFrame, Provenance]: ...
    def fetch_trading_calendar(self, start: DateLike, end: DateLike
                               ) -> tuple[list[_date], Provenance]: ...
    def fetch_industry(self, symbols: Sequence[str]) -> tuple[pd.DataFrame, Provenance]: ...
```

**统一返回约定**（契约测试逐条断言）：

| 方法 | 返回列（canonical，**未归一前**的原始列名由各 provider 负责映射） |
| --- | --- |
| `fetch_bars` | `date, symbol, open, high, low, close, volume(股), amount(元), adj_factor, is_suspended, is_st, limit_up, limit_down, prev_close, list_date, delist_date, industry?` |
| `fetch_symbol_meta` | `symbol, name?, board?, list_date, delist_date?, industry?, total_mv?` |
| `fetch_index_members` | `index_code, symbol, effective_from, effective_to` |
| `fetch_fundamentals` | `symbol, report_period, announce_date, ...fields` |
| `fetch_trading_calendar` | `list[date]`（升序、去重） |
| `fetch_industry` | `symbol, industry, effective_from?` |

### 4.2 `data/cache.py`

```python
class DataCache:
    def __init__(self, root: str | Path, *, version: int, fmt: str = "parquet",
                 ttl_hours: float = 24.0) -> None
    # 只读查询
    def lookup(self, dataset: str, key: str, *, params: Mapping[str, Any] | None = None,
               now: datetime | None = None) -> CacheLookup
    def read(self, dataset: str, key: str) -> tuple[pd.DataFrame | None, CacheMeta | None]
    # 写入与增量
    def write(self, dataset: str, key: str, frame: pd.DataFrame, *,
              params: Mapping[str, Any] | None = None,
              start: Any = None, end: Any = None, now: datetime | None = None) -> CacheMeta
    def merge_incremental(self, cached: pd.DataFrame, fresh: pd.DataFrame, *,
                          keys: Sequence[str]) -> pd.DataFrame
    # 清单
    def manifest(self) -> list[CacheMeta]
    def params_hash(self, params: Mapping[str, Any] | None) -> str
    def path_for(self, dataset: str, key: str) -> Path
    def clear(self, *, dataset: str | None = None) -> int     # 返回删除文件数

def parquet_available() -> bool
```

**降级规则（C5）**：`fmt="parquet"` 且 `parquet_available()` 为假 →
① 改用 `.csv` 落盘；② 发一次 `logger.warning`（只发一次，不刷屏）；③ `CacheMeta.fmt="csv"` 如实记录；
④ **不抛异常**。

### 4.3 `data/ratelimit.py`

```python
class RateLimiter:
    def __init__(self, *, requests_per_minute: int, burst: int,
                 min_interval_ms: float = 0.0, clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep) -> None
    def acquire(self, n: int = 1) -> float      # 返回本次等待秒数（0 表示未等待）
    def try_acquire(self, n: int = 1) -> bool
    def wait_time(self, n: int = 1) -> float
    def stats(self) -> dict[str, float]         # acquired/waited_total/waited_max/waits

def retry_call(fn: Callable[[], T], *, max_attempts: int = 5, backoff: float = 1.5,
               jitter: bool = True, retry_on: Sequence[type[BaseException]] = (...),
               sleeper: Callable[[float], None] = time.sleep,
               random: Callable[[], float] = random.random,
               on_retry: Callable[[int, BaseException, float], None] | None = None,
               is_empty: Callable[[T], bool] | None = None) -> T
```

**可测试性关键**：`clock` / `sleeper` / `random` **全部可注入** →
测试用假时钟与假 sleeper，断言「等待了多久、重试了几次」，**不真 sleep**。

**「空结果不重试」语义**：`is_empty(result)` 为真时**立即返回**该结果，不计失败、不重试；
由调用方转成 warning 进质量报告（空结果可能是停牌/退市/无数据，重试无意义且浪费配额）。

### 4.4 `data/quality.py`

```python
@dataclass(slots=True)
class QualityFinding:
    code: str            # Q1..Q12
    level: str           # error | warning
    message: str
    count: int = 0
    samples: list[dict[str, Any]] = field(default_factory=list)   # 前 20 条

@dataclass(slots=True)
class ProviderQualityReport:
    findings: list[QualityFinding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool: ...
    def errors(self) -> list[QualityFinding]: ...
    def warnings(self) -> list[QualityFinding]: ...
    def to_dict(self) -> dict[str, Any]: ...
    def to_markdown(self, *, max_samples: int = 20) -> str: ...
    def exit_code(self) -> int: ...        # 0 无 error；2 有 error（供 CLI 使用）
    def merge(self, other: "ProviderQualityReport") -> "ProviderQualityReport": ...

class QualityChecker:
    """Q1~Q12。纯函数式：输入 canonical 表，输出报告；不修改输入。"""
    def __init__(self, *, thresholds: QualityThresholds | None = None) -> None
    def check_bars(self, bars, *, calendar=None, symbol_meta=None) -> ProviderQualityReport
    def check_fundamentals(self, fundamentals, *, symbol_meta=None) -> ProviderQualityReport
    def check_index_members(self, members, *, symbol_meta=None) -> ProviderQualityReport
    def check_cross(self, bars, *, symbol_meta, members=None) -> ProviderQualityReport
    def check_survivorship(self, bars, *, symbol_meta, candidates) -> ProviderQualityReport
    def run_all(self, *, bars, symbol_meta, fundamentals=None, members=None) -> ProviderQualityReport
```

Q1~Q12 的判定沿用 `docs/11` §7 表格（其中 **Q3 量级校验**与 **Q12 幸存者偏差自检**必须有专门用例）。

### 4.5 `data/registry.py`

```python
@dataclass(frozen=True, slots=True)
class ProviderContext:                       # 工厂入参：显式传递依赖，不用全局状态
    name: str
    config: DataConfig
    root: Path
    cache: DataCache | None = None
    limiter: RateLimiter | None = None
    client: Any | None = None                # akshare 客户端（生产真库 / 测试 FakeClient）
    index_members_path: Path | None = None
    fundamentals_path: Path | None = None

ProviderFactory = Callable[[ProviderContext], DataProvider]

def register_provider(name: str, factory: ProviderFactory, *, overwrite: bool = False) -> None
def available_providers() -> list[str]
def get_provider_factory(name: str) -> ProviderFactory
def build_provider(config: DataConfig | Mapping[str, Any] | None = None, *,
                   provider: str | None = None,
                   root: str | Path | None = None,
                   cache: DataCache | None = None,
                   limiter: RateLimiter | None = None,
                   client: Any | None = None,
                   index_members_path: str | Path | None = None,
                   fundamentals_path: str | Path | None = None) -> DataProvider
def provider_capabilities(name: str, *, config=None, strict: bool = True) -> ProviderCapabilities
```

四个内置实现：`synthetic`（默认）、`csv`、`parquet`、`akshare`。

- `build_provider` 对未注册名字抛 **`ConfigError`**（A1 决议；与 strategy/portfolio 注册表风格一致），
  **绝不静默回退到 synthetic**；
- 工厂返回对象必须满足 `DataProvider` 协议，否则抛 `DataError` 并列出缺失属性（构建期闸门）；
- **注册表与配置的分工**（A4 决议）：`DataConfig.__post_init__` 只校验
  `provider in KNOWN_PROVIDERS`（声明式内置白名单，不查注册表）；
  「名字是否有实现」由 `build_provider` 在运行层查注册表判定；
- `_unregistered_builtins()` import 期自检「内置名单 - 已注册 - `PENDING_PROVIDERS`」，
  非空即拒绝导入；分步交付期尚未接入的实现写进 `PENDING_PROVIDERS`（显式状态，M4-8 清空）；
- `provider_capabilities` 的 **strict / degraded** 两态：`strict` 是**可用性闸门**而非
  「探测过程是否报错」——文件型 provider 的探测失败表现为「行情抽样失败 → `daily_bars=False`」
  （见 §4.1：能力探测不抛异常，只写 `notes`），所以闸门必须落在**能力**上：
  `strict=True` 时构建失败或 `BACKTEST_REQUIRED`（`daily_bars`/`listing_dates`）缺失即抛
  `DataError`；`strict=False` 时始终返回声明（含全 `False` 下界）并在 `notes` 写明原因 ——
  「查不到能力」不得伪装成「天生没有这个能力」（能力矩阵报告用 `strict=False`）；
- `root` 等路径一律复用 `config.loader.resolve_path`（以项目根为基准），
  避免「换个 CWD 就读到另一个 `data/raw`」这种不报错的失真。

### 4.6 `data/loader.py` 扩展

```python
@dataclass(slots=True)
class IngestResult:
    store: DataStore
    quality: ProviderQualityReport
    provenance: dict[str, Provenance]
    manifest: list[CacheMeta]
    capabilities: ProviderCapabilities
    diagnostics: dict[str, Any] = field(default_factory=dict)   # 降级/近似披露

def ingest_from_provider(provider: DataProvider, *, config: DataConfig,
                         universe_config: UniverseConfig,
                         index_codes: Sequence[str] = ("000300.SH",),
                         start: DateLike, end: DateLike,
                         symbols: Sequence[str] | None = None,
                         fundamentals: bool = True,
                         industry: bool = True) -> IngestResult
```

流程（9 步，同 `docs/11` §5.1）：
`calendar → index_members(union) → symbol_meta → bars(+停牌/涨跌停/ST) → fundamentals+industry
→ normalize_* → validate_bars + QualityChecker → DataStore → manifest + 质量报告`

**降级披露一致性**：`capabilities.index_members=False` 时，`diagnostics["index_members_approximated"]=True`
且 universe 在这些日期退化为 `all_listed`（与缺陷 #8 的「不得静默兜底」同一原则）；
同时**把该警告写入 `IngestReport.quality`**（不只是 stdout/日志），使报告层能直接引用。

### 4.8 `loader.ingest_from_provider` 实现决议（M4-10）

实现为 `IngestReport`（比初稿的 `IngestResult` 多一个 `diagnostics` 字段，供 D1 的
「原始校验报告」与报告层使用）。九步的步骤名固定为
`calendar / index_members / symbol_meta / bars / fundamentals / industry / normalize / validate / store`
（`INGEST_STEPS`），取值域 `ok | degraded | failed | skipped`（`STEP_STATUSES`）。

**`skipped` 与 `degraded` 的分界（关键）**：`skipped` **只表示用户显式没请求**
（如 `fundamentals=False`）；「源不支持该能力」**属于 `degraded`**，不是 `skipped`。
否则报告层会把「这个源没有财务数据」误读成「这次没要财务数据」。

| 步骤 | 情形 | 步态 | 行为 |
|---|---|---|---|
| `symbol_meta` | 源**不支持**上市日（`listing_dates=False`） | `degraded` | 不中止 → 无 meta 模式 → 由 `data.listing_date.policy` 决定（`strict` 由 store 报错 / `proxy` 代理口径并登记披露） |
| `symbol_meta` | 源声称支持但**抓取失败** | `degraded`（`fallback`）/ 抛错（`fail`） | `failure_policy` 决定 |
| `fundamentals` / `industry` | 源不支持 | `degraded` | 可选数据，**一律不中止** |
| `fundamentals` / `industry` | 抓取失败 | `degraded`（`fallback`）/ `failed`（`fail`） | 可选数据，**一律不中止**（`fail` 只体现为「本次无该数据」） |
| `fundamentals` / `industry` | 用户显式没请求 | `skipped` | 唯一该用 `skipped` 的情形 |
| `bars` | 取数失败或为空 | 抛 `DataError` | 必需步骤，消息带 `step_status` 快照 |
| `validate` | `DataQualityReport.errors` 非空 | `failed`（strict，抛 `DataQualityError` 附报告）/ `degraded`（non-strict，继续） | D1：校验错误必须影响步态，不能只做信息记录 |
| `index_members` | 成分缺失 | `degraded` | `fallback_to_all=True` → 退化全市场并披露；`False` → 抛错（不静默退化） |

**`degradation_notes` 必须具体**（D4）：逐条写明被触发的**既有口径**，例如
「`is_suspended` 缺失 → 由 `volume<=0` 推断停牌」「`limit_up/limit_down` 缺失 →
按板块规则推算（主板 10% / 创业板·科创板 20% / 北交所 30% / 主板 ST 5%，取自
`PriceLimitConfig` 实际值）」「`is_st` 缺失 → 默认 False」——而不是笼统的一句「已降级」。

**两条属性**：`has_errors = (not quality.ok) or step_status["validate"] == "failed"`；
`overall_status` 按 `failed > degraded > ok` 汇总。

**`manifest` 语义**：只有提供 `cache_manifest()` 的 provider 才有缓存清单；
无缓存能力时 `manifest == []` **并在 notes 里说明**，避免被误读成「抓取失败」。

**失败原子性**：失败不影响已写入的缓存；重跑时前面步骤走缓存命中，不会重复抓取
（适配层不清理、不回滚缓存）。

**`capabilities` 语义**：provider 声明它**理论上**具备的能力，不代表本次一定拿到数据；
本次实际达成情况看 `step_status`。

### 4.7 `data/akshare_provider.py` 实现决议（M4-8）

| # | 决议 | 理由 |
|---|---|---|
| 1 | **多标的循环显式化**：`fetch_bars` 逐标的调用 + 逐标的缓存；失败只记 warning，比例超过 `data.max_missing_ratio` 升级为 `DataError`；标的数 > 50 时每 10 个写一条 `audit.log`；合并后按 `(date, symbol)` 升序 | AKShare 日线接口一次只查一个代码，`_call` 不能假设「一次拿全」 |
| 2 | 协议文档新增「**多标的取数约定**」（`provider.py` 的 `DataProvider` docstring）：空序列=不过滤、部分失败语义、合并顺序、主键唯一、闭区间、`Provenance.rows` 口径 | 让四个实现有同一套可断言的对外行为 |
| 3 | **快照累积是状态不是缓存**：纯函数 `_accumulate_snapshot(cached, current, snapshot_date)`；固定 key（如 `000300.SH`，不带日期）；`meta.start/end` 表示已累积区间；**同日重复抓取幂等**（内容不同则保留首次并告警） | 历史成分只能靠逐日观测累积，key-value 缓存语义表达不了「新进/退出/留存」 |
| 4 | 成交量 **手→股**用模块常量 `_DEFAULT_VOLUME_TO_SHARES = 100.0`，**不读** `data.unit_conversion`；并在 `capabilities().notes` 写明已换算 | 该配置默认 1.0，使用者一旦忘改，量纲整体错 100 倍且**不报错**；配置被显式改非默认值时，会在 warnings 里披露该覆盖无效 |
| 5 | 时间戳统一 `provider.utc_now()`（带时区 UTC）；缓存层的 `now` 入参也统一规整为 UTC（naive 按 **UTC** 解释，历史 manifest 的 naive 值按**本地时间**换算） | naive/aware 相减会直接抛 `TypeError`；且按本地时间解释会让测试结果随机器变化 |
| 6 | `fetch_trading_calendar` 缓存存**表**（单列 `date`），对外转升序去重的 `list[date]`，`Provenance.rows = len(days)` | 单一存储形态，避免「同一数据集两种落盘格式」 |
| 7 | 指数成分**声明不可用但仍返回数据 + 具体警告**（写明 index_code、请求区间、快照日、后果） | 数据是真的，只是语义不满足历史要求；返回空表等于假装没有数据，静默当历史用则造成幸存者偏差 |

**能力声明（未经联网探测前的保守口径）**：`daily_bars` / `adjustment_factors` / `listing_dates` /
`fundamentals` / `industry` 为 `True`；`index_members` / `suspensions` / `price_limits` /
`st_flags` / `delistings` / `market_cap` / `intraday` 为 `False`。
`industry` 声明为 `True` 的理由：它以 `effective_from/effective_to` 区间累积，且 note 明确
「首次抓取日之前不可得」；而指数成分直接决定股票池、误用即幸存者偏差，故按最严标准声明。

---

## 5. 关键伪代码

### 5.1 缓存：命中判定与增量

```
lookup(dataset, key, params, now):
    meta = 读 manifest 中 (dataset,key);  frame_path = path_for(dataset,key)
    若 文件不存在 or meta 不存在            → miss_no_file
    若 meta.schema_version != cfg.version   → miss_version        # 旧版本不读、不删
    若 meta.params_hash != hash(params)     → miss_params_hash
    若 now - meta.fetched_at > ttl_hours    → miss_stale（但**仍返回 frame**，供增量使用）
    否则                                    → hit
    返回 CacheLookup(frame=(未命中时也返回 frame，便于增量), meta, hit, reason)
```

```
fetch_with_cache(dataset, key, params, fetch_fn, *, keys, start, end, refresh):
    lk = lookup(...)
    若 refresh: 忽略 lk.hit
    若 lk.hit and not refresh:
        return lk.frame, Provenance(cache_hit=True, rows=len(lk.frame))

    # 增量：只抓「缓存最大日期 +1 天 ~ end」
    inc_start = start
    若 lk.frame 非空 and lk.meta.end 存在:
        inc_start = max(start, lk.meta.end + 1 天)
    若 inc_start > end:                     # 已覆盖，无需抓
        return lk.frame, Provenance(cache_hit=True, warnings=("区间已覆盖，未发起抓取",))

    limiter.acquire()
    fresh = retry_call(lambda: fetch_fn(inc_start, end), is_empty=lambda df: len(df)==0, ...)
    若 fresh 为空:
        warning「空结果不重试」→ 质量报告
        return lk.frame, Provenance(cache_hit=bool(lk.frame), warnings=(...))

    merged = merge_incremental(lk.frame, fresh, keys=keys)   # 按主键去重，保留 fresh
    meta = write(dataset, key, merged, params=params)
    return merged, Provenance(cache_hit=False, rows=len(merged), params_hash=meta.params_hash)
```

`merge_incremental`：`concat([cached, fresh])` → `drop_duplicates(subset=keys, keep="last")`
→ 按 `date`（或 `effective_from`）排序 → `reset_index(drop=True)`；
**断言**：合并后行数 ≥ `max(len(cached), len(fresh))`（防丢数据）。

### 5.2 令牌桶

```
acquire(n):
    waited = 0
    循环:
        补充令牌:  elapsed = clock() - last_refill
                  tokens = min(burst, tokens + elapsed * rate_per_sec)   # rate = rpm/60
                  last_refill = clock()
        若 tokens >= n:
            tokens -= n; acquired += n
            若 min_interval_ms > 0:  # 两次请求最小间隔
                need = min_interval - (clock() - last_request_at)
                若 need > 0: sleeper(need); waited += need
            last_request_at = clock(); return waited
        need = (n - tokens) / rate_per_sec
        若 min_interval_ms > 0: need = max(need, min_interval - (clock() - last_request_at))
        sleeper(need); waited += need
```

**边界**：`requests_per_minute <= 0` → 视为不限流（`acquire` 立即返回 0，`stats` 记录 disabled）；
`n > burst` → 抛 `DataError`（配置错误，不允许无限等待）。

### 5.3 `retry_call`

```
retry_call(fn, ...):
    last_exc = None
    对 attempt in 1..max_attempts:
        尝试:  result = fn()
              若 is_empty 存在 且 is_empty(result): return result      # 空结果不重试
              return result
        捕获 exc:
              若不属 retry_on: raise                                  # 不可重试 → 直接抛
              last_exc = exc
              若 attempt == max_attempts: break
              delay = backoff ** (attempt-1)
              若 jitter: delay *= (0.5 + random() * 0.5)              # 抖动 ∈ [0.5,1)
              若 on_retry: on_retry(attempt, exc, delay)
              sleeper(delay)
    raise last_exc
```

**审计**：`on_retry` 由 provider 传入，写 `audit.log("provider", "retry", ...)`。

### 5.4 契约测试基类（`tests/contracts/provider_contract.py`）

```python
class ProviderContractMixin:
    """四个 provider 共用的契约（子类实现 make_provider() 即可）。"""
    def make_provider(self) -> DataProvider: raise NotImplementedError
    def contract_bars_symbols(self) -> list[str]: ...

    def test_capabilities_are_declared(self): ...
    def test_health_check_returns_result(self): ...
    def test_fetch_bars_schema_and_types(self): ...
    def test_fetch_bars_respects_symbols_and_range(self): ...
    def test_fetch_symbol_meta_has_listing_dates(self): ...
    def test_fetch_calendar_is_sorted_unique(self): ...
    def test_fetch_returns_provenance(self): ...
    def test_missing_capability_returns_empty_not_error(self): ...
```

每个 `test_provider_*.py` 里：
```python
class TestSyntheticProvider(ProviderContractMixin): ...
class TestCsvProvider(ProviderContractMixin): ...
class TestParquetProvider(ProviderContractMixin): ...
class TestAKShareProvider(ProviderContractMixin): ...    # 用 FakeClient，离线
```

> 运行器已支持 `Test*` 类（`run_tests.py:39-43`），pytest 同样收集，两边一致。
> 基类**不叫** `Test*`，因此不会被直接收集（避免基类被当成用例跑）。

---

## 6. 需要你确认的 4 个开放问题

| # | 问题 | 我的建议 |
| --- | --- | --- |
| **O1** | `akshare_provider.py` 在 M4 就要落地（否则「四 provider 契约全绿」无法达成），但你的分工把它放在 M5 | **M4 落「可注入 client 的映射骨架 + MAPPERS」并用 `FakeClient` 过契约；M5 再做真实 fixture、`probe_akshare.py`、`fetch_data.py`、字段核对**。这样 M4 验收标准可达成，且 M5 只剩联网相关部分 |
| **O2** | 契约测试按 **4 个实现各跑一遍**，用例数会放大 | 预计新增 **约 50 条**（契约 8×4=32，cache 7，ratelimit 6，quality 9，registry 3），而非你估的 30 条。若你要求控制在 30 条，我会把契约基类压到 5 条（→ 20）并把 quality 拆一部分到 M5 |
| **O3** | `quality.py` 的「报告落盘 + 退出码」在 M4 做还是 M5 做 | **M4 只做纯函数 + 报告数据结构 + `to_markdown()`/`exit_code()`；文件落盘与 CLI 归 M5 的 `fetch_data.py`**（避免 M4 引入 CLI 层） |
| **O4** | 新增配置的层级 | **平铺**：`data.cache / data.rate_limit / data.retry / data.unit`，不做 `data.providers.<name>.*` 嵌套（四 provider 行为应一致，差异只在映射） |

---

## 7. 交付顺序（每步单独提交 + 单独跑测试）

| 步 | 内容 | 新增用例（估） |
| --- | --- | --- |
| M4-1 | `config/schema.py` 四个配置块 + `configs/base.yaml` 同步 | 4 |
| M4-2 | `data/provider.py`（协议 + 3 个数据结构 + `missing_required`） | 5 |
| M4-3 | `data/cache.py`（含 pyarrow 降级 + 增量 + manifest） | 7 |
| M4-4 | `data/ratelimit.py`（令牌桶 + retry_call，时钟/睡眠可注入） | 6 |
| M4-5 | `data/quality.py`（Q1~Q12 + 报告 + markdown + exit_code） | 9 |
| M4-6 | 三个 provider 实现（synthetic / file） | 6 |
| M4-7 | `data/registry.py` + `data/__init__.py` 导出 | 3 |
| M4-8 | `akshare_provider.py` 映射骨架 + `FakeClient` | 5 |
| M4-9 | `tests/contracts/provider_contract.py` + 四个子类 | 32 |
| M4-10 | `loader.py::ingest_from_provider` 适配层 | 6 |

> 预计新增 **约 83 条**（若采纳 O2 的压缩方案则约 60 条）。**确认后我按 M4-1 → M4-10 逐步交付，
> 每步都跑全量测试并报告红绿**，不会一次写完全部代码。

---

## 8. 验收标准（本模块 Definition of Done）

1. 四个 provider 的契约测试全绿（同一基类、同一断言集合）；
2. 现有 605 用例**零回归**；
3. `store.py` 对外接口零变更（PIT 守卫 / 双价格 / 单位换算原样）；
4. 缓存用例覆盖：命中 / 过期 / 参数哈希不符 / 版本变更 / 增量合并 / pyarrow 缺失降级；
5. 限流与重试用例覆盖：令牌桶限速（假时钟）、退避与抖动（假 sleeper）、**空结果不重试**、不可重试异常直接抛；
6. 质量检查覆盖：Q3（手→股量级）与 Q12（幸存者偏差）各有专门用例，且各有**反例**（构造错误数据必须被抓到）；
7. 文档同步：`docs/11` 回填实际接口、`docs/DEVELOPMENT.md` 看板更新、`README` 目录树与文档索引更新；
8. 新增用例数与文档数字由 `tests/test_defect_11_docs_consistency.py` 机械守护。
