# M4 取数层成文（DataProvider 抽象 + 四实现 + 适配层）

> 对应代码：`src/aqs/data/`（`provider.py` / `cache.py` / `ratelimit.py` / `quality.py` /
> `registry.py` / `synthetic_provider.py` / `file_provider.py` / `akshare_provider.py` /
> `loader.py::ingest_from_provider`）
> 设计来源：`docs/18_m4_dataprovider.md`（接口与决策记录）、`docs/11_akshare_provider.md`（AKShare 映射）
> 契约测试：`tests/contracts/provider_contract.py` + `tests/test_provider_contract.py`（4 × 8 = 32 条）
> 状态：M4-1 ~ M4-10 已交付；**联网探测与真实抓取属 M5**（`tools/probe_akshare.py`）

---

## 1. 目标与边界

取数层回答一个问题：**「这份数据是从哪来的、能不能用、缺什么、缺了以后会怎样」**。
它把「数据从哪来」与「数据怎么用」解耦：上层（`ingest_from_provider`、`DataStore`）
只面对 canonical schema，不关心背后是合成数据、CSV、Parquet 还是 AKShare。

**三条不可让步的约束**（贯穿全部实现与测试）：

1. **能力必须显式声明** —— 数据源缺什么，上层据此决定「报错」还是「降级并披露」，
   **绝不允许静默兜底**（与缺陷 #8 的上市日处理同一原则）；
2. **每次取数都要有溯源** —— 报告里的数字要能追到「哪个源、何时抓的、是否命中缓存、参数哈希」；
3. **缺失与降级要能翻译成人话** —— 供 `diagnostics` 与报告层直接披露，而不是让使用者猜。

**边界（明确不做）**：

| 不做 | 归属 |
|---|---|
| 联网探测接口是否存在、列名是否如预期 | M5（`tools/probe_akshare.py`） |
| 复权、填充、过滤、股票池、PIT 访问 | 数据层既有实现（`schema.py` / `store.py` / `universe.py`），取数层**不改其对外语义** |
| 评价指标与报告呈现 | M6 / M7 |

---

## 2. 接口总览

```python
# ---------------- provider.py：抽象（不依赖任何具体实现，避免循环依赖）----------------
@dataclass(frozen=True, slots=True)
class ProviderCapabilities:            # 能力声明（12 项布尔 + default_adjust + notes）
    @classmethod
    def full(cls, *, default_adjust="hfq", notes=()) -> ProviderCapabilities
    def missing_required(self, *, need_index_members=False, need_fundamentals=False,
                         need_adjustment_factors=False, need_price_limits=False) -> tuple[str, ...]
    def as_dict(self) -> dict[str, Any]

@dataclass(frozen=True, slots=True)
class Provenance:                      # 一次取数的溯源
    source: str; fetched_at: datetime   # fetched_at 恒为**带时区 UTC**（utc_now）
    cache_hit: bool; rows: int; symbols: int
    params_hash: str = ""; warnings: tuple[str, ...] = ()

@dataclass(slots=True)
class ProviderHealth:                  # 健康检查（ok=False ≠ 不可用）
    ok: bool; checked_at: datetime; latency_ms: float
    details: dict[str, Any]; errors: tuple[str, ...]

@runtime_checkable
class DataProvider(Protocol):          # 7 个 fetch_* + capabilities + health_check + name
    ...

def utc_now() -> datetime              # 数据层统一时间戳（UTC）
def filter_effective_window(frame, start, end, *, from_col="effective_from", to_col="effective_to")
def degradation_notes(caps, *, need_*) -> tuple[str, ...]

# ---------------- cache.py ----------------
class DataCache:
    def lookup(dataset, key, *, params=None, now=None) -> CacheLookup   # hit 仅当「文件+版本+参数哈希+未过期」
    def read(dataset, key) -> tuple[pd.DataFrame | None, CacheMeta | None]
    def write(dataset, key, frame, *, params=None, start=None, end=None, now=None) -> CacheMeta
    def params_hash(params) -> str        # **不含日期区间**，否则增量永远失效
    def manifest() -> list[CacheMeta]

# ---------------- ratelimit.py ----------------
class RateLimiter:                     # 令牌桶；clock/sleeper 可注入
    def acquire(n=1) -> float
def retry_call(fn, *, max_attempts=5, backoff=1.5, jitter=True, retry_on=(...),
               sleeper=..., random=..., on_retry=None,
               is_empty=None, retry_on_empty=False, on_empty=None)

# ---------------- quality.py ----------------
class QualityChecker:                  # Q1~Q12 纯函数检查
    def run_all(*, bars, symbol_meta=None, fundamentals=None, members=None,
                calendar=None, candidates=None) -> ProviderQualityReport
@dataclass(slots=True)
class ProviderQualityReport:           # ok / errors() / warnings() / add / merge / exit_code / to_markdown

# ---------------- registry.py ----------------
@dataclass(frozen=True, slots=True)
class ProviderContext:                 # 工厂入参：显式传递依赖，不用全局状态
    name: str; config: DataConfig; root: Path
    cache: DataCache | None; limiter: RateLimiter | None; client: Any | None
    index_members_path: Path | None; fundamentals_path: Path | None
ProviderFactory = Callable[[ProviderContext], DataProvider]
def register_provider(name, factory, *, overwrite=False) -> None
def available_providers() -> list[str]
def get_provider_factory(name) -> ProviderFactory          # 未知名 → ConfigError
def build_provider(config=None, *, provider=None, root=None, cache=None, limiter=None,
                   client=None, index_members_path=None, fundamentals_path=None) -> DataProvider
def provider_capabilities(name, *, config=None, strict=True) -> ProviderCapabilities

# ---------------- loader.py：适配层（M4-10）----------------
@dataclass(slots=True)
class IngestReport:
    store: DataStore; quality: ProviderQualityReport
    provenance: dict[str, Provenance]; capabilities: ProviderCapabilities
    step_status: dict[str, str]; degradation_notes: tuple[str, ...]
    manifest: list[CacheMeta]; diagnostics: dict[str, Any]
    @property
    def has_errors(self) -> bool
    @property
    def overall_status(self) -> str
def ingest_from_provider(provider, *, config, universe_config, index_codes=("000300.SH",),
                         start, end, symbols=None, fundamentals=True, industry=True) -> IngestReport

# ---------------- akshare_provider.py：端点参数构造（M5-1 提升为公开）----------------
def client_kwargs_for(ep, *, symbol=None, start=None, end=None, adjust="",
                      report_period=None, industry_name=None, **kwargs) -> dict[str, Any]
```

---

## 3. 能力矩阵（四实现 × 12 项）

`✓` = 声明可用且契约测试验证真的能取到；`✗` = 如实声明不可用（调用时返回空表/快照 + **warning**）。

| 能力 | synthetic | csv / parquet | akshare（M4-8 骨架） | 缺失后的既有口径 |
|---|---|---|---|---|
| `daily_bars` | ✓ | ✓ | ✓ | —（必需） |
| `listing_dates` | ✓ | ✓（有 `list_date` 列时） | ✓ | 由 `data.listing_date.policy` 决定：`strict` 报错 / `proxy` 代理口径 |
| `adjustment_factors` | ✓ | ✓（有 `adj_factor` 列时） | ✓（hfq ÷ 原始价派生） | `adj_factor` 按 1.0 处理 → **后复权口径不可用，收益会失真** |
| `fundamentals` | ✓ | ✓（有财务文件时） | ✓（公告日缺失的行**丢弃并计数**） | 财务因子整体不可用 |
| `industry` | ✓ | ✓（有 `industry` 列时） | ✓（**快照累积**，首次抓取日之前不可得） | RMS 行业敞口退化 |
| `index_members` | ✓（真实历史成分） | ✓（有成分文件时） | **✗ 只给当前成分** | 见 §6：股票池退化 + `index_members_approximated` |
| `suspensions` | ✓ | ✓（有 `is_suspended` 列时） | ✗ | 由 `volume<=0` 推断停牌 |
| `price_limits` | ✓ | ✓（有 `limit_up/limit_down` 列时） | ✗ | 按板块规则推算（`PriceLimitConfig`：主板 10% / 创业板·科创板 20% / 北交所 30% / 主板 ST 5%） |
| `st_flags` | ✓ | ✓（有 `is_st` 列时） | ✗ | 默认 `False`（**不假设任何标的为 ST**） |
| `delistings` | ✓ | ✗（夹具不含 `delist_date` 时） | ✗ | **幸存者偏差风险**：已退市标的可能整体缺席 |
| `market_cap` | ✓ | ✓（有 `total_mv` 列时） | ✗ | 市值类因子不可用 |
| `intraday` | ✗（**日频引擎不消费，声明 True 是虚假能力**） | ✗ | ✗ | 不适用 |

> **为什么 `index_members` 对 AKShare 声明 `False`，而 `industry` 声明 `True`**：
> `ProviderCapabilities.index_members` 的定义是**历史**成分（能回答「某日成分是谁」），
> 而候选接口只返回**当前**成分 —— 把当前成分回溯历史正是幸存者偏差的来源，
> 且它直接决定股票池，误用后果最严重，故按最严标准声明；
> `industry` 以 `effective_from/effective_to` 区间累积，缺口（首次抓取日之前）已在
> `capabilities().notes` 明确披露。两者的差别是**后果严重度**，不是数据形态。

**能力由实际列推断**（CSV/Parquet）：`adj_factor→adjustment_factors`、`is_suspended→suspensions`、
`limit_up→price_limits`、`is_st→st_flags`、`list_date→listing_dates`、`delist_date→delistings`、
`industry→industry`、`total_mv→market_cap`；成分/财务文件存在与否决定另两项。
**缺什么就如实说缺什么**，而不是乐观地宣称「都有」然后在下游悄悄失真。

---

## 4. 注册表：构建与派发

- 工厂签名 `ProviderContext → DataProvider`（依赖显式传递，不用全局状态）；
  四个内置工厂：`synthetic`（默认）/ `csv` / `parquet` / `akshare`；
- **未知名抛 `ConfigError`，绝不回退到 synthetic** —— 静默回退意味着报告里的数字来自
  另一个数据源却看不出来；
- **构建期协议闸门**：工厂返回对象必须满足 `DataProvider`（`runtime_checkable`），
  否则抛 `DataError` 并列出缺失属性（把「方法名拼错」挡在第一次取数之前）；
- **配置层与运行层分工**：`DataConfig` 只校验 `provider in KNOWN_PROVIDERS`（声明式白名单，
  不查注册表）；「这个名字是否真的有实现」由 `build_provider` 在运行层判定；
- **import 期自检**：`KNOWN_PROVIDERS - 已注册 - PENDING_PROVIDERS` 非空即拒绝导入
  （防「忘了注册某个内置源」）。分步交付期用 `PENDING_PROVIDERS` 显式声明未接入项，
  M4-8 已清空；
- **路径解析复用** `config.loader.resolve_path`（以项目根为基准）——
  避免「换个工作目录就读到另一个 `data/raw`」这种不报错的失真；
- `provider_capabilities(name, strict=)` 的 `strict` 是**可用性闸门**：
  构建失败、或 `BACKTEST_REQUIRED` 缺失即抛错；`strict=False` 返回全 `False` 下界 + `notes`
  中的失败原因（**「查不到能力」不得伪装成「天生没有这个能力」**）。

---

## 5. 缓存 / 限流 / 重试 / 降级

### 5.1 缓存（`cache.py`）

| 项 | 设计 |
|---|---|
| 目录 | `<cache.root>/<dataset>/<key>.<fmt>`（`key` 经 `_safe` 处理，防路径穿越） |
| 格式 | Parquet 优先；pyarrow 不可用时**降级为 CSV 并 warning 一次**（如实记录实际格式） |
| 参数哈希 | `params_hash` 覆盖「口径类」参数（复权方式、映射器版本、单位换算），**不含日期区间** —— 否则每次取新区间都会整库失效，增量失去意义 |
| 版本隔离 | `version` 变更后旧缓存**不读也不删**（人工清理），避免新旧 schema 混用 |
| 四态命中 | `hit` / `miss_no_file` / `miss_version` / `miss_params_hash` / `miss_stale`；**stale 仍返回 frame** 供增量 |
| TTL | `ttl_hours > 0` 才做新鲜度判定；**`<= 0` 表示「一律视为命中」**（不是「立即过期」——这是实测踩过的坑） |
| 增量 | 调用方以 stale 的「最后日期 + 1」为起点只抓增量，合并时按主键去重并断言**行数不减** |
| 原子写 | 临时文件 + 替换；`manifest.json` 记录 dataset/key/provider/version/rows/start/end/fetched_at/params_hash/fmt/path |
| 时间戳 | 统一 UTC；调用方传入的 naive `now` 按 **UTC** 解释，历史 manifest 的 naive `fetched_at` 按**本地时间**换算 |

### 5.2 限流与重试（`ratelimit.py`）

- 令牌桶：`requests_per_minute` / `burst` / `min_interval_ms`，**`clock` 与 `sleeper` 可注入**
  （测试不真睡、不依赖真实时间）；
- `retry_call`：指数退避 + 抖动，`retry_on` 之外的异常**直接抛出**；
  `on_retry` 回调用于写审计；
- **空结果默认不重试**（`retry_on_empty=False`）：空结果通常意味着「停牌/退市/当日无数据」，
  重试无意义且白耗配额，应转成 warning 进质量报告。

### 5.3 降级与失败策略（`data.failure_policy`）

| 场景 | `fallback`（默认） | `fail` |
|---|---|---|
| 有陈旧缓存 + 抓取失败 | 返回陈旧快照 + `Provenance(cache_hit=True, warnings=(…含数据截至日…))` | 抛 `DataError` |
| 无缓存可退 + 抓取失败 | **抛 `DataError`**（不凭空造数据） | 抛 `DataError` |
| 必需步骤失败（行情/元信息） | 降级（无 meta 模式 / 无成分模式）并披露 | 抛 `DataError` |
| 可选数据失败（财务/行业） | 记 `degraded` 并继续 | 记 `failed` 并继续（**可选数据不阻断整体**） |

---

## 6. `ingest_from_provider`：九步流程与步态

步骤名固定为 `INGEST_STEPS`：
`calendar → index_members → symbol_meta → bars → fundamentals → industry → normalize → validate → store`。

`step_status` 取值域 `ok | degraded | failed | skipped`，**分界是本层的核心约定**：

| 取值 | 含义 |
|---|---|
| `ok` | 该步骤按预期完成 |
| `skipped` | **只表示用户显式没请求**（如 `fundamentals=False`） |
| `degraded` | 源不支持该能力，或抓取失败但有降级路径（`fallback`） |
| `failed` | 该数据集本次确实缺失（`fail` 策略下的可选数据），或校验未通过（`strict`） |

> 把「源不支持」记成 `skipped` 会让报告层把「这个源没有财务数据」误读成「这次没要财务数据」，
> 进而误判可用性 —— 这是刻意区分的。

**关键失败路径**（`docs/18` §4.8 有完整表）：

- **上市日**：源不支持 → 不中止；由 `data.listing_date.policy` 决定（`strict` 由 store 报错 /
  `proxy` 代理口径并登记披露）；源声称支持但抓取失败 → 按 `failure_policy`；
- **财务/行业**：可选数据，失败**一律不中止**；
- **校验**：`DataQualityReport.errors` 非空时**必须影响步态**
  （`strict` → `failed` + 抛 `DataQualityError` 附报告；`non-strict` → `degraded` + 继续），
  而不是只做信息性记录；
- **指数成分近似**：`capabilities.index_members=False` 且请求了指数时
  `diagnostics["index_members_approximated"]=True`，**并写入 `quality` 报告结构**；
  `universe.fallback_to_all=True` → 股票池退化并披露；`False` → 抛错（不静默退化）。

**两个属性**：`has_errors = (not quality.ok) or step_status["validate"] == "failed"`；
`overall_status` 按 `failed > degraded > ok` 汇总。

**失败的原子性**：失败**不影响已写入的缓存**；重跑时前面步骤走缓存命中，不会重复抓取
（适配层不清理、不回滚缓存）。

**`capabilities` 的语义**：provider 声明它**理论上**具备的能力；
本次实际达成情况看 `step_status` —— 不要因为 `capabilities.daily_bars is True` 就认为
这次一定拿到了数据。

---

## 7. 契约测试清单与验收口径

`tests/contracts/provider_contract.py::ProviderContractMixin`（8 项）由四个子类继承，
运行器统一收集 → **32 条**：

| 编号 | 检查 | 覆盖的风险 |
|---|---|---|
| C1 | 能力声明 ↔ 实际行为一致（含「请求不存在的标的/指数 → 空表 + warning，不抛异常」） | 虚假能力 / 上层误判可用性 |
| C2 | `missing_required` 边界（全需求开启时缺失项 = 声明为 False 的项；声明 5 项只缺 1 项时只返回它） | 闸门误判 |
| C3 | `degradation_notes` 与 `capabilities()` **双向自洽**（缺失必披露、可用不得被说成缺失、退市缺失独立提示幸存者偏差） | 披露与实际不符 |
| C4 | 指数成分**区间相交**过滤（跨窗口生效必须保留 / 窗口早于生效日必须为空 / 无假阳性） | **幸存者偏差** |
| C5 | 字段规范（canonical 列名） | 下游解析错位 |
| C6 | 主键唯一（`bars` 按 `(date,symbol)`、`symbol_meta` 按 `symbol`、`fundamentals` 按 `(symbol,report_period,announce_date)`） | 重复行放大权重 |
| C7 | PIT 列存在与区间（`list_date` 非空、`announce_date` 非空且晚于报告期、行情落在请求区间内） | 上市日兜底 / 财务未来函数 |
| C8 | `Provenance` 契约（`rows` 描述返回数据、日历 `rows=交易日数` 且 `symbols=0`、时间戳带时区、`source` 一致） | 报告数字无法溯源 |

**验收口径**：

1. 32 条全绿（4 实现 × 8 项）；Parquet 在无 pyarrow 时**显式 skip，不计入通过**；
2. 契约**必须包含正反例**：C1 的「不存在标的」、C4 的「窗口早于生效日」都是反例；
3. 契约测试**不用 `setUp`**（零依赖运行器不调用它），夹具走 classmethod + 类级缓存；
4. 「能力声明与行为一致」允许「声明 `False` 但返回数据」的情形，但**必须有 warning** ——
   无声地返回数据同样不合格。

---

## 8. 与既有数据层的边界（不改其对外语义）

| 组件 | 取数层的用法 | 明确不改的部分 |
|---|---|---|
| `schema.normalize_bars` | 归一 + 补派生列（含 `board` 优先取数据源值、`is_suspended` 由 `volume<=0` 推断、涨跌停按板块推算） | 列规范、双价格口径 |
| `schema.validate_bars` | 第 7 步校验并保留**原始报告**在 `diagnostics["validate"]` | 校验规则与严重度 |
| `store.DataStore` | 适配层构造它（`validate=True`，store 保留自己的 `quality`），并读取其内部表做一致性测试 | PIT 访问、股票池、上市日策略、T+1 相关语义 |
| `universe.UniverseBuilder` | 仅通过 `universe_config` 影响；成分缺失按 `fallback_to_all` 决策 | 过滤规则与顺序 |
| `load_market_data` | **保留但标注 deprecated**，一致性由 `test_ingest.py` 守护（bars 严格 `assert_frame_equal`） | 行为不变 |

`ingest_from_provider` 与 `load_market_data` 的一致性测试记下一个口径陷阱：
`generate_market_data` 的「位置 → 代码」分配**取决于是否显式传入 `symbols`**
（传与不传会让两个标的的上市窗口对调，行数相同但区间不同），
故两条路径必须走**同一次生成调用**才可比。

---

## 9. 已知限制（诚实声明）

1. **AKShare 的接口名与列名是候选值**：M4-8 只交付映射骨架，真实字段名、dtype、分页与限额
   由使用者在联网环境执行 `tools/probe_akshare.py`（M5-1 已交付工具、M5 阶段 B 执行）后固化 ——
   届时只需改 `ENDPOINTS` / `MAPPERS` 两张表（单点修改）。探测报告格式见
   `docs/data/probe_report.schema.json`（**M5-1 阶段 A 从未联网**，故候选值仍未核实）；
2. **AKShare 的联网部分完全未验证**：本仓库没有发出过任何网络请求；
   所有 AKShare 用例都跑 `FakeAKShareClient` + `tests/fixtures/akshare/`（**手工构造的样例**）；
   探测工具的 `--dry-run` 同样走这套假数据，**只证明脚本自身能执行**（退出码恒 0），
   不能作为接口可用的证据；
3. **停牌/涨跌停/ST 历史/退市/市值**在 AKShare 侧尚未实现（能力声明为 `False`，口径已披露）；
4. **指数成分的历史语义依赖快照累积**：只有我们实际观测过的日期才有成分，
   首次抓取日之前不可得（已在能力 `notes` 与每次调用的 warning 中披露）；
5. **分钟级频率限制**只有配置项（`data.rate_limit`），未做日内配额管理；
6. **真实数据的单位**（财务可能以万元/亿元返回）未核实，属 M5 必须项。
