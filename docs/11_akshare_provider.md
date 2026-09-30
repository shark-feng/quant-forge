# AKShare 数据源接入设计（DataProvider 抽象 + AKShare 适配层）

> 状态：**设计稿，等确认后实现**（对应里程碑 M4 / M5）。
> 前置阅读：`10_round2_design.md` §5。
> 设计原则：**不做接口搬运**——先由数学模型反推字段需求，再挑选接口、定义映射、设计校验与降级。

---

## 1. 设计目标与约束

| 目标 | 具体要求 |
|---|---|
| 可替换 | `DataProvider` 抽象；AKShare 只是实现之一（另有 synthetic / csv / parquet） |
| PIT 正确 | 所有产出带 `announce_date` / 生效日；进入 `DataStore` 后越界查询抛 `LookaheadError` |
| 双价格体系 | 同时产出**原始价**（成交/涨跌停/费用）与**后复权因子**（信号/收益） |
| 可离线测试 | 全部单测基于脱敏 fixture；在线抓取是独立 CLI，不进测试路径 |
| 可复现 | 缓存带版本与参数哈希；同一 `(provider, params, 日期区间)` 结果可复现 |
| 不静默降级 | 任何缺失/近似（历史成分、公告日、上市日）必须写进质量报告与 `provenance.warnings` |

**环境约束（重要）**：本机无法访问外网（PyPI 亦超时）。因此本次交付为
**实现 + 离线 fixture 测试**；真实接口的可用性与字段名由 `tools/probe_akshare.py`
在联网环境探测并生成 `docs/data/akshare_capability_report.md`，实现以该报告为准。
下文所有接口名均标注为**候选**。

---

## 2. 从数学模型反推数据需求（先定规格，再选接口）

| # | 消费方 | 需要的字段 | 数学/业务依据 | 缺失或近似的后果 |
|---|---|---|---|---|
| D1 | M3 信号（MA/突破/量比） | 后复权 OHLC、成交量 | 收益序列需剔除除权跳变 | 除权日产生假突破/假死叉 |
| D2 | M2 撮合（涨跌停） | 原始 close、prev_close、板块 | 涨跌停价 = prev_close ×(1±pct) | 该拒的成交了 → 收益虚高 |
| D3 | M2 撮合（停牌） | 停牌标记 / 零成交 | 停牌不可成交 | 停牌日被撮合 |
| D4 | M1 股票池 | list_date、delist_date、ST 标记 | 上市≥60 交易日、剔除 ST/退市 | **幸存者偏差**、新股噪音 |
| D5 | M2 成本模型 | 成交额、成交量 | VWAP = amount/volume；佣金/印花税基数 | 成本失真 |
| D6 | M2 冲击成本 η(Q/ADV)^θ | **ADV（股）** | Q/ADV 是唯一自变量 | 大单成本被严重低估 |
| D7 | M6 容量估计 | ADV（元）、成交额分布 | 规模放大 → 冲击成本上升 | 容量被高估 |
| D8 | M6 基准对比 | 指数行情 + **历史成分** | 信息比率/超额收益 | 基准不可比 |
| D9 | M9 多因子 | 报告期 + **公告日** + ROE/净利润/营收/总资产/净资产 | PIT 因子构造 | **严重未来函数** |
| D10 | M9 Barra 风格 | 总市值/流通市值、换手率、波动率 | Size/Liquidity/Volatility 暴露 | 无法行业/市值中性化 |
| D11 | M5 RMS 行业暴露 | 行业分类（含历史变更） | 单行业敞口上限 | 规则失效 |
| D12 | M5 涨跌停规则核对 | 涨停/跌停价或涨跌幅 | ST(5%)、创业板/科创板(20%) 校验 | 板块规则误用 |

> **不采集**：分时/Tick（第三阶段）、逐笔委托、龙虎榜、资金流、新闻舆情——与第一阶段数学模型无关。

---

## 3. AKShare 接口清单（候选，需探测确认）

> 探测方式：`python tools/probe_akshare.py --out docs/data/akshare_capability_report.md`
> 逐接口记录：是否存在、返回列名、dtype、行数、调用耗时、是否需要 token、是否分页、失败模式。

| 数据集 | 候选接口 | 关键返回字段 | 备注 / 风险 |
|---|---|---|---|
| 交易日历 | `tool_trade_date_hist_sina` | `trade_date` | 权威性一般，可与行情日期并集交叉校验 |
| 股票列表（含上市日） | `stock_individual_info_em`、`stock_info_sh_name_code`、`stock_info_sz_name_code`、`stock_info_bj_name_code` | `code`、`name`、`上市时间` | **上市日是硬需求**（缺陷 8）；分批调用量大 |
| 日线（不复权） | `stock_zh_a_hist(symbol, period="daily", start_date, end_date, adjust="")` | `日期,开盘,收盘,最高,最低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率` | ⚠️ **成交量单位是「手」**，必须 ×100 转股 |
| 日线（后复权） | `stock_zh_a_hist(..., adjust="hfq")` | 同上 | 与不复权序列比值即复权因子；需交叉验证 |
| 复权因子（备用） | `stock_zh_a_daily(symbol="sh600000", adjust="hfq-factor")` | `date, hfq_factor` | 沪市代码前缀 `sh`/`sz` 规则 |
| 停复牌 | `stock_tfp_em(date=)` | 停牌时间/复牌时间/停牌原因 | 按日快照；也可用 `volume==0` 兜底（需标注） |
| 涨停/跌停池 | `stock_zt_pool_em(date)`、`stock_zt_pool_dtgc_em(date)` | 代码、涨跌幅、封单 | 只覆盖封板股；其余用 prev_close 规则推算 |
| 退市 | `stock_info_sh_delist`、`stock_info_sz_delist` | 代码、退市日期 | **幸存者偏差关键** |
| 曾用名（ST 历史） | `stock_info_change_name` | 代码、名称、变更日 | 由「含 ST/*ST 的名称区间」推历史 ST 标记 |
| 指数成分（当前） | `index_stock_cons_csindex(symbol)`、`index_stock_cons_weight_csindex(symbol)` | 成分券代码、权重、日期 | ⚠️ **多为当前成分**；历史成分需快照累积 |
| 指数行情 | `index_zh_a_hist(symbol="000300", period="daily")` | `日期,开盘,收盘,...` | 基准对比用 |
| 财务（含公告日） | `stock_yjbb_em(date)`（业绩报表）、`stock_financial_abstract_ths`、`stock_financial_report_sina` | `股票代码, 每股收益, 营业总收入, 净利润, 净资产收益率, 公告日期` | ⚠️ **必须有公告日**；按报告期分批抓取 |
| 分红送配 | `stock_fhps_em(date)` | 除权除息日、送转比例、现金分红 | 复权因子与现金分红入账的交叉校验 |
| 行业分类（东财） | `stock_board_industry_name_em`、`stock_board_industry_cons_em` | 行业名、成分券 | 按快照累积历史 |
| 行业分类（申万） | `sw_index_first_info`、`index_component_sw` | 一级行业、成分 | 若可用优先（Barra 行业中性常用） |
| 市值 / 估值 | `stock_a_indicator_lg(symbol)`、`stock_zh_a_spot_em` | `trade_date, total_mv, circ_mv, pe, pb` | 历史市值优先；实时快照仅能构造当日截面 |

**探测后必须固化的三件事**：
1. 每个数据集的实际列名与 dtype（映射表以探测结果为准，不照抄文档）；
2. 缺失清单：若「历史指数成分」「公告日」「上市日」任一不可得 → 在 `capabilities()` 中置 `False`，
   并触发对应降级策略（见 §6.4），**绝不静默假设**；
3. 调用代价（耗时/分页/限额），用于设定限流与缓存 TTL。

---

## 4. 字段映射表（AKShare → canonical schema）

### 4.1 日线 `bars`

| canonical 列 | AKShare 来源 | 转换规则 | 校验 |
|---|---|---|---|
| `date` | `日期` | `to_datetime().normalize()` | 唯一主键之一 |
| `symbol` | 调用参数（或 `代码`） | `normalize_symbol()` → `600000.SH` | 正则 `^\d{6}\.(SH|SZ|BJ)$` |
| `open/high/low/close` | 同名中文列 | 直接复制（**不复权口径**） | >0；`high>=max(o,c)`、`low<=min(o,c)` |
| `volume` | `成交量` | **× 100（手→股）** | ≥0；停牌日应为 0 |
| `amount` | `成交额` | 直接复制（元） | ≥0；与 `volume×vwap` 量级一致 |
| `adj_factor` | `hfq收盘 / 不复权收盘` 或 `hfq_factor` | 按 symbol 归一（首日=1） | 单调性、跳变检测（\|Δln f\|>30% 告警） |
| `prev_close` | `前收盘` 或 `close.shift(1)` | 优先接口字段 | 与 `close/涨跌幅` 交叉校验 |
| `limit_up/limit_down` | 优先涨停/跌停池；否则 `prev_close×(1±pct)` | pct 由板块+ST 决定（已有实现） | `limit_up > limit_down`；未停牌日 close 落在区间内 |
| `is_suspended` | 停复牌表 | 按日期区间展开布尔列 | 与 `volume==0` 一致性（不一致 → warning） |
| `is_st` | 曾用名区间 | 名称含 `ST`/`*ST` 的日期区间 | 与 `name` 快照一致性 |
| `list_date` | `stock_individual_info_em.上市时间` | `to_datetime()` | **缺失 → 错误（缺陷 8）** |
| `delist_date` | 退市表 | 直接复制 | 退市后不得有行情 |
| `board` | `infer_board(symbol)` | 代码前缀 | 与 ST/涨跌幅规则联动（缺陷 2 保证不被覆盖） |
| `industry` | 行业分类表 | 按生效日展开 | 与 RMS `industry_exposure` 对接 |

### 4.2 指数成分 `index_members`

| canonical | 来源 | 规则 |
|---|---|---|
| `index_code` | 调用参数 | `000300.SH` / `000905.SH` / `000852.SH` |
| `symbol` | 成分券代码 | `normalize_symbol()` |
| `effective_from` | **快照抓取日**（若接口无历史生效日） | 见 §6.4 降级 |
| `effective_to` | 下一次快照日 − 1 天 | 滚动闭合 |

### 4.3 财务 `fundamentals`

| canonical | 来源 | 规则 |
|---|---|---|
| `symbol` / `report_period` | 代码 / 报告期 | 主键 |
| `announce_date` | `公告日期` | **必填**；缺失 → 该行丢弃并计入 `dropped_rows` |
| `roe` / `net_profit` / `revenue` / `total_assets` / `total_equity` | 对应列 | 数值化；单位统一（元） |

> 单位陷阱：部分接口为「万元/亿元」，映射表必须显式记录换算系数，并由
> `test_akshare_provider.py` 用已确认单位的历史数据做数值回归。

### 4.4 行业 `industry`

`symbol, industry, effective_from, effective_to` —— 与指数成分同样的**快照累积**语义。

---

## 5. 组件与接口

```python
# src/aqs/data/provider.py
@dataclass(frozen=True, slots=True)
class ProviderCapabilities: ...        # 见 docs/10 §3.1
@dataclass(frozen=True, slots=True)
class Provenance: ...
@dataclass(slots=True)
class ProviderHealth:
    ok: bool; checked_at: datetime; latency_ms: float
    details: dict[str, Any]; errors: tuple[str, ...] = ()

class DataProvider(Protocol): ...      # 见 docs/10 §3.1

# src/aqs/data/akshare_provider.py
class AKShareProvider:
    def __init__(self, config: AKShareConfig | Mapping | None = None, *,
                 client: Any | None = None,          # 依赖注入：测试传 FakeClient
                 cache: DataCache | None = None,
                 limiter: RateLimiter | None = None,
                 session_root: str | Path | None = None) -> None
    # 内部：_fetch(fn_name, **kwargs) → rate limit → retry → cache → 标准化
    # 字段映射集中在 MAPPERS: dict[str, Callable[[DataFrame], DataFrame]]

# src/aqs/data/cache.py
class DataCache: ...                    # 见 docs/10 §3.1

# src/aqs/data/ratelimit.py
class RateLimiter: ...
def retry_call(...): ...

# src/aqs/data/registry.py
def build_provider(config) -> DataProvider      # synthetic|csv|parquet|akshare
def available_providers() -> list[str]
```

### 5.1 与现有 `DataStore` 的适配层

```python
# src/aqs/data/loader.py（扩展）
@dataclass(slots=True)
class IngestResult:
    store: DataStore
    quality: DataQualityReport
    provenance: dict[str, Provenance]
    manifest: list[CacheMeta]

def ingest_from_provider(
    provider: DataProvider,
    *,
    config: DataConfig,
    universe_config: UniverseConfig,
    index_codes: Sequence[str],
    start: date, end: date,
    symbols: Sequence[str] | None = None,     # 默认取指数成分并集
    fundamentals: bool = True,
    industry: bool = True,
) -> IngestResult
```

流程：

```
① 交易日历 → ② 指数历史成分（并集 → 候选池，天然含退市股）
→ ③ symbol_meta（list_date/delist_date/name/board/industry）
→ ④ 日线（不复权 + 后复权因子）+ 停牌 + 涨跌停 + ST 历史
→ ⑤ 财务（按公告日）+ 行业快照
→ ⑥ normalize_bars / normalize_fundamentals / normalize_index_members（复用第一轮实现）
→ ⑦ validate_bars + ProviderQualityChecks（§6）
→ ⑧ DataStore(bars, calendar, members, fundamentals, config, universe_config)
→ ⑨ 写 manifest（版本/参数哈希/行数/时间）→ 质量报告落盘
```

**双价格体系落地**：`normalize_bars` 已按 `adj_factor` 生成 `*_adj` 列；
AKShare 只负责提供**原始价 + 复权因子**，不改现有口径。

---

## 6. 缓存 / 限流 / 重试 / 降级

### 6.1 缓存

| 项 | 设计 |
|---|---|
| 目录 | `data/cache/<provider>/<dataset>/<key>.parquet`（key 例：`600000.SH`、`000300.SH`、`calendar`） |
| 格式 | Parquet（`pyarrow` 可选依赖；不可用时自动退化为 CSV 并 warning） |
| 清单 | `data/cache/manifest.json`：`dataset/key/provider/schema_version/rows/start/end/fetched_at/params_hash` |
| 版本 | `cache.version`（配置）：**schema 变更即 +1**，旧版本缓存不读、不删（可人工清理） |
| 增量 | 读取缓存的最大日期 → 只抓 `max_date+1 ~ end` → 按主键 concat 去重（保留最新） |
| 新鲜度 | `ttl_hours`（默认 24h）：未过期直接命中；`refresh=True` 强制重抓 |
| 一致性 | `params_hash` 覆盖 `adjust/复权方式/区间粒度/单位换算版本`；不一致视为未命中 |

### 6.2 限流

```python
class RateLimiter:                      # 令牌桶
    def __init__(self, *, requests_per_minute: int, burst: int, min_interval_ms: float = 0.0)
    def acquire(self, n: int = 1) -> None      # 阻塞等待，直到拿到令牌
    def stats(self) -> dict[str, float]        # 已用令牌/等待总时长/最大等待
```
默认 `requests_per_minute=300, burst=10`（保守，可按探测结果调整）；
所有等待时长汇总进 `provenance.warnings` 与运行日志。

### 6.3 重试

```
retry_call(fn, max_attempts=5, backoff=1.5, jitter=True,
           retry_on=(ConnectionError, TimeoutError, HTTPError, JSONDecodeError, KeyError))
```
- 指数退避 `backoff**n` + 抖动；
- **空结果不重试**（可能是停牌/退市/无数据），转为 warning 进入质量报告；
- 每次重试写审计日志（`category="provider", action="retry"`）；
- 连续失败达到阈值 → 触发降级（§6.4）。

### 6.4 降级与近似（必须显式）

| 场景 | 策略 | 记录位置 |
|---|---|---|
| 接口整体不可用（网络/封禁） | 按 `failure_policy.on_provider_error`：`fallback` 用本地 parquet 快照；`fail` 抛 `DataError` | `provenance.source = "fallback:parquet"`，`warnings` |
| 单标的拉取失败 | 该标的剔除并记 warning；若剔除比例 > 阈值（默认 1%）→ 升级为 error | 质量报告 `errors/warnings` |
| **历史指数成分不可得** | 只允许用**抓取日快照**构建成分表，并**禁止**把当前成分回溯到历史回测：`universe` 在这些日期上退化为 `all_listed` 并标注「成分近似」 | `capabilities.index_members=False` + 质量报告 + `diagnostics` |
| **公告日不可得** | `capabilities.fundamentals=False`，财务数据整体不可用（M9 前必须解决） | 质量报告 error |
| **上市日不可得** | 按 `data.listing_date.policy`：`strict`（默认）→ 报错；`proxy` → 显式代理并披露（缺陷 8） | 质量报告 + `diagnostics` |
| ST 历史不可得 | 用当前名称快照近似，标注「ST 历史近似」 | 质量报告 warning |
| 复权因子缺失 | 用 hfq/不复权比值推算；若两者都缺 → 该标的剔除 | 质量报告 |

---

## 7. 数据质量检查（provider 层）

在既有 `validate_bars`（8 类检查）之外，新增 provider 专属检查 `data/quality.py`：

| # | 检查 | 判定 | 级别 |
|---|---|---|---|
| Q1 | 主键重复 | `(date, symbol)` 重复数 > 0 | error |
| Q2 | OHLC 合法性 | `high<low`、`high<max(o,c)`、`low>min(o,c)`、非正值 | error |
| Q3 | 成交量单位一致性 | `amount / (volume×close)` 中位数 ∈ [0.5, 2.0]（量级校验，防「手/股」错位） | error（**新增，专治手→股陷阱**） |
| Q4 | 复权因子 | `>0`；单调跳变 \|Δln f\| > 30% 逐条告警 | error / warning |
| Q5 | 涨跌停区间 | 未停牌日 `limit_down ≤ close ≤ limit_up`；`limit_up > limit_down` | warning |
| Q6 | 停牌一致性 | `is_suspended` 与 `volume==0` 不一致的行数 | warning |
| Q7 | 上市/退市一致性 | 行情早于 `list_date` 或晚于 `delist_date` | error |
| Q8 | 交易日覆盖 | 标的存在日期缺口（非停牌日缺失）比例 > 阈值 | warning |
| Q9 | 跨表一致性 | `bars.symbol ⊆ meta.symbol`；`index_members.symbol ⊆ meta.symbol` | error |
| Q10 | 公告日 | `announce_date ≥ report_period`；缺失公告日的行数 | error |
| Q11 | 市值/财务非负 | 市值、总资产、净资产为负或为 0 的比例 | warning |
| Q12 | 幸存者偏差自检 | 候选池中退市标的数量 > 0（否则告警「可能只取到当前存续股」） | warning（**关键**） |

**输出**：`quality_report.json` + `quality_report.md`（含统计表、错误样本前 20 条、修复建议），
落盘到 `reports/data_quality/`；`strict=true` 且有 error 时中止构建。

---

## 8. 配置

```yaml
# configs/data.yaml（新增，可被 base.yaml 的 data 段整体引用）
akshare:
  request:
    requests_per_minute: 300
    burst: 10
    min_interval_ms: 50
    timeout_seconds: 30
    retry: { max_attempts: 5, backoff_seconds: 1.5, jitter: true }
  cache:
    root: data/cache
    format: parquet            # parquet | csv
    version: 1
    ttl_hours: 24
    incremental: true
    refresh: false
  scope:
    index_codes: ["000300.SH", "000905.SH", "000852.SH"]
    extra_symbols: []          # 追加自选股
    include_delisted: true     # 必须为 true 才算合格（幸存者偏差自检 Q12）
  adjust: hfq
  datasets:
    bars: true
    suspensions: true
    price_limits: true
    st_history: true
    index_members: true
    fundamentals: true
    industry: true
    market_cap: true
  failure_policy:
    on_provider_error: fallback   # fallback | fail
    fallback_provider: parquet
    max_symbol_failure_ratio: 0.01
  unit:
    volume_multiplier: 100        # 手 → 股（Q3 校验）
```

---

## 9. 测试计划

### 9.1 离线 fixture 策略（不依赖网络）

```
tests/fixtures/akshare/
├── stock_zh_a_hist_600000_raw.csv        # 中文列名、含"手"单位
├── stock_zh_a_hist_600000_hfq.csv
├── stock_individual_info_em.csv
├── stock_info_change_name.csv            # ST 历史
├── stock_info_sh_delist.csv
├── stock_tfp_em_20220301.csv
├── index_stock_cons_csindex_000300.csv
├── index_zh_a_hist_000300.csv
├── stock_yjbb_em_2022q1.csv              # 含"公告日期"
├── stock_board_industry_cons_em.csv
└── README.md                             # 每个样例的来源、抓取日、脱敏说明
```

```python
class FakeAKShareClient:            # tests/ 内部
    """按函数名返回对应 fixture；可注入延迟/异常以测试限流与重试。"""
```

### 9.2 用例清单

| 分组 | 用例 | 断言要点 |
|---|---|---|
| 契约（4 provider 共用） | `test_capabilities_declared`、`test_schema_conformant`、`test_primary_key_unique`、`test_pit_columns_present`、`test_provenance_returned` | canonical schema、主键唯一、`list_date`/`announce_date` 存在 |
| 映射 | `test_bars_mapping_units`（**手→股**）、`test_hfq_factor_normalized`、`test_symbol_normalisation`、`test_board_inference`、`test_st_history_intervals`、`test_suspension_intervals`、`test_delist_propagation` | 数值与区间正确 |
| PIT | `test_announce_date_not_report_period`、`test_no_future_rows_in_bars`、`test_index_membership_effective_dates` | 越界不可见 |
| 缓存 | `test_cache_roundtrip`、`test_incremental_merge`、`test_ttl_expiry`、`test_version_bump_ignores_old`、`test_params_hash_mismatch` | 命中/未命中、去重、版本隔离 |
| 限流 | `test_limiter_blocks_above_rate`、`test_limiter_burst`、`test_limiter_stats` | 调用间隔 ≥ 1/rps |
| 重试/降级 | `test_retry_succeeds_after_transient_error`、`test_retry_exhausted_raises`、`test_empty_result_no_retry`、`test_fallback_to_parquet`、`test_symbol_failure_ratio_escalation` | 次数、退避、升级为 error |
| 质量 | `test_q3_volume_unit_guard`、`test_q4_adj_factor_jump`、`test_q9_cross_table`、`test_q12_survivorship_selfcheck`、`test_quality_report_written` | 报告字段与级别 |
| 适配 | `test_ingest_builds_store`、`test_ingest_quality_gate_strict`、`test_dual_price_system_preserved`、`test_universe_contains_delisted` | `DataStore` 可用、双价格口径 |
| CLI | `test_fetch_data_dry_run`、`test_probe_report_schema` | 不联网也能跑 `--dry-run` |

预计 **~52 个新用例**。

### 9.3 在线验证（由使用者在联网环境执行，非 CI）

```powershell
pip install akshare
python tools\probe_akshare.py --out docs\data\akshare_capability_report.md
python tools\fetch_data.py --provider akshare --index 000300.SH,000905.SH,000852.SH `
       --start 20180101 --end 20231231 --quality-report reports\data_quality
```

验收：`quality_report.json` 中 `errors == 0`；候选池含退市股（Q12 通过）；
随机抽 3 个标的核对成交量单位与复权因子（人工核对一次，写入报告附录）。

---

## 10. 已知风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| AKShare 接口不稳定/改名 | 抓取失败 | 探测脚本 + 能力声明 + 降级到本地快照；映射集中在 `MAPPERS` 便于单点修改 |
| 反爬/限速封禁 | 批量抓取中断 | 保守限流 + 断点续抓（缓存增量）+ 失败比例阈值 |
| 历史指数成分不可得 | 指数增强类策略的股票池不可信 | 明确降级并禁止当前成分回溯；报告中标注 |
| 公告日字段缺失 | M9 因子无法 PIT | 置 `capabilities.fundamentals=False`；M9 前必须解决 |
| 数据体量大（全A股多年） | 磁盘/耗时 | 默认只取三大指数成分并集（约 1800 只）；支持 `--symbols` 限额 |
| 单位/精度陷阱 | 成本与收益系统性偏差 | Q3 量级校验 + 数值回归用例 + 人工抽检 |
| 与 AKShare 的许可证/署名 | 合规 | `NOTICE` 中署名 AKShare（MIT）与数据来源声明；不修改其代码、不打包其数据 |

---

## 11. 交付物清单（M4 / M5）

1. `src/aqs/data/provider.py`（抽象 + 能力 + 溯源 + 健康检查）
2. `src/aqs/data/akshare_provider.py`（映射 / 限流 / 重试 / 降级 / 缓存接入）
3. `src/aqs/data/cache.py`、`ratelimit.py`、`registry.py`、`quality.py`
4. `src/aqs/data/loader.py::ingest_from_provider`（适配层）
5. `src/aqs/data/synthetic.py` 补齐 `list_date` / `industry`（缺陷 8/12 前置）
6. `tools/probe_akshare.py`、`tools/fetch_data.py`
7. `tests/contracts/provider_contract.py` + `tests/fixtures/akshare/*` + 9.2 节用例
8. `configs/data.yaml` + `config/loader.py` 读取
9. 文档：本文件 + `docs/data/akshare_capability_report.md`（联网后生成）
