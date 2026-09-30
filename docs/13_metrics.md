# M6 评价层设计：指标 / 基准 / 暴露 / 归因 / 稳健性

> 状态：**设计稿，等确认后实现**（里程碑 M6）。
> 对应代码：`src/aqs/metrics/`（11 个模块）。
> 验收门禁：`tests/test_metrics_*.py` 全绿；所有指标有**手算一致性**用例。

---

## 1. 职责边界（合同禁止项优先）

| M6 **做** | M6 **不做** |
|---|---|
| 消费 `BacktestResult` / `MetricsReport`，计算指标 | **不得修改回测路径**：不碰 `BacktestEngine`、不碰账户与撮合 |
| 通过注入的 `runner` 回调触发重跑（敏感性/样本外/滚动） | 不自己决定「怎么重跑」——重跑策略由调用方给定 |
| 输出纯数据（`MetricsReport` / `DataFrame` / JSON） | 不做绘图与排版（属 M7） |
| 明确每个指标的定义与口径（写进本文档） | 不做因子模型（属 M9） |

**唯一允许的依赖方向**：`metrics → (BacktestResult, DataStore, 基准序列)`，只读。

---

## 2. 模块划分

```
src/aqs/metrics/
├── __init__.py         导出与注册表
├── config.py           MetricsConfig（强类型 + 校验）
├── returns.py          NAV → 收益序列、年化、波动、CVaR 输入
├── risk.py             夏普 / Sortino / Calmar / 最大回撤 / 回撤持续期 / VaR / ES
├── relative.py         信息比率 / 跟踪误差 / Beta / Alpha / 超额年化
├── trading.py          换手率 / 交易统计 / 成本后收益 / 成本占比
├── capacity.py         容量估计（参与率 + 冲击成本二分求解）
├── exposure.py         行业 / 市值 / 风格暴露（可用字段）
├── attribution.py      成本归因 / 信号归因 / 基准回归归因
├── sensitivity.py      成本与滑点敏感性（多场景重跑）
├── splits.py           样本内/外分割、滚动窗口
├── montecarlo.py       Block Bootstrap 蒙特卡洛
└── engine.py           MetricsEngine：编排上述模块，产出 MetricsReport
```

---

## 3. 指标定义（口径必须唯一确定）

符号：`V_t` 为第 t 日收盘总资产，`n` 为交易日数，`N=252`（`trading_days_per_year` 可配），
`r_t = V_t / V_{t-1} - 1`，`rf_a` 为年化无风险利率（默认 0.02），`rf_d = (1+rf_a)^(1/N) - 1`。

| 指标 | 公式 | 说明 / 边界 |
|---|---|---|
| 累计收益 | `V_T/V_0 - 1` | — |
| 年化收益（几何） | `(1+cum)^(N/n) - 1` | `n` 为**交易日数**；不足 1 年时按同式外推，报告中标注 |
| 年化波动 | `std(r, ddof=1) × √N` | `n<2` 时返回 `nan` 并告警 |
| 日胜率 | `count(r>0) / n` | — |
| 夏普比率 | `(mean(r) - rf_d) / std(r) × √N` | 日频算术年化；`std=0` → `nan` + warning |
| 下行波动 | `√( mean( min(r - rf_d, 0)² ) ) × √N` | MAR 默认取 `rf_d`，可配 0 |
| Sortino | `(mean(r) - rf_d) × N / 下行波动` | 分母为 0 → `nan` |
| 最大回撤 | `min_t( V_t / cummax(V)_t - 1 )` | 返回 ≤0 的负数 |
| 回撤持续期 | 从峰值日起到净值重新 ≥ 该峰值的交易日数（未恢复则为至今） | 另出「最长回撤持续期」 |
| Calmar | `年化收益 / |最大回撤|` | MDD=0 → `nan` |
| 年化超额 | `(1+excess_cum)^(N/n) - 1`，`excess_cum = V_T/V_0 - B_T/B_0`（几何差口径见下） | 同时输出**算术差**口径，两者都写报告，避免口径争议 |
| 跟踪误差 | `std(r - r_b, ddof=1) × √N` | — |
| 信息比率 | `mean(r - r_b) / std(r - r_b) × √N` | 与跟踪误差同源 |
| Beta | `cov(r, r_b) / var(r_b)` | `var=0` → `nan` |
| Alpha（CAPM 年化） | `年化收益 - rf_a - β × (基准年化 - rf_a)` | M9 将扩展为多因子 alpha |
| 单边年化换手 | `Σ_t (买入额+卖出额)/2 ÷ mean(V) × N/n` | 另出「双边」口径与「换手次数」 |
| 成本后收益 | 直接用 `V_T/V_0-1`（引擎已扣成本） | — |
| 成本前收益 | 用 `costs.scale = 0` **重跑**得到 | 不做事后近似（近似会低估复利影响） |
| 成本占净值比 | `Σ成本 / mean(V)` | 另按科目拆分 |
| 年化成本率 | `Σ成本/m mean(V) × N/n` | — |
| 容量（金额） | 见 §4 | 解一元方程 |
| VaR / ES | 复用 `aqs.risk.var.historical_var_es`（历史模拟） | 与 M5 同一实现，口径一致 |
| 破产概率（MC） | `P(净值 < 初始 × (1-threshold))` | 阈值可配（默认 0.5） |

> **口径争议处理原则**：凡有两种常见口径的指标（年化超额、换手、Alpha），
> **两种都算并都写进报告**，而不是挑一个装作没有争议。

---

## 4. 容量估计（可实现的定义，不玩概念）

**目标**：求最大 AUM，使得**年化冲击成本率 ≤ `target_impact`**（默认 2%）。

单笔冲击成本比例（与 M2 成本模型一致）：`impact_i = η × (Q_i / ADV_i)^θ`
其中 `Q_i = AUM × |Δw_i| / price_i`（股），`ADV_i` 为该标的日均成交量（股）。

组合年化冲击成本率：

```
C(AUM) = (N/n) × Σ_t Σ_i |Δw_{i,t}| × η × ( AUM × |Δw_{i,t}| / (price_{i,t} × ADV_{i,t}) )^θ
```

`C(AUM)` 关于 AUM 单调递增（θ>0），用**二分法**在 `[AUM_lo, AUM_hi]` 上求根：

```
lo, hi = 0, 1e12
while hi - lo > 1e-6 * hi:
    mid = (lo + hi) / 2
    if C(mid) > target_impact: hi = mid
    else: lo = mid
capacity = lo
```

同时输出敏感性表：`participation ∈ (0.05, 0.10, 0.20)` 下的容量与对应冲击成本、以及
「当前 AUM 下的年化冲击成本」。

**已知局限（必须写进报告）**：`Δw` 来自**历史权重变化**，即假设未来换手结构与回测期相同；
ADV 取回测期均值，未考虑规模放大后的市场冲击反身性。

---

## 5. 暴露与归因（M6 可得字段 vs M9 交付）

| 维度 | M6（本阶段） | M9（第二阶段） |
|---|---|---|
| 行业暴露 | 按 `industry` 聚合持仓市值占比（时间平均 + 序列） | 行业中性化约束 |
| 市值暴露 | 持仓加权 `ln(total_mv)` 相对基准分位 | Size 因子暴露 |
| 风格暴露 | **可用字段**：波动率（20/60 日）、换手率、动量（20/60 日） | Barra 风格（Value/Growth/Quality…） |
| 成本归因 | 佣金/印花税/过户费/滑点/冲击 明细占比 | 同 |
| 信号归因 | 按 `SignalIntent.reason` 分组：笔数/胜率/平均持有天数/已实现盈亏贡献 | 同 |
| 因子归因 | **基准回归**：`r = α + β·r_bench + ε`，输出 α/β/R² | 多因子收益分解 |

**归因守恒校验**（作为测试用例）：
- `Σ 成本明细 == account.total_costs`（容差 1e-6）；
- `Σ 信号分组已实现盈亏 == account.realized_pnl + 未平仓浮动`（容差 1e-6）；
- 基准回归：`α + β·mean(r_b)` 与 `mean(r)` 的差 == `mean(ε)`（恒等式自检）。

---

## 6. 稳健性分析（重跑通过注入的 runner）

```python
Runner = Callable[[BaseConfig], BacktestResult]      # 调用方给出「怎么重跑」
```

| 分析 | 实现 | 输出 |
|---|---|---|
| 成本/滑点敏感性 | 用 `CostScenario` 生成配置覆盖 → runner 重跑 | 场景 × 指标矩阵（含「策略是否仍有效」判定：年化>0 且信息比率>阈值） |
| 样本内/外分割 | runner 两次（不同区间） | 两段指标 + 衰减率（`out/in`） |
| 滚动窗口 | runner 多次（`window`/`step`） | 每窗指标序列 + 正收益窗口占比 + 指标标准差 |
| 蒙特卡洛 | 对日收益做 **Block Bootstrap**（默认 block=20，保留自相关）→ 重算净值路径 | 分位数（5/25/50/75/95）年化收益与 MDD、破产概率、可复现（seed） |

> **为什么用 Block Bootstrap**：日收益存在自相关与波动聚集，简单 IID 重采样会低估尾部风险。

伪代码（蒙特卡洛）：

```
def montecarlo(returns, paths, block, seed, initial=1.0):
    rng = default_rng(seed); n = len(returns); n_blocks = ceil(n / block)
    for p in range(paths):
        path = []
        while len(path) < n:
            s = rng.integers(0, n - block + 1)
            path.extend(returns[s : s + block])
        v = initial * cumprod(1 + path[:n])
        record(annualized(v), max_drawdown(v), ruin(v))
    return MCReport(quantiles, ruin_prob, paths)
```

---

## 7. 接口定义

```python
# config.py
@dataclass(slots=True)
class MetricsConfig:
    risk_free_rate: float = 0.02
    trading_days_per_year: int = 252
    confidence_levels: tuple[float, ...] = (0.95, 0.99)
    benchmarks: tuple[str, ...] = ("000300.SH", "000905.SH", "000852.SH")
    sortino_mar: float | None = None          # None → 取 rf_d
    capacity: CapacityConfig = CapacityConfig()
    montecarlo: MonteCarloConfig = MonteCarloConfig()
    validity: ValidityConfig = ValidityConfig()   # 「策略是否仍有效」的判定阈值

# returns.py
def daily_returns(nav: pd.Series) -> pd.Series
def cumulative_return(nav: pd.Series) -> float
def annualized_return(nav: pd.Series, *, periods_per_year: int) -> float
def annualized_volatility(returns: pd.Series, *, periods_per_year: int) -> float

# risk.py
@dataclass(frozen=True, slots=True)
class RiskMetrics:
    sharpe: float; sortino: float; calmar: float
    max_drawdown: float; max_drawdown_days: int; longest_drawdown_days: int
    var_95: float; es_95: float; var_99: float; es_99: float
def compute_risk_metrics(nav, returns, config) -> RiskMetrics
def drawdown_series(nav: pd.Series) -> pd.Series
def drawdown_table(nav: pd.Series, *, top: int = 10) -> pd.DataFrame

# relative.py
@dataclass(frozen=True, slots=True)
class RelativeMetrics:
    benchmark: str; beta: float; alpha_annual: float; r_squared: float
    tracking_error: float; information_ratio: float
    excess_annual_geometric: float; excess_annual_arithmetic: float
def compute_relative(returns, bench_returns, config) -> RelativeMetrics

# trading.py
@dataclass(frozen=True, slots=True)
class TradingMetrics:
    turnover_annual_one_way: float; turnover_annual_two_way: float
    n_trades: int; avg_holding_days: float; win_rate: float
    cost_total: float; cost_detail: dict[str, float]
    cost_to_nav: float; annual_cost_rate: float
def compute_trading_metrics(result: BacktestResult, equity: pd.Series) -> TradingMetrics

# capacity.py
@dataclass(frozen=True, slots=True)
class CapacityReport:
    capacity_amount: float; target_impact: float; achieved_impact: float
    by_participation: dict[float, dict[str, float]]
    limiting_symbol: str; warnings: tuple[str, ...]
def estimate_capacity(result, store, config) -> CapacityReport
def impact_cost_rate(aum: float, rebalances: Sequence[Rebalance], ...) -> float

# exposure.py
@dataclass(frozen=True, slots=True)
class ExposureReport:
    industry_avg: dict[str, float]; industry_last: dict[str, float]
    size_avg: float; size_percentile: float
    style: dict[str, float]                  # volatility / turnover / momentum
    by_day: pd.DataFrame
def compute_exposures(result, store, config) -> ExposureReport

# attribution.py
@dataclass(frozen=True, slots=True)
class AttributionReport:
    cost: dict[str, float]
    by_signal: pd.DataFrame                  # reason × (笔数/胜率/平均收益/贡献)
    benchmark_regression: dict[str, float]   # alpha/beta/r2/残差均值
    reconciliation: dict[str, float]         # 守恒校验结果
def attribute(result, store, benchmark_returns=None) -> AttributionReport

# sensitivity.py / splits.py / montecarlo.py
def run_scenarios(runner, scenarios: Sequence[CostScenario], base: BaseConfig) -> SensitivityReport
def split_sample(runner, *, in_sample, out_sample, base) -> SplitReport
def roll_windows(runner, *, window, step, base) -> RollingReport
def montecarlo(returns, *, paths, block, seed, ruin_threshold) -> MCReport

# engine.py
class MetricsEngine:
    def __init__(self, config: MetricsConfig | Mapping | None = None) -> None
    def compute(self, result, *, store, benchmark_navs=None) -> MetricsReport
    def with_sensitivity(self, result, *, runner, store, scenarios) -> MetricsReport
    def with_splits(self, *, runner, in_sample, out_sample) -> MetricsReport
    def with_rolling(self, *, runner, window, step) -> MetricsReport
    def with_montecarlo(self, result) -> MetricsReport
```

`MetricsReport`（纯数据、可 JSON 序列化）字段见 `docs/10_round2_design.md` §3.2。

---

## 8. 配置（`configs/base.yaml` 的 `metrics:` 段）

```yaml
metrics:
  risk_free_rate: 0.02
  trading_days_per_year: 252
  confidence_levels: [0.95, 0.99]
  benchmarks: ["000300.SH", "000905.SH", "000852.SH"]
  sortino_mar: null                # null = 使用日频无风险利率
  capacity:
    participation: [0.05, 0.10, 0.20]
    target_impact: 0.02            # 可接受的年化冲击成本率
    bisection_tol: 1.0e-6
  montecarlo:
    paths: 1000
    block: 20
    seed: 20240101
    ruin_threshold: 0.5            # 净值跌破初始 50% 视为破产
  validity:                        # 「成本加倍后策略是否仍有效」的判定
    min_annual_return: 0.0
    min_information_ratio: 0.3
    max_drawdown: 0.30
```

---

## 9. 伪代码（编排）

```
compute(result, store, benchmark_navs):
    nav     = result.equity_curve["total_value"]
    rets    = daily_returns(nav)
    bench   = benchmark_navs or load_benchmarks(store, config.benchmarks)   # PIT：基准取区间内行情
    report  = MetricsReport(
        period   = make_period(nav),
        returns  = compute_returns(nav, rets, config),
        risk     = compute_risk_metrics(nav, rets, config),
        relative = {name: compute_relative(rets, b, config) for name, b in bench.items()},
        trading  = compute_trading_metrics(result, nav),
        capacity = estimate_capacity(result, store, config),
        exposures= compute_exposures(result, store, config),
        attribution = attribute(result, store, primary_benchmark),
    )
    assert_reconciliation(report)          # 守恒校验（归因），失败即抛 MetricsError
    return report
```

---

## 10. 测试计划（`tests/test_metrics_*.py`，预计 45 用例）

| 文件 | 用例要点 |
|---|---|
| `test_metrics_returns.py` | 手算年化/波动；单日/零波动边界；`n<2` 告警；几何 vs 算术口径差异 |
| `test_metrics_risk.py` | 夏普/Sortino 手算；MDD 手算（含持续时间与恢复点）；Calmar；VaR/ES 与 `risk.var` 一致性 |
| `test_metrics_relative.py` | Beta=1（组合=基准）→ α≈0、IR 极端值处理；β=0（组合与基准无关）；跟踪误差手算 |
| `test_metrics_trading.py` | 换手率手算；成本明细求和 == `account.total_costs`；成本后 < 成本前 |
| `test_metrics_capacity.py` | 容量单调性；二分收敛与容差；参与率敏感性表自洽；`ADV=0` 的降级 |
| `test_metrics_exposure.py` | 行业占比求和 == 1（或 ≤1 含现金）；市值加权正确；缺字段时降级为 warning |
| `test_metrics_attribution.py` | 三项守恒校验；信号分组贡献可加性；基准回归 r²∈[0,1] |
| `test_metrics_sensitivity.py` | 场景重跑使用同一 seed 可复现；成本加倍后收益不升；有效性判定阈值生效 |
| `test_metrics_splits.py` | 样本外衰减计算；滚动窗口覆盖完整、无重叠、窗口数正确 |
| `test_metrics_montecarlo.py` | 同 seed 结果完全一致；路径数生效；block=1 等价 IID；破产概率 ∈ [0,1] |
| `test_metrics_engine.py` | 端到端：合成数据回测 → 报告字段完整、JSON 可序列化、`MetricsEngine` 不修改 `BacktestResult` |

**关键负面用例（必须写）**：
1. 传入空净值序列 → 明确报错而非返回 0；
2. 基准与组合日期不重叠 → 抛 `InsufficientDataError`（不做静默对齐）；
3. `runner` 抛异常时敏感性分析整体失败并保留已完成场景（不吞异常）；
4. `MetricsEngine.compute` 前后对 `result.equity_curve` 做深比较，证明**未改动回测路径**。

---

## 11. 待确认的口径（需要用户拍板）

| # | 议题 | 候选 | 建议 |
|---|---|---|---|
| 1 | 无风险利率 | 0（学术常见）/ 1.5% / 2%（10Y 国债近似） | 配置化，默认 **2%**，报告写明 |
| 2 | 年化方式 | 几何（复利）/ 算术 | **几何**，同时输出算术供对照 |
| 3 | 换手率口径 | 单边年化 / 双边年化 | 默认**单边年化**，两种都输出 |
| 4 | 容量定义 | 冲击成本阈值法（本设计）/ 收益衰减法 | **冲击成本阈值法**（可复现、可解释） |
| 5 | 「成本加倍后仍有效」判定 | 年化>0 且 IR>0.3 且 MDD<30% | 采用，阈值进配置 |
| 6 | 基准序列来源 | `index_zh_a_hist`（价格指数，不含分红）/ 全收益指数 | 先用**价格指数**并在报告标注口径 |
