# 第二轮设计文档（R2：缺陷修复 · 工程化 · 数据源 · 评价与报告）

> 状态：**设计已定稿，等确认后按里程碑逐个实现**（本轮不写实现代码）。
> 前置：第一轮已交付 M0~M5，398 个用例全绿。
> 目标仓库：<https://github.com/shark-feng/quant-forge>（包名 `quant-forge`，import 名保持 `aqs`）。
> 阅读顺序：本文档 → `11_akshare_provider.md`（数据源） → `12_framework_matrix.md`（框架选型） → 各模块设计文档。

---

## 0. 验收标准 → 里程碑映射

| 合同验收项 | 对应模块 | 里程碑 | 交付物 |
|---|---|---|---|
| 第一轮 12 条缺陷全部修复 + 回归测试 | R2-01 缺陷修复 | **M1** | 修复后的代码 + 回归用例 + `docs/CHANGELOG.md` |
| GitHub 仓库上传成功、README 完整、.gitignore 正确 | R2-02 工程化 | **M2** | `LICENSE`/`NOTICE`/`README.md`/`.gitignore`/`pyproject.toml` + 上传 runbook |
| 框架选型矩阵完成、主引擎边界清晰 | R2-03 选型矩阵 | **M3** | `docs/12_framework_matrix.md` + 决策记录 |
| AKShare 数据源接入、质量报告通过 | R2-04 DataProvider + R2-05 AKShare | **M4 / M5** | `data/provider.py`、`data/akshare_provider.py`、`data/cache.py`、`tools/fetch_data.py`、质量报告 |
| M6 评价层交付 | R2-06 metrics | **M6** | `src/aqs/metrics/` + 测试 |
| M7 报告层交付、第一份完整回测报告 | R2-07 report | **M7** | `src/aqs/report/` + `reports/<run>/report.html` |
| 偏差与压力测试套件完成 | R2-08 测试套件 | **M8** | `tests/test_bias_*.py`、`tests/test_stress_*.py`、`docs/15_testing_suite.md` |
| 第二阶段设计文档完成 | R2-09 阶段规划 | **M9** | `docs/16_phase2_design.md` |
| 全部测试通过、无未来函数、T+1 正确、涨跌停正确、成本正确 | 贯穿全过程 | 每个里程碑 | 质量门禁（§7.4） |

---

## 1. 目录结构变化

```
D:\Quantify\  （= GitHub 仓库 quant-forge）
├── LICENSE                          🆕 MIT（建议，见 §8.2）
├── NOTICE                           🆕 数据来源与合规声明
├── README.md                        ✏️ 重写：简介/合规/结构/安装/快速开始/AKShare 数据准备/示例/测试/进度/免责
├── CHANGELOG.md                     🆕 每轮变更记录（含 12 条缺陷修复）
├── .gitignore                       ✏️ 补 data/cache、reports、.tmp_tests、密钥文件
├── pyproject.toml                   ✏️ 依赖补全 + 可选依赖分组 + 包名 quant-forge
├── requirements.txt                 ✏️ 与 pyproject 对齐（核心 / science / data / dev 四组）
├── configs/
│   ├── base.yaml                    ✏️ 新增 akshare / metrics / report 段
│   ├── risk.yaml                    ✏️ audit_log 语义明确
│   ├── costs.yaml                   （不变）
│   ├── data.yaml                    🆕 数据源与缓存/限流配置（可被 base.yaml 引用）
│   └── strategies/*.yaml            ✏️ volume 策略新增 volume_min_base
├── docs/
│   ├── 00~06 *_*.md                 ✏️ 一致性修正（§4.12）
│   ├── 10_round2_design.md          🆕 本文档
│   ├── 11_akshare_provider.md       🆕 数据源设计
│   ├── 12_framework_matrix.md       🆕 框架选型矩阵
│   ├── 13_metrics.md                🆕 M6 设计（含接口/伪代码/测试用例）
│   ├── 14_report.md                 🆕 M7 设计
│   ├── 15_testing_suite.md          🆕 偏差与压力测试套件设计
│   ├── 16_phase2_design.md          🆕 第二阶段设计
│   └── 17_github_upload.md          🆕 上传 runbook（本文档 §8 的展开版）
├── src/aqs/
│   ├── data/
│   │   ├── provider.py              🆕 DataProvider 抽象 + 能力声明 + 溯源信息
│   │   ├── akshare_provider.py      🆕 AKShare 实现（字段映射/限流/重试/降级）
│   │   ├── cache.py                 🆕 Parquet 缓存 + 版本 + 增量更新 + 清单
│   │   ├── ratelimit.py             🆕 令牌桶 + 指数退避重试
│   │   ├── quality.py               🆕 质量检查器（从 schema.py 拆出，可出报告/退出码）
│   │   ├── registry.py              🆕 provider 注册表（synthetic/csv/parquet/akshare）
│   │   ├── loader.py                ✏️ 适配 provider 协议
│   │   ├── schema.py                ✏️ 缺陷 2/8 修复
│   │   ├── store.py                 ✏️ 缺陷 8 修复（list_date 策略）
│   │   └── synthetic.py             ✏️ 缺陷 12（代码唯一性）+ 提供 list_date/industry
│   ├── metrics/                     🆕 M6（11 个模块，见 §5.4）
│   ├── report/                      🆕 M7（6 个模块，见 §5.5）
│   ├── risk/                        ✏️ 缺陷 1/6/7
│   ├── engine/                      ✏️ 缺陷 3/4/6/7
│   ├── config/                      ✏️ 缺陷 5 + 新配置段
│   └── strategy/                    ✏️ 缺陷 9
├── tests/
│   ├── fixtures/akshare/            🆕 离线样例（接口返回脱敏样本，供离线测试）
│   ├── contracts/
│   │   └── provider_contract.py     🆕 各 provider 共用的契约测试基类
│   ├── test_defect_*.py             🆕 12 条缺陷的回归用例（一文件一条，见 §4）
│   ├── test_provider_*.py           🆕 provider 契约/映射/缓存/限流
│   ├── test_metrics_*.py            🆕 M6（8 个文件）
│   ├── test_report_*.py             🆕 M7（3 个文件）
│   ├── test_bias_*.py               🆕 未来函数/幸存者/过拟合
│   ├── test_stress_*.py             🆕 危机窗口/极端波动/流动性枯竭/成本敏感性
│   ├── test_docs_consistency.py     🆕 缺陷 11：文档与代码一致性守护
│   └── test_packaging.py            🆕 依赖声明/版本号/入口点
├── tools/
│   ├── fetch_data.py                🆕 在线数据准备 CLI（下载→缓存→质量报告）
│   ├── probe_akshare.py             🆕 接口能力探测（把探测结果固化为字段映射依据）
│   ├── run_report.py                🆕 一键：跑回测 → 生成报告 → 打印验收摘要
│   └── upload_github.ps1            🆕 上传脚本（§8.4）
└── reports/                         （gitignore，仅保留 .gitkeep）
```

**不新增**：不引入 matplotlib/plotly（报告用内嵌 SVG，避免重依赖）；不引入 web 框架（HTML 单文件输出）。

---

## 2. 模块清单与依赖

```
                    ┌────────────────────────────────────────────┐
                    │ R2-01 缺陷修复（M1）                        │
                    │ 12 条：risk/data/engine/config/strategy 补丁 │
                    └───────────────┬────────────────────────────┘
                                    ▼
   ┌────────────────────┐   ┌───────────────────────┐   ┌──────────────────────┐
   │ R2-03 框架选型矩阵 │   │ R2-02 工程化/GitHub    │   │ R2-04 DataProvider   │
   │ （文档决策，M3）    │   │ LICENSE/README/CI 脚本 │   │ 抽象 + 缓存 + 限流   │
   └────────────────────┘   └───────────────────────┘   └──────────┬───────────┘
                                                                    ▼
                                                        ┌──────────────────────┐
                                                        │ R2-05 AKShare 适配    │
                                                        │ 字段映射/质量报告      │
                                                        └──────────┬───────────┘
                                                                   ▼
   ┌────────────────────┐   ┌───────────────────────┐   ┌──────────────────────┐
   │ R2-08 偏差与压力    │◀──│ R2-06 metrics（M6）    │◀──│ 真实/合成数据 + 引擎  │
   │ 测试套件（M8）      │   │ 指标/基准/暴露/归因    │   └──────────────────────┘
   └────────────────────┘   └──────────┬────────────┘
                                       ▼
                            ┌───────────────────────┐
                            │ R2-07 report（M7）     │
                            │ HTML/Markdown/验收报告 │
                            └───────────────────────┘
                                       ▼
                            ┌───────────────────────┐
                            │ R2-09 第二阶段设计(M9) │
                            └───────────────────────┘
```

| 编号 | 模块 | 依赖 | 交付物 | 验收 |
|---|---|---|---|---|
| R2-01 | 缺陷修复 12 条 | — | 补丁 + 回归用例 | 12 条逐条有对应测试且全绿 |
| R2-02 | 工程化与 GitHub | R2-01 | LICENSE/NOTICE/README/.gitignore/pyproject/tools | 仓库可克隆安装、README 完整、无敏感文件 |
| R2-03 | 框架选型矩阵 | — | `docs/12` | 10 个维度结论 + 主引擎边界 + 许可证评估 |
| R2-04 | DataProvider 抽象 | R2-01 | `data/provider.py`、`cache.py`、`ratelimit.py`、`registry.py` | 契约测试对 4 个 provider 全通过 |
| R2-05 | AKShare 适配 | R2-04 | `data/akshare_provider.py`、`tools/probe_akshare.py`、`tools/fetch_data.py` | 离线 fixture 测试全绿 + 质量报告无 error |
| R2-06 | M6 metrics | R2-01 | `src/aqs/metrics/` 11 模块 | 指标手算一致性、基准对比、归因可复现 |
| R2-07 | M7 report | R2-06 | `src/aqs/report/` 6 模块 | 生成 HTML/Markdown，含验收报告章节 |
| R2-08 | 偏差与压力套件 | R2-06 | `tests/test_bias_*`、`test_stress_*` | 三类偏差 + 三段危机 + 成本敏感性全绿 |
| R2-09 | 第二阶段设计 | R2-06 | `docs/16` | 模型→模块映射表 + 优先级 + 验收标准 |

---

## 3. 接口定义（新增模块）

### 3.1 数据源抽象（R2-04，详见 `docs/11_akshare_provider.md`）

```python
@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """数据源能力声明：上层据此决定校验强度与降级路径。"""
    daily_bars: bool; adjustment_factors: bool; suspensions: bool; price_limits: bool
    st_flags: bool; listing_dates: bool; delistings: bool; index_members: bool
    fundamentals: bool;      # 必须含 announce_date
    industry: bool; market_cap: bool; intraday: bool
    default_adjust: str = "none"
    notes: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class Provenance:
    """数据溯源：来源、抓取时间、是否命中缓存、行数、告警。"""
    source: str; fetched_at: datetime; cache_hit: bool
    rows: int; symbols: int; warnings: tuple[str, ...] = ()

class DataProvider(Protocol):
    name: str
    def capabilities(self) -> ProviderCapabilities: ...
    def health_check(self) -> ProviderHealth: ...
    def fetch_bars(self, symbols, start, end, *, adjust="none") -> tuple[DataFrame, Provenance]: ...
    def fetch_symbol_meta(self, symbols) -> tuple[DataFrame, Provenance]: ...   # list_date/delist_date/name/board/industry/total_mv
    def fetch_index_members(self, index_code, start, end) -> tuple[DataFrame, Provenance]: ...
    def fetch_fundamentals(self, symbols, start, end) -> tuple[DataFrame, Provenance]: ...
    def fetch_trading_calendar(self, start, end) -> tuple[list[date], Provenance]: ...
    def fetch_industry(self, symbols) -> tuple[DataFrame, Provenance]: ...
```

```python
class DataCache:
    def __init__(self, root, *, version: int, fmt: str = "parquet") -> None
    def read(self, dataset: str, key: str) -> tuple[DataFrame | None, CacheMeta | None]
    def write(self, dataset: str, key: str, frame: DataFrame, meta: CacheMeta) -> CacheMeta
    def is_fresh(self, meta: CacheMeta, *, ttl_hours: float, now: datetime) -> bool
    def merge_incremental(self, cached: DataFrame, fresh: DataFrame, *, key: tuple[str, ...]) -> DataFrame
    def manifest(self) -> list[CacheMeta]           # 数据版本清单（可写入 reports/data_manifest.json）

class RateLimiter:
    def __init__(self, *, requests_per_minute: int, burst: int) -> None
    def acquire(self, n: int = 1) -> None           # 阻塞式令牌桶
    def try_acquire(self) -> bool
    def wait_time(self) -> float

def retry_call(fn, *, max_attempts: int, backoff: float, jitter: bool,
               retry_on: tuple[type[Exception], ...], on_retry: Callable | None = None) -> Any
```

### 3.2 M6 评价层（R2-06）

```python
@dataclass(frozen=True, slots=True)
class MetricsConfig:
    risk_free_rate: float = 0.02          # 年化无风险利率
    trading_days_per_year: int = 252
    confidence_levels: tuple[float, ...] = (0.95, 0.99)
    benchmarks: tuple[str, ...] = ("000300.SH", "000905.SH", "000852.SH")
    capacity_participation: tuple[float, ...] = (0.05, 0.10, 0.20)
    capacity_target_impact: float = 0.02  # 可接受冲击成本上限
    montecarlo_paths: int = 1000
    montecarlo_block: int = 20
    seed: int = 20240101

@dataclass(frozen=True, slots=True)
class MetricsReport:
    """所有指标的一次性汇总（纯数据，可序列化为 JSON）。"""
    period: dict[str, Any]                 # 起止、交易日数
    returns: dict[str, float]              # 累计/年化/波动/日胜率/best/worst
    risk: dict[str, float]                 # 夏普/Sortino/Calmar/最大回撤/回撤持续期/VaR/ES
    relative: dict[str, float]             # 信息比率/超额年化/跟踪误差/Alpha/Beta
    trading: dict[str, float]              # 换手率/交易笔数/平均持有天数/成本后收益/成本占比
    capacity: dict[str, Any]               # 各参与率下的容量估计与冲击成本
    exposures: dict[str, Any]              # 行业/市值/风格暴露
    attribution: dict[str, Any]            # 成本/信号/因子归因
    benchmark: dict[str, Any]              # 与三基准对比
    warnings: tuple[str, ...] = ()

class MetricsEngine:
    def __init__(self, config: MetricsConfig | Mapping | None = None) -> None
    def compute(self, result: BacktestResult, *, store: DataStore,
                benchmarks: Mapping[str, pd.Series] | None = None) -> MetricsReport: ...
    def sensitivity(self, runner: Callable[[BaseConfig], BacktestResult],
                    scenarios: Sequence[CostScenario]) -> SensitivityReport: ...
    def split(self, *, in_sample: tuple[date, date], out_sample: tuple[date, date]) -> SplitReport: ...
    def rolling(self, *, window: int, step: int, fn: Callable[[date, date], BacktestResult]) -> RollingReport: ...
    def montecarlo(self, returns: Sequence[float], *, paths: int, block: int, seed: int) -> MCReport: ...
```

> **边界（合同禁止项）**：`MetricsEngine` 只消费 `BacktestResult`，**不得修改回测路径**；
> 所有需要重跑的场景（敏感性/样本外/滚动）都通过接收一个 `runner` 回调实现，
> 由调用方决定如何重跑，指标层不接触引擎内部状态。

### 3.3 M7 报告层（R2-07）

```python
@dataclass(frozen=True, slots=True)
class ReportConfig:
    formats: tuple[str, ...] = ("html", "markdown")
    output_dir: str = "reports"
    language: str = "zh"
    include_charts: bool = True
    max_trades_in_table: int = 200
    sections: tuple[str, ...] = ("summary", "equity", "drawdown", "trades", "positions",
                                 "exposure", "attribution", "metrics", "rms",
                                 "engine_acceptance", "cost", "data_quality")

class ReportBuilder:
    def __init__(self, config: ReportConfig | Mapping | None = None) -> None
    def build(self, result: BacktestResult, metrics: MetricsReport, *,
              store: DataStore | None = None, risk: RiskEngine | None = None,
              output_dir: str | Path | None = None) -> ReportArtifacts: ...

@dataclass(frozen=True, slots=True)
class ReportArtifacts:
    html: Path | None; markdown: Path | None; json: Path
    charts: dict[str, Path]; sections: tuple[str, ...]

class AcceptanceReport:      # 引擎与 RMS 验收
    @staticmethod
    def engine_acceptance(result) -> dict[str, Any]    # 事件顺序/T+1/涨跌停/停牌/成本
    @staticmethod
    def rms_acceptance(risk) -> dict[str, Any]         # 拒单率/误杀率/触发延迟
```

### 3.4 偏差与压力测试套件（R2-08）

不新增生产代码，只新增**测试夹具与断言工具** `tests/testkit/bias.py`：

```python
def assert_no_lookahead(store, strategy, days) -> None
    """对每个交易日断言：策略可见的最大数据日期 ≤ 当日。"""
def assert_t_plus_one(result, store) -> None
    """断言每笔成交的交易日 = 其信号日的下一个交易日（允许顺延）。"""
def assert_price_limit_respected(result, store) -> None
    """断言不存在「涨停买入成交」「跌停卖出成交」。"""
def assert_survivorship_universe(store, day, expected_delisted) -> None
def run_crisis_scenario(name: str, *, strategy, portfolio) -> BacktestResult
    """用 CRISIS_WINDOWS 生成对应极端行情并回测。"""
def cost_sensitivity(scenarios, runner) -> pd.DataFrame
```

### 3.5 第二阶段模块占位（R2-09 只出设计，不实现）

```python
# src/aqs/factors/      M9 多因子（FF3/FF5、Barra 风格、行业/市值中性）
# src/aqs/optimizer/    M10 组合优化（均值-方差、约束、换手惩罚）
# src/aqs/volatility/   M11 GARCH(1,1) 与波动率预测
# src/aqs/statarb/      M12 协整配对（ADF、z-score、价差回归）
# src/aqs/riskfactor/   M13 因子风险模型（因子暴露、协方差、VaR/ES 分解）
```

---

## 4. 第一轮缺陷修复设计（12 条，逐条）

> 每条包含：**根因 → 修复方案 → 接口变化 → 回归测试 → 风险**。
> 回归用例统一放在 `tests/test_defect_*.py`，命名与缺陷号一一对应。

### 缺陷 1：`RiskTrigger.latency_days` 恒为 0

- **根因**：`risk/base.py::_record_trigger` 中，`pause/force_close` 分支把 `acted_on` 置为 `None`，
  紧随其后的 `if action in ("pause","force_close") and acted_on is not None` 永远不成立 → `latency_days` 恒为 0。
- **修复方案**：
  ```
  订单级动作（reject / reduce）：acted_on = 触发日， latency_days = 0
  控制类动作（pause / force_close）：acted_on = 触发日的**下一个交易日**，latency_days = 1
  ```
  交易日由引擎注入的解析器给出（不能用「自然日 +1」，否则跨周末/节假日会算错）：
  ```python
  class RiskEngine:
      next_trading_day_of: Callable[[date], date | None] | None = None   # 🆕
      def bind_calendar(self, resolver: Callable[[date], date | None]) -> None
      def _resolve_acted_on(self, day: date) -> tuple[date, int, bool]
          # 返回 (acted_on, latency_days, approx)
          # 有日历解析器 → (下一交易日, 1, False)
          # 无解析器      → (day + 1 自然日, 1, True)  # 标记为近似，并在汇总中暴露
  ```
  `RiskTrigger` 新增字段 `latency_approx: bool = False`；
  `LatencyReport` 新增 `n_approx: int`、`by_rule` 保持。
  引擎在 `BacktestEngine._build_risk_engine` 中 `engine.bind_calendar(store.calendar.next_trading_day)`。
- **接口变化**：`RiskTrigger`（+`latency_approx`）、`RiskEngine.bind_calendar`、`LatencyReport`（+`n_approx`）。
- **回归测试**（`tests/test_defect_01_risk_latency.py`）：
  1. 订单级 reject → `acted_on == day`、`latency_days == 0`；
  2. pause → `acted_on == 下一交易日`、`latency_days == 1`；
  3. force_close → 同上；
  4. **跨周末**：周五触发 → `acted_on` 为下周一，`latency_days == 1`；
  5. 未绑定日历时 → `latency_approx is True` 且 `n_approx == 1`；
  6. 端到端：回测里触发 pause 后 `diagnostics["risk"]["latency"].mean_days == 1.0`。
- **风险**：`acted_on` 是**约定语义**（下一个交易日生效）而非「实际成交日」；
  若顺延卖出导致实际执行更晚，延迟会被低估。缓解：在 `docs/06` §4 明确口径，
  并在报告层同时输出「实际强平完成日」与约定执行日（M7 的 engine_acceptance 章节）。

### 缺陷 2：`board` 列被推断值覆盖

- **根因**：`data/schema.py::normalize_bars` 中 `if "board" not in columns or out["board"].isna().any():` 一旦有任一 NaN
  就用 `infer_board` **整体覆盖**，数据源自带的板块信息被丢弃；其后还有一段冗余 `fillna`。
- **修复方案**：
  ```python
  if "board" in out.columns:
      provided = out["board"].astype(str).str.strip().str.lower()
      valid = set(b.value for b in Board)
      invalid = ~provided.isin(valid)          # 含 NaN 转成的 "nan"
      out["board"] = provided.where(~invalid, pd.NA)
      inferred = out["symbol"].map(_board_map(out["symbol"].unique()))
      out["board"] = out["board"].fillna(inferred)     # 仅补缺失/非法
      if invalid.any(): logger.warning("board 列有 %d 行非法/缺失，已用代码前缀推断", int(invalid.sum()))
  else:
      out["board"] = out["symbol"].map(_board_map(out["symbol"].unique()))
  ```
  删除死代码 `out["board"].fillna(out["symbol"].map({...}))`。
  `validate_bars` 新增检查：`board` 值不在 `Board` 枚举内 → **error**。
- **接口变化**：无（`BARS_COLUMNS` 中 `board` 仍是可选列）；`DataQualityReport.errors` 新增一类。
- **回归测试**（`tests/test_defect_02_board_column.py`）：
  1. 数据源给 `600000.SH → gem` → 保持 `gem`（不被推断成 main）；
  2. 部分行 NaN → 仅这些行被推断；
  3. 无 `board` 列 → 全量推断（沿用原行为）；
  4. 非法值 `"x"` → 该行推断 + warning + `validate_bars` 报 error；
  5. 涨跌停幅度随数据源 board 变化（主板 10% vs 创业板 20%）——验证 board 真正参与计算。

### 缺陷 3：DAY 订单未成交被标记为 REJECTED

- **根因**：`engine/broker.py::_handle_result` 的兜底分支
  `if result.can_retry and order.time_in_force is GTC: … defer … else: order.mark_rejected(...)`，
  使 `TimeInForce.DAY` 的订单在当日无法成交时被当成「拒单」。
- **修复方案**：明确两种有效期的语义，并让统计口径与之对齐：
  ```
  DAY：仅在其到期日（submit_date）有效；当日未成交 → EXPIRED（不计入 rejected）
  GTC：顺延；deferred_days > max_defer_days → EXPIRED
  不可顺延的硬拒绝（退市/不足一手/超持仓/限价未触及…）→ REJECTED
  ```
  ```python
  if result.filled: ...
  elif result.can_retry and order.time_in_force is TimeInForce.GTC:
      self.stats.deferred_events += 1; still = order.defer(reason)
      if not still: self.stats.expired += (1 if partially else 0) + 1; _retire(EXPIRED)
  elif result.can_retry and order.time_in_force is TimeInForce.DAY:
      self.stats.expired += 1; order.expire(reason)      # 🆕 Order.expire()
      self.audit.log("order", "expired", reason=f"day_expired:{reason.value}")
  else:
      self.stats.rejected += 1; order.mark_rejected(reason)
  ```
  `Order` 新增方法 `expire(reason: RejectReason = NONE) -> None`（置 EXPIRED，保留 `reject_reason` 作为原因记录）。
  `BrokerStats` 新增 `expired_day: int`（区分「DAY 过期」与「GTC 顺延超限」）。
- **接口变化**：`Order.expire`、`BrokerStats.expired_day`。
- **回归测试**（`tests/test_defect_03_order_tif.py`）：
  1. DAY 订单遇涨停 → `status == EXPIRED`、`stats.expired_day == 1`、`stats.rejected == 0`；
  2. GTC 订单遇涨停 → `status == SUBMITTED`、`deferred_days == 1`（不 expired）；
  3. GTC 顺延超限 → `EXPIRED` 且 `expired_day == 0`；
  4. DAY 部分成交后遇停牌 → 已成交部分保留、剩余 `EXPIRED`；
  5. DAY + 退市（不可顺延）→ 仍为 `REJECTED`；
  6. 端到端：`orders.csv` 中的 status 分布与 `diagnostics` 一致。

### 缺陷 4：T+1 冻结下等比例减仓不达标

- **根因**：`engine/backtest.py::_apply_risk_controls` 只对 `account.sellable(symbol) > 0` 的标的生成减仓单；
  当日买入（T+1 冻结）的标的被跳过，组合敞口高于 `target_exposure_scale`，且**没有任何记录**。
- **修复方案**（跨日补偿 + 显式记录，两者都做；**实现落在新模块 `engine/control.py`**）：
  1. 新增 `engine/control.py::ExposureControlState`（跨日维持）：
     ```python
     @dataclass(slots=True)
     class ExposureControlState:
         target_scale: float = 1.0          # RMS 给出的目标敞口
         pending: dict[str, float] = {}     # 因 T+1/停牌未能减仓的**股数**
         deferred_days: dict[str, int] = {} # 每个标的已顺延的交易日数
         deferred_events: int = 0           # 累计顺延事件数
         unmet_days: int = 0                # 连续未达标的交易日数
         max_unmet_days: int = 0
         history: list[dict] = []           # 每日目标 vs 实现敞口（进报告）
     ```
  2. `plan_exposure_reduction(account, snapshot, *, scale, lot_size, pending, tolerance)`：
     每次按「当前敞口 → 目标敞口」**重新计算**应减数量；`pending` 只用于
     **优先处理顺序**与欠账记录（**不与重算数量相加**，否则会过度卖出）；
  3. 当日因不可卖而未完成的部分记入 `pending` 并累计 `deferred_days`/`deferred_events`；
  4. `unmet_days` 连续超过 `engine.exposure_control.unmet_warning_days`（默认 5）→
     写 warning 日志 + RMS 审计事件 `exposure_unmet_warning`；
  5. `diagnostics.risk.exposure_control` / `exposure_control_history` 输出全过程；
  6. 文档：`docs/06_risk_rms.md` §4 补充「减仓可达性与偏差」口径说明。
- **接口变化**：新增模块 `engine/control.py`；`EngineConfig.exposure_control`（新配置段）；
  诊断新增两个字段。**对外签名不变**。
- **回归测试**（`tests/test_defect_04_exposure_control.py`，13 条）：
  1. T+1 冻结 → 当日无卖单、`pending` 记录、`reasons=t1_locked`；
  2. 次日解锁 → 按当日敞口重算并补减，`unmet` 清空；
  3. `pending` 标的被优先处理；
  4. 停牌 → `reasons=suspended`；
  5. 达标/容差内 → 不产生卖单；
  6. 目标 0 → 清仓且 `tag=risk_close`；
  7. 端到端：`deferred_events ≥ 2`、`history` 含 `t1_locked`、最终敞口 < 0.55；
  8. `scale=1.0` → `pending` 恒为空；
  9. 诊断字段完整性。

### 缺陷 5：`PROJECT_ROOT` 硬编码

- **根因**：`config/loader.py::PROJECT_ROOT = Path(__file__).resolve().parents[3]`，
  一旦包被安装到 site-packages（`pip install .`）就指向错误的目录。
- **修复方案**：
  ```python
  ENV_PROJECT_ROOT = "AQS_PROJECT_ROOT"

  def discover_project_root(start: Path, *, max_levels: int = 6) -> Path | None:
      """自 start 向上查找含 pyproject.toml（或 .git）的目录。"""
  def resolve_project_root(*, start: Path | None = None) -> Path:
      # 1) 环境变量 AQS_PROJECT_ROOT（必须是存在的目录；缺 pyproject.toml 时 warning）
      # 2) discover_project_root(包目录) → 命中即用
      # 3) fallback：Path(__file__).resolve().parents[3]（保持兼容）
      # 结果缓存；提供 clear_project_root_cache() 供测试使用
  PROJECT_ROOT: Path = resolve_project_root()
  ```
  `resolve_path()` 改用 `resolve_project_root()`；新增 `AQS_DATA_DIR` 覆盖缓存目录（可选）。
- **接口变化**：新增 `discover_project_root`、`resolve_project_root`、`clear_project_root_cache`；
  `PROJECT_ROOT` 从常量变为解析结果（仍以模块级名称导出，兼容既有引用）。
- **回归测试**（`tests/test_defect_05_project_root.py`）：
  1. 设置 `AQS_PROJECT_ROOT` → 返回值等于该路径（monkeypatch + 清缓存）；
  2. 未设置时 → 命中真实仓库根（含 pyproject.toml）；
  3. 在临时目录树 `a/b/c/pyproject.toml` 下从 `a/b/c/d/e` 向上发现 → 返回 `a/b/c`；
  4. 都不满足 → fallback 到 `parents[3]` 并不抛异常；
  5. 环境变量指向不存在目录 → warning + 继续下一策略。

### 缺陷 6：RMS `audit_log` 未生效

- **根因**：`engine/backtest.py::_build_risk_engine` 显式 `dataclasses.replace(cfg, audit_log=None)`，
  把 `configs/risk.yaml` 里的 `audit_log: reports/rms_audit.jsonl` 抹掉了。
- **修复方案**：
  1. 默认遵循配置；新增构造参数 `risk_audit_log: str | Path | None | Literal["config"] = "config"`：
     - `"config"` → 用 `RiskConfig.audit_log`（相对路径按 `PROJECT_ROOT` 解析），
     - `None` → 仅内存（测试/性能场景），
     - 具体路径 → 写到该路径；
  2. `RiskEngine.close()`（🆕）关闭 `AuditStream`；`BacktestEngine.run()` 在收尾时调用，保证落盘完整；
  3. `diagnostics` 增加 `risk_audit_log` 与实际写入条数 `audit_records`；
  4. 文档：说明审计文件为 JSONL（每行一条决策），并给出字段表。
- **接口变化**：`BacktestEngine(risk_audit_log=...)`、`RiskEngine.close()`、`RuleRiskEngine.audit_records` 计数。
- **回归测试**（`tests/test_defect_06_risk_audit.py`）：
  1. 指定临时路径 → 回测后文件存在且 JSONL 行数 ≥ 1、字段含 `category/action/payload`；
  2. `risk_audit_log=None` → 不创建文件，`audit_records > 0`（内存中仍有记录）；
  3. 配置里的相对路径 → 解析到项目根下；
  4. 关闭后再次写入不报错（幂等）。

### 缺陷 7：`NullRiskEngine` 的 stats 口径不明

- **根因**：`RuleRiskEngine` 有 `RiskStats`，`NullRiskEngine` 没有；`_diagnostics` 对二者输出结构不同
  （`{"checked": 0}` vs 完整字典），下游报告无法统一消费。
- **修复方案**：
  1. 把 `RiskStats` 上移到 `risk/base.py`，`RiskEngine.__init__` 创建 `self.stats = RiskStats()`；
  2. `NullRiskEngine.check_order` 也累加 `checked`（并新增 `stats.enabled = False` 标记）；
  3. `RiskStats.as_dict()` 增加 `enabled: bool`，确保开启/关闭两种模式的字段完全一致；
  4. `_diagnostics` 统一输出 `self.risk.stats.as_dict()`；
  5. 文档：明确口径 —— **关闭风控时 `checked` = 引擎询问过的订单数，`rejected/reduced` 恒为 0，
     `reject_rate = 0` 不代表「无风险」，只代表「未启用风控」**。
- **接口变化**：`RiskStats` 迁移 + `enabled` 字段；`RiskEngine.stats` 属性。
- **回归测试**（`tests/test_defect_07_risk_stats_schema.py`）：
  1. 关闭风控跑一次回测 → `stats["enabled"] is False`、`checked == 提交订单数`、`rejected == 0`；
  2. 开启风控 → 同样的键集合（结构一致）；
  3. `risk_enabled=false` 时 `reject_rate == 0` 且 diagnostics 中显式说明未启用。

### 缺陷 8：`_listed_days_for` 静默用 `bar_seq` 兜底

- **根因**：`data/store.py::_listed_days_for` 在缺少 `list_date` 时用「该标的第几根 K 线」代替上市天数；
  若数据从中间开始（或只取了回测区间），上市天数被严重低估 → 股票池「上市不足 60 日」过滤失效。
- **修复方案**：引入**显式上市日策略**，绝不静默兜底：
  ```yaml
  data:
    listing_date:
      policy: strict        # strict | proxy（默认 strict）
      proxy_warn_once: true
  ```
  - `strict`：``DataStore`` 构造时若有标的缺 `list_date` → 直接抛 `DataQualityError`
    （报错信息包含修复路径：补数据 或 显式改为 `proxy`）；
  - `proxy`：允许降级，但必须**显式**，且**四个位置**同时披露：
    `DataStore.listed_days_source(symbol)`、`quality.stats`（含样本）、
    `UniverseStats.proxy_symbols`、回测 `diagnostics.listed_days_proxy_symbols`；
  - `listed_days_source` 返回**三态**（实现后细化）：
    | 取值 | 含义 |
    |---|---|
    | `list_date` | list_date 落在数据日历内，按交易日**精确**计数 |
    | `list_date_window` | list_date 早于数据起点，按工作日**近似**（只高估不低估） |
    | `bar_seq_proxy` | **缺 list_date**，用 K 线序号代理（仅 `policy=proxy` 允许） |
  - AKShare provider 在 `fetch_symbol_meta` 层强制校验 `list_date` 非空（provider 契约，见 §5.2）；
  - 合成数据补齐 `list_date`（老股取数据起点前 400 天 → 走 `list_date_window`），
    测试夹具默认 `proxy`（手工造数通常不提供上市日）。
- **接口变化**：`DataConfig.listing_date`（新子结构）、`DataStore.listed_days_source` /
  `proxy_listed_symbols` / `window_listed_symbols`、`DataQualityReport.stats` 新键、
  `UniverseStats.proxy_symbols`。
- **回归测试**（`tests/test_defect_08_listing_date.py`）：
  1. `policy=strict` + 缺 `list_date` → `DataQualityError`；
  2. `policy=proxy` → `listed_days_source == "bar_seq_proxy"`、`quality.stats` 记录该标的、
     `diagnostics` 中出现计数、日志有 warning；
  3. 有 `list_date` → `listed_days_source == "list_date"`，且上市交易日数按日历计算正确；
  4. **回归本质**：数据从区间中间开始时（只取 30 天行情），`strict` 必须报错而不是把老股当成新股过滤掉；
  5. provider 契约测试：`fetch_symbol_meta` 缺 `list_date` 的 AKShare 实现必须抛错。

### 缺陷 9：`volume_ratio` 极小基准导致量比爆炸

- **根因**：`strategy/indicators.py::volume_ratio` 用 `base.replace(0, np.nan)` 只挡了严格 0；
  停牌复牌、新股上市等场景下 20 日均量可能只有几百股，量比被放大成几十倍 → 假信号。
- **修复方案**：
  ```python
  def volume_ratio(volume, window, *, exclude_current=True, min_base: float = 0.0) -> DataFrame:
      # base < min_base → NaN（视为无信号），而不是产生巨大比值
  ```
  `VolumeStrategy` 新增参数 `volume_min_base: float = 10000.0`（股），并写入 `configs/strategies/volume.yaml`；
  指标层函数默认 `0.0`（纯函数保持无假设），策略层传配置值。
  另外 `meta` 中记录 `volume_ma` 与 `volume_ratio`，便于报告层审查。
- **接口变化**：`volume_ratio(..., min_base=)`、`VolumeStrategy.PARAM_KEYS` 增加 `volume_min_base`。
- **回归测试**（`tests/test_defect_09_volume_ratio_guard.py`）：
  1. base=1e6、当日量=2e6 → 量比 2.0（正常）；
  2. base=100（低于阈值 10000）→ `NaN`，策略不产生信号；
  3. base 恰好等于阈值 → 参与计算（边界）；
  4. 配置 `volume_min_base` 为负 → `ConfigError`；
  5. 端到端：构造「长期停牌后复牌巨量」行情，策略不误报。

### 缺陷 10：测试代码坏味道

- **根因 A**：`tests/test_events.py`（**已核对定位**：不在 `test_engine_backtest.py`）中
  `__side_buy()` 定义在第 59 行、却在第 42 行首次调用，依赖运行时查找；
  且 `__` 前缀命名会让 `from module import *` 与部分静态检查失效，同时遮蔽了已导入的 `Side`。
  **修复**：删除该函数，直接使用 `Side.BUY`（`test_events.py` 顶部已导入 `Side`）。
- **根因 B**：`tests/test_portfolio_engine.py::make_setup` 先调用 `load_portfolio(...)`（第 39 行），
  紧接着又被 `build_portfolio(section, ...)` 覆盖 → 冗余调用且掩盖了真实配置来源。
  **修复**：删除 `load_portfolio` 调用与对应 import，保留显式 `build_portfolio`。
- **回归测试**：无新增功能测试；由既有 398 用例保证不回归；
  另在 `tests/test_docs_consistency.py` 增加一条**静态检查**：测试文件中不得存在
  「先调用后定义」的模块级私有函数（AST 检查 `__` 前缀函数的定义位置），防止再次出现。

### 缺陷 11：文档与实现不一致

- **已确认的具体问题**：
  1. `docs/06_risk_rms.md` §2.2 标题写「规则清单（12 条）」，实际 13 条；
  2. 同文档 §4 触发延迟口径需按缺陷 1 的修复重写；
  3. `docs/00_system_design.md` §6 模型映射表中 `risk/var.py`、`portfolio/sizing.py` 仍标 ❌（实际已交付）；
  4. `docs/00_system_design.md` §2 目录树中 `metrics/`、`report/` 标 ❌（第二轮将交付，需更新为 🆕）；
  5. `README.md` 的用例数/模块数需随每轮更新（当前 398/24）；
  6. `docs/03_acceptance_report.md` 的阶段范围标题需随轮次更新。
- **修复方案（防复发）**：新增 `tests/test_docs_consistency.py`，用 AST/正则从代码中取真值，与文档中的数字比对：
  | 断言 | 代码真值来源 | 文档位置 |
  |---|---|---|
  | 风控规则条数 | `len(aqs.risk.available_rules())` | `docs/06` §2.2 标题、README |
  | 策略条数 | `len(aqs.strategy.available_strategies())` | `docs/00` §2、README |
  | 组合类条数 | `len(aqs.portfolio.available_portfolios())` | `docs/00` §2、README |
  | 测试用例总数 | AST 统计 `tests/test_*.py` 中的 `test_*` 函数/方法数 | README、`docs/03` §1 |
  | 测试模块数 | `tests/test_*.py` 文件数 | README、`docs/03` §1 |
  | 包版本 | `aqs.__version__` | `pyproject.toml` |
  失败信息中给出「文档应为 X」的修复建议。
- **回归测试**：`tests/test_docs_consistency.py`（上述 6 条断言，另有 1 条 AST 坏味道检查）。

### 缺陷 12：`--symbols` 参数与输出口径不符（**已复现，成因与原始描述不同**）

- **复现结论（本机实测）**：`generate_market_data(n_symbols=12/15/30/50)` 的
  生成代码数、唯一代码数、`bundle.symbols()` 数量**三者完全一致**（例：30 → 30），
  因此「生成 30 但只有 12 个标的」并非生成器丢标的。
- **真实成因**：输出把**三个不同口径**混在一起，读者无法分辨：
  1. `--symbols 30` = **生成** 30 只（`generate_market_data`）；
  2. 指数成分默认 `index_size=15`（`SyntheticMarketConfig.index_size`）= **入指数** 15 只；
  3. 经股票池过滤（ST/停牌/上市不足 60 日/20 日均额 < 5000 万）后 = **当日入池** 约 12 只。
  demo 只打印了 `store.describe()`，其中 `symbols` 是「有行情的标的数」，
  与「入池数」在概念上不同，导致读数歧义。
- **次要健壮性问题**：`data/synthetic.py::_symbol_codes` 用
  `while s in seen: s = s.replace(".", f"{rng.integers(1,9)}.", 1)` 做去重兜底，
  该写法会产出非法代码（如 `6000005..SH`）。当前参数下该分支为**死代码**（已实测无重复），
  但一旦 `n` 增大或板块分配规则调整即可能触发 → 改为**按板块族递增分配**，从根上消除重复。
- **修复方案**：
  1. CLI 改名：`--generate-symbols`（保留 `--symbols` 为 deprecated 别名并打印提示）；
  2. 输出与 `summary.json` 明确三段口径：`generated / index_members / universe_avg`
     （新增 `data_scope` 段：`generated, with_bars, index_members, universe_avg, universe_first_day`）；
  3. `_symbol_codes` 改为确定性分配（600xxx/000xxx/300xxx/688xxx 各自独立计数器），
     并加断言 `len(set(codes)) == n` 且全部匹配 `^\d{6}\.(SH|SZ)$`；
  4. 合成配置暴露 `index_size`（默认 15）到 CLI（`--index-size`），让「入池多少」可控；
  5. 合成数据补齐 `list_date`（缺陷 8 前置）与 `industry`（RMS 行业规则用）。
- **接口变化**：`generate_market_data(..., industry=True)`、demo CLI 参数、`summary.json` 的 `data_scope` 段。
- **回归测试**（`tests/test_defect_12_demo_scope.py`）：
  1. `_symbol_codes(50)` 全部合法（正则）且无重复；
  2. `generate_market_data(n_symbols=30)` 的 `bars` 覆盖 30 个标的、每个都有 `list_date`；
  3. CLI 解析：`--generate-symbols` 与 `--symbols` 均可解析，后者打印 deprecation；
  4. `summary.json` 的 `data_scope` 四元组自洽（`generated == with_bars == 30`），
     且 `universe_avg ≤ index_members ≤ generated`；
  5. `index_size` 从配置生效（`--index-size 5` → 指数成分 ≤ 5）。

---

## 5. 数据源设计要点（详见 `docs/11_akshare_provider.md`）

### 5.1 从数学模型反推字段（不做无用搬运）

| 模型 / 用途 | 需要的数据字段 | 为什么需要 | 缺失后果 |
|---|---|---|---|
| MA/突破/量比信号 | 后复权 OHLC、成交量 | 收益连续性是信号前提 | 除权日假突破 |
| 涨跌停撮合 | 原始 close/prev_close、板块、ST 标记 | 涨停不可买、跌停不可卖 | 不可成交的订单被成交，收益虚高 |
| 成本模型 | 成交额、成交量（VWAP） | 佣金/印花税/滑点基数 | 成本失真 |
| 冲击成本 η(Q/ADV)^θ | **ADV（股）**、参与率 | Q/ADV 是冲击成本的唯一自变量 | 大额订单成本被低估 |
| 容量估计（M6） | ADV（元）、日成交额分布 | 规模放大后收益衰减 | 容量被高估 |
| 股票池 | list_date、delist_date、ST、停牌 | 上市不足 60 日/退市/ST 过滤 | 幸存者偏差、新股噪音 |
| 指数基准（M6） | 指数行情 + **历史成分** | 相对收益与信息比率 | 基准不可比 |
| 多因子（M9） | 报告期 + **公告日** + ROE/净利润/营收/资产/权益 | PIT 因子构造 | 严重未来函数 |
| Barra 风格（M9） | 总市值/流通市值、换手率、波动率 | 规模/流动性/波动因子暴露 | 因子模型无法中性化 |
| RMS 行业暴露 | 行业分类（历史） | 单行业敞口上限 | 规则失效 |

### 5.2 provider 契约（四实现共用一套契约测试）

```python
class ProviderContract:      # tests/contracts/provider_contract.py
    def test_capabilities_declared(self);  def test_schema_conformant(self)      # canonical schema
    def test_primary_key_unique(self);     def test_pit_columns_present(self)    # list_date/announce_date
    def test_no_future_rows(self);         def test_provenance_returned(self)
    def test_adjust_field_consistency(self)  # 后复权因子 > 0 且首日归一
```

### 5.3 缓存 / 限流 / 重试 / 降级

```
fetch_* → RateLimiter.acquire() → Cache.is_fresh? → 命中：直接返回(cache_hit=True)
                                                  → 未命中：Provider 拉取(带 retry_call)
                                                              → 标准化 → 质量检查 → 写缓存 → 返回
拉取失败 → 重试 N 次 → 仍失败：按 failure_policy 处理
        ├─ fallback: csv/parquet 本地快照（记录 warning，provenance.source 标注 fallback）
        └─ fail: 抛 DataError（并保留已有缓存不删除）
```

### 5.4 AKShare 实现的诚实约束

- 本机**无外网**（pip 安装 PyPI 亦超时），因此实现将：
  1. 用 `tools/probe_akshare.py` 输出「接口可用性 + 字段清单」报告，**探测结果作为字段映射的固化依据**；
  2. 单元测试全部基于 `tests/fixtures/akshare/*.csv` 的**脱敏样例**，不依赖网络；
  3. 在线拉取由使用者执行 `python tools/fetch_data.py --provider akshare ...`；
  4. 设计文档中的接口名标注为**候选**，实现前用探测结果替换，避免「照搬文档猜接口」。

---

## 6. 配置变更（新增参数与默认值）

```yaml
# configs/base.yaml（新增/调整）
data:
  provider: synthetic            # synthetic | csv | parquet | akshare
  listing_date:
    policy: strict               # strict | proxy（缺陷 8）
    proxy_warn_once: true
  quality:
    strict: true
    fail_on: [error]             # error | warning
  akshare:                       # 见 configs/data.yaml，可整体覆盖
    rate_limit: { requests_per_minute: 300, burst: 10 }
    retry:      { max_attempts: 5, backoff_seconds: 1.5, jitter: true }
    cache:      { root: data/cache, format: parquet, version: 1, ttl_hours: 24, incremental: true }
    adjust: hfq
    index_codes: ["000300.SH", "000905.SH", "000852.SH"]
    failure_policy: { on_provider_error: fallback, fallback_provider: parquet }

engine:
  risk_audit_log: config         # config | none | <path>（缺陷 6）
  exposure_control:
    unmet_warning_days: 5        # 缺陷 4

risk:
  audit_log: reports/rms_audit.jsonl   # 生效；JSONL 审计流

metrics:                          # 🆕 M6
  risk_free_rate: 0.02
  trading_days_per_year: 252
  confidence_levels: [0.95, 0.99]
  benchmarks: ["000300.SH", "000905.SH", "000852.SH"]
  capacity: { participation: [0.05, 0.10, 0.20], target_impact: 0.02 }
  montecarlo: { paths: 1000, block: 20, seed: 20240101 }

report:                           # 🆕 M7
  formats: [html, markdown]
  output_dir: reports
  include_charts: true
  max_trades_in_table: 200
  sections: [summary, equity, drawdown, trades, positions, exposure,
             attribution, metrics, rms, engine_acceptance, cost, data_quality]

strategy:                         # ✏️ 缺陷 9
  # configs/strategies/volume.yaml: params.volume_min_base = 10000
```

> 所有新增参数都进 `config/schema.py` 的 dataclass 并做范围校验（未知键继续报错）。

---

## 7. 测试计划

### 7.1 分层

| 层 | 内容 | 位置 | 是否依赖网络 |
|---|---|---|---|
| L1 单元 | 函数/规则/指标手算一致性 | `tests/test_*.py` | 否 |
| L2 契约 | 4 个 provider 共用同一套契约断言 | `tests/contracts/` | 否（离线 fixture） |
| L3 集成 | 引擎 + 策略 + 组合 + 风控 + 指标 + 报告全链路 | `tests/test_*_engine*.py` | 否（合成数据） |
| L4 偏差 | 未来函数 / 幸存者 / 过拟合 | `tests/test_bias_*.py` | 否 |
| L5 压力 | 危机窗口 / 极端波动 / 流动性枯竭 / 成本敏感性 | `tests/test_stress_*.py` | 否 |
| L6 文档 | 文档与代码一致性、包元数据 | `tests/test_docs_consistency.py`、`test_packaging.py` | 否 |
| L7 在线 | 真实 AKShare 抓取（手动，非 CI） | `tools/fetch_data.py --self-test` | 是 |

### 7.2 新增用例估算

| 模块 | 预估用例 | 说明 |
|---|---|---|
| 缺陷 1~12 回归 | 34 | 见 §4 各条 |
| DataProvider 契约 + 缓存 + 限流 | 30 | 4 provider × 契约 + 缓存增量/版本/熔断 |
| AKShare 映射（离线 fixture） | 22 | 接口→canonical schema、质量报告、降级 |
| M6 metrics | 45 | 10 个指标文件，含手算一致性 |
| M7 report | 20 | 渲染、章节、图表、验收报告 |
| 偏差与压力套件 | 32 | 3 类偏差 + 3 段危机 + 成本/流动性 |
| 工程化/文档一致性 | 8 | 依赖声明、入口点、文档数字 |
| **合计新增** | **≈191** | 总量预计 **≈590**（±20%） |

### 7.3 关键测试用例（示例，说明"怎么测"）

```python
# 未来函数（L4）
def test_signal_only_uses_data_up_to_decision_time():   # 逐日断言 PIT 视图上界
def test_fill_is_next_trading_day_after_signal():       # 含顺延，用 calendar 计算期望值
def test_fundamentals_not_visible_before_announce_date():
# 幸存者偏差（L4）
def test_universe_contains_delisted_name_before_delisting():
def test_index_membership_changes_over_time():
# 过拟合（L4）
def test_in_sample_vs_out_of_sample_metrics_differ_and_are_both_reported():
def test_parameter_sensitivity_degrades_gracefully():   # 参数网格，收益不应出现单点尖峰
def test_cost_doubling_still_tradable_or_flags_decay():
def test_walk_forward_windows_cover_full_period_without_overlap():
def test_montecarlo_resample_is_reproducible_with_seed():
# 压力（L5）
def test_gfc_2008_scenario_runs_and_reports_drawdown():
def test_crash_2015_scenario_runs(): def test_covid_2020_scenario_runs():
def test_liquidity_drought_blocks_orders_via_participation_cap():
def test_limit_down_streak_defers_sells_and_records_latency():
```

### 7.4 质量门禁（每个里程碑必须满足）

1. `python tests/run_tests.py` 全绿（含新增用例），失败数 = 0；
2. `tests/test_docs_consistency.py` 通过（文档数字与代码一致）；
3. 每个里程碑的**验收命令**在文档中给出，并已在本机实际执行过；
4. 任何影响回测结果的参数都在 `configs/` 中，代码内无魔法数字（新增代码需自查）；
5. 涉及未来函数/T+1/涨跌停/幸存者偏差的改动，必须有显式用例（L4）；
6. 里程碑结束时更新 `CHANGELOG.md` 与相关 `docs/`。

### 7.5 防退化

- 缺陷修复的用例**永久保留**，并在文件中注明「回归缺陷 #N」；
- 新增的 `test_docs_consistency.py` 直接把「文档漂移」变成红灯。

---

## 8. GitHub 上传步骤（详见 `docs/17_github_upload.md`）

### 8.1 仓库信息

| 项 | 值 |
|---|---|
| 远端 | <https://github.com/shark-feng/quant-forge> |
| 仓库名 | `quant-forge` |
| 默认分支 | `main` |
| 包名 / import 名 | `quant-forge` / `aqs`（保持不变，避免大范围改名） |
| 可见性 | 建议 **private**（含策略与研究结论；确认后也可 public） |

### 8.2 许可证选择

| 选项 | 适合场景 | 注意 |
|---|---|---|
| **MIT（建议）** | 研究/教学，希望他人自由使用与修改 | 无专利条款 |
| Apache-2.0 | 需要显式专利授权与 NOTICE 机制 | 与「参考 GPL 框架」的边界更清晰 |
| 不建议 | 本项目**不含** GPL 代码；若未来集成 Backtrader（GPL-3.0），需隔离为可选插件并单独声明许可 |

> 结论：**MIT** + `NOTICE`（数据来源、AKShare 署名、合规声明、第三方框架许可证清单）。
> 若后续 vendored/复制任何 GPL 代码，必须在 `NOTICE` 中声明并保持隔离（不链接进主包）。

### 8.3 上传前检查清单

1. **敏感信息扫描**（重要）：`git grep -nE "sk-|api[_-]?key|token|password"` → 命中即加入 `.gitignore` 或移出仓库；
2. 大文件与数据：`data/raw`、`data/cache`、`reports/*` 已在 `.gitignore`；单文件 > 50MB 不得入库；
3. `reports/rms_audit.jsonl` 等运行产物不入库（仅保留 `.gitkeep`）；
4. `__pycache__`、`.tmp_tests`、`.pytest_cache` 不入库；
5. README 完整性按 §8.5 的清单自检；
6. `python tests/run_tests.py` 全绿、`python examples/demo_backtest.py` 可跑通；
7. `pyproject.toml` 中的 `requires-python`、`dependencies`、`optional-dependencies` 与 `requirements.txt` 一致（由 `test_packaging.py` 守护）。

### 8.4 上传命令（二选一）

**路径 A：仓库已存在（网页端已创建空仓库）**

```powershell
cd D:\Quantify
git init
git branch -M main
git add .
git status                      # 人工确认没有数据/密钥/报告产物
git commit -m "chore: 第二轮工程化基线（含 M0-M5 与 12 条缺陷修复）"
git remote add origin https://github.com/shark-feng/quant-forge.git
git push -u origin main
```

**路径 B：用 GitHub CLI 新建并推送（推荐）**

```powershell
# 前置：winget install --id GitHub.cli  （或 scoop install gh）
gh auth login                     # 浏览器授权一次

cd D:\Quantify
git init
git branch -M main
git add .
git commit -m "chore: 第二轮工程化基线（含 M0-M5 与 12 条缺陷修复）"
gh repo create quant-forge --private --source=. --remote=origin --push
```

**后续提交规范**

```powershell
git checkout -b fix/r2-01-defects
# … 修改 …
git add -A
git commit -m "fix(r2-01): 修复 latency/board/TIF/减仓/根目录/审计/stats/上市日/量比 12 条缺陷"
git push -u origin fix/r2-01-defects
gh pr create --fill               # 或 git push origin main（单人项目）
```

**tag 与发布（可选）**

```powershell
git tag -a v0.2.0 -m "round-2: defects fixed, akshare provider, metrics & report"
git push origin v0.2.0
gh release create v0.2.0 --notes-file CHANGELOG.md
```

### 8.5 README 必含章节（验收项）

1. 项目简介（一句话 + 目标）；2. **合规声明**（研究用途、不接实盘、第三阶段仅模拟）；
3. 目录结构；4. 安装（Python 3.10+，核心/可选依赖分组）；5. 快速开始（测试 + 合成数据回测）；
6. **AKShare 数据准备**（安装、探测、抓取、缓存位置、质量报告、离线使用）；
7. 回测示例（三种策略 + 报告生成命令）；8. 测试命令与当前用例数；
9. 阶段进度表（M0~M13）；10. 免责声明；11. 许可证与致谢（AKShare 等）。

### 8.6 上传后验证

```powershell
git remote -v
git log --oneline -3
gh repo view --web                # 打开仓库页面确认文件齐全
python -c "import urllib.request;print(urllib.request.urlopen('https://github.com/shark-feng/quant-forge').status)"
```

---

## 9. 里程碑与确认点

| 里程碑 | 内容 | 交付后确认点 |
|---|---|---|
| **M1** | 12 条缺陷修复 + 回归用例（`tests/test_defect_*.py`） | 「12 条是否都按预期修复」 |
| **M2** | 工程化：LICENSE/NOTICE/README/.gitignore/pyproject/上传脚本 + 实际上传 | 「仓库可见性（private/public）、许可证（MIT/Apache）」 |
| **M3** | 框架选型矩阵（`docs/12`）+ 决策记录 | 「主引擎边界是否认可」 |
| **M4** | DataProvider 抽象 + 缓存 + 限流 + 契约测试 | 「provider 接口是否满足扩展需要」 |
| **M5** | AKShare 适配 + 探测脚本 + 抓取 CLI + 质量报告 | 「在线抓取由你执行；字段映射以探测报告为准」 |
| **M6** | M6 metrics（11 模块 + 测试） | 「指标口径（无风险利率、年化、容量定义）是否有异议」 |
| **M7** | M7 report（HTML/Markdown/验收报告）+ 第一份完整报告 | 「报告章节是否齐全、是否要加中文字体/图表类型」 |
| **M8** | 偏差与压力测试套件 | 「危机场景参数是否需要调整」 |
| **M9** | 第二阶段设计（`docs/16`）+ 模型→模块映射 | 「第二阶段优先级与验收标准」 |

---

## 10. 风险与待确认事项

| # | 事项 | 影响 | 我的建议 | 需要你确认 |
|---|---|---|---|---|
| 1 | 本机**无外网** | AKShare 在线抓取无法在本次会话内验证 | 实现 + 离线 fixture 测试全绿；在线抓取与探测报告由你在联网环境执行 | 是否接受「离线实现 + 你执行在线验证」 |
| 2 | 仓库可见性 | private 更安全，public 便于展示 | 先 **private**，稳定后再转 public | private 还是 public |
| 3 | 许可证 | 影响他人使用 | **MIT** + NOTICE | MIT / Apache-2.0 |
| 4 | 数据落地位置 | `data/cache` 体积可能较大（全A股日线多年 ≈ 数百 MB） | 不入库，仅本地缓存；提供 `--symbols/--index` 限额 | 是否只做指数成分（沪深300/中证500/中证1000）子集 |
| 5 | 财务数据接口的公告日字段 | 若 AKShare 候选接口无公告日，则不能用于 PIT 因子 | 探测后若缺失 → 该字段标记为 `capabilities.fundamentals=False`，M9 前必须解决 | 是否接受「先探测再定」 |
| 6 | 报告图表实现 | 不引入 matplotlib 会导致图表较朴素 | 内嵌 SVG（净值/回撤/持仓分布/月度热力图） | 是否接受无 matplotlib |
| 7 | `listing_date.policy` 默认值 | strict 会让现有合成数据部分场景报错 | 默认 **strict**，合成数据补齐 `list_date` 后不受影响 | 是否接受 strict 为默认 |
| 8 | 缺陷 4 的减仓口径 | 约定「次日生效」而非「实际成交日」 | 报告层同时输出约定日与完成日 | 是否接受 |

---

## 11. 本轮不做的事（边界声明）

1. 不实现第二阶段的因子/优化/波动率/配对交易（只出设计文档，M9）；
2. 不实现 Tick/订单簿回放与最优执行（第三阶段）；
3. 不接入任何券商交易接口，不做自动下单；
4. 不引入 Backtrader 作为主引擎（详见 `docs/12_framework_matrix.md` 结论）；
5. 不修改现有模块的**对外语义**（除缺陷修复涉及的行为变更，且均已列明并有回归用例）。
