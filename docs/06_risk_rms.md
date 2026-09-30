# M5 风控 RMS 设计：接口 / 伪代码 / 测试用例

> **状态：已实现并通过验收（73 个风控相关用例全绿）。**
> 对应代码：`src/aqs/risk/`（`base.py` / `rules.py` / `engine.py` / `var.py` / `registry.py` / `stats.py`）
> 对应配置：`configs/risk.yaml`
> 验收门禁：`tests/test_risk_*.py` 全绿。

---

## 1. 合同要求的接口

```python
RiskDecision CheckOrder(Order, Account, MarketSnapshot)

返回：
- accept               （由 action 派生：allow / reduce 视为通过）
- reject_reason
- modified_quantity
- action: allow / reject / reduce / pause / force_close
```

本项目的实现对应 `RiskEngine.check_order(order, account, snapshot)`，
并保留 `CheckOrder` 作为同名别名（便于与合同术语逐字对齐）。

**订单级接口之外还需要的三个维度**（否则规则无法落地）：

| 维度 | 接口 | 用途 |
|---|---|---|
| 日内状态 | `on_day_start(day, account)` / `on_day_end(day, account)` | 当日下单次数、当日亏损、回撤阈值 |
| 订单生命周期 | `on_order_submitted` / `on_order_cancelled` / `on_fill` | 撤单率、当日累计成交量 |
| 组合级动作 | `evaluate_controls(account, snapshot)` / `target_exposure_scale()` / `is_paused` / `force_close_requested` | 无订单日也要检查回撤与当日亏损；减仓、暂停、强平 |

> **为什么需要 `evaluate_controls`**：一个只在「下单时」被调用的 RMS 会漏掉真正的账户级风险 ——
> 当天没有任何订单时，回撤与当日亏损规则根本不会被评估。引擎在每日盘后**独立调用一次**，
> 只执行标记为 `portfolio_level = True` 的规则（回撤 / 当日亏损 / 撤单率 / 下单频率）。

---

## 2. 规则模型

```python
class RiskRule(ABC):
    name: str
    priority: int                     # 数值越小越先执行
    action: str                       # 配置声明的动作（可与规则的自然动作不同）
    def check(self, ctx: RiskContext) -> RiskDecision

@dataclass(slots=True)
class RiskContext:
    order: Order
    account: Account
    snapshot: MarketSnapshot
    day: DayState                     # 日内计数与基准
    engine: EngineState                # 峰值净值、当前敞口、暂停状态
    industry_of: Callable[[str], str | None] | None
    returns_of: Callable[[str, date, int], Sequence[float]] | None   # VaR 用
```

### 2.1 引擎聚合逻辑（含优先级与短路）

```
decision = ALLOW, qty = order.remaining
for rule in sorted(rules, key=priority):
    d = rule.check(ctx)
    if d.action == ALLOW:  continue
    if d.action == REDUCE:
        qty = min(qty, d.modified_quantity)
        if qty <= 0: return REJECT(rule)
        record(d); continue
    if d.action == REJECT:      return REJECT(rule, reason)
    if d.action == PAUSE:       engine.pause(rule);      return PAUSE(rule)
    if d.action == FORCE_CLOSE: engine.request_force_close(rule); return FORCE_CLOSE(rule)
return REDUCE(qty) if qty < order.remaining else ALLOW
```

规则按优先级执行，`reject/pause/force_close` 立即短路，`reduce` 会累积（取最严格者）。

### 2.2 规则清单（12 条，对应合同 §七）

| # | 规则 | 默认优先级 | 动作 | 判定 |
|---|---|---|---|---|
| 1 | `max_order_quantity` | 10 | reduce | 单笔订单股数 > 上限 → 削减 |
| 2 | `max_order_notional` | 11 | reduce | 单笔订单金额 > 上限 → 削减 |
| 3 | `price_deviation` | 20 | reject | 限价相对最新价偏离 > 阈值；市价单按当日涨跌停区间校验 |
| 4 | `order_frequency` | 30 | reject | 当日下单笔数 > 上限（分钟级限制在日频模式下不适用，见 §5 限制） |
| 5 | `cancel_ratio` | 31 | pause | 当日撤单率 > 阈值且样本足够 → 暂停当日交易 |
| 6 | `max_trade_volume_daily` | 40 | reduce | 当日累计成交量 + 本单 > ADV × 比例 → 削减 |
| 7 | `max_position_per_symbol` | 50 | reduce | 成交后单票市值占比 > 上限 → 削减 |
| 8 | `industry_exposure` | 60 | reduce | 成交后行业市值占比 > 上限 → 削减 |
| 9 | `gross_exposure` | 70 | reject | 成交后总市值敞口 > 上限；禁止卖空 |
| 10 | `cash_sufficiency` | 80 | reduce | 成交后现金比例 < 最低要求 → 削减 |
| 11 | `daily_loss_limit` | 90 | pause | 当日亏损 > 阈值 → 暂停当日新开仓 |
| 12 | `max_drawdown_action` | 100 | force_close | 回撤分档：预警 / 减仓至半仓 / 强制平仓并停止 |
| 13 | `portfolio_var_limit` | 110 | reduce | 成交后组合 1 日 VaR（历史模拟）> 上限 → 削减 |

**减仓与强平如何落地**：RMS 不直接卖股票（那是组合层的职责），而是把状态交给引擎：

```
target_exposure_scale():
    1.0  正常
    0.5  回撤 ∈ [reduce_drawdown, force_close_drawdown)
    0.0  回撤 >= force_close_drawdown 或已请求强平
```

引擎在盘后按该比例生成减仓订单（卖出 `持仓 × (1 - scale)` 的可卖部分），
`scale = 0` 时清仓全部持仓并停止开新仓。

---

## 3. VaR / ES（历史模拟法，第一阶段必须实现）

```python
def historical_var_es(returns, *, confidence=0.95, horizon=1) -> VaRResult
    # VaR = -quantile(returns, 1 - confidence) × sqrt(horizon)
    # ES  = -mean(returns[returns <= quantile])
def portfolio_returns(weights, returns_matrix) -> np.ndarray
def var_breach_rate(returns, *, confidence, window) -> BreachResult   # 滚动 VaR 验收
```

验收：滚动回测的**突破率 ≈ 1 - 置信度**（95% VaR 的突破率应接近 5%）。

---

## 4. RMS 验收指标（合同 §九）

| 指标 | 定义 | 实现 |
|---|---|---|
| 拒单率 | 被拒订单数 / 提交订单数 | `RiskStatsCollector.reject_rate` |
| 误杀率 | 被拒订单中，事后 N 个交易日按原方向为盈利的比例 | `evaluate_rejections(rejected, store, horizon=5)` |
| 触发延迟 | 规则条件首次满足日 → 动作生效日 的交易日数 | `RiskTrigger.latency_days`（由引擎记录） |

误杀率的判定口径（必须写进报告）：被拒买单若之后 N 日上涨 → 记为「误杀」；
被拒卖单若之后 N 日下跌 → 记为「误杀」。**这只是事后视角的近似指标，不代表风控做错了**
（当时的信息集不同），用于评估阈值是否过严。

---

## 5. 已知限制（诚实声明）

1. **分钟级频率限制**在日频回测中无法真实生效：`max_orders_per_minute` 只在日频模式下退化为
   当日总量检查（`max_orders_per_day` 才是有效约束）；真正的分钟级限制需要第三阶段的 Tick 回放。
2. `industry_exposure` 依赖行业分类数据；未提供行业映射时该规则自动跳过并在审计中记录。
3. `portfolio_var_limit` 依赖历史收益序列；窗口不足时跳过（记录 `skipped` 原因），不静默放行。
4. 强平/减仓在 T+1 开盘执行（受涨跌停/停牌约束，可能被顺延）—— RMS 的「立即」在日频语义下是「次日开盘」。

---

## 6. 测试用例

> 实际执行结果：`test_risk_var` 16 / `test_risk_rules` 18 / `test_risk_engine` 19 /
> `test_risk_stats` 10 / `test_risk_engine_integration` 10 —— **共 73 个，全部通过**。

### 6.1 VaR/ES（`tests/test_risk_var.py`）

| # | 用例 | 断言 |
|---|---|---|
| V1 | 历史模拟 VaR 手算 | 与手算分位数一致 |
| V2 | ES ≤ VaR | 尾部均值不小于 VaR |
| V3 | 持有期缩放 | `horizon=4` 时 VaR = 1 日 VaR × 2 |
| V4 | 置信度单调性 | 99% VaR ≥ 95% VaR |
| V5 | 组合收益 | 权重加权一致；权重和不为 1 时抛错 |
| V6 | **突破率** | 滚动 VaR 的突破率 ∈ [3%, 8%]（95% 置信度） |
| V7 | 样本不足 | 抛 `InsufficientDataError` |

### 6.2 规则（`tests/test_risk_rules.py`）

逐条规则至少一个「触发」与一个「不触发」用例，另加边界与缺失数据情形（共 24 条）。

### 6.3 规则引擎（`tests/test_risk_engine.py`）

| # | 用例 | 断言 |
|---|---|---|
| E1 | 优先级顺序 | 先执行的规则名记录在审计中 |
| E2 | `reject` 短路 | 后续规则不再执行 |
| E3 | `reduce` 累积 | 两条 reduce 规则取最小值 |
| E4 | `reduce` 后为 0 → 拒单 | reject |
| E5 | `pause` | `is_paused=True`，拒绝新订单 |
| E6 | `force_close` | `force_close_requested=True`，`target_exposure_scale()=0` |
| E7 | 回撤分档 | 10%/15%/20% → 1.0 / 0.5 / 0.0 |
| E8 | 审计日志 | 每条决策可追溯（规则名/动作/数量） |
| E9 | **热更新** | `update_rules` 后新规则立即生效，旧规则消失 |
| E10 | 实时预警 | 触发 pause/force_close 时产生 alert |
| E11 | observe 模式 | 只记录不拦截 |
| E12 | 空规则集 | 一律放行 |

### 6.4 RMS 指标（`tests/test_risk_stats.py`）

| # | 用例 | 断言 |
|---|---|---|
| S1 | 拒单率 | 与手算一致 |
| S2 | 分规则拒单统计 | 各规则计数正确 |
| S3 | 误杀率 | 用构造的被拒单 + 后续行情验证 |
| S4 | 触发延迟 | 日频下 pause/force_close 延迟 = 1 个交易日 |

### 6.5 引擎接入（`tests/test_risk_engine_integration.py`）

| # | 用例 | 断言 |
|---|---|---|
| I1 | 单笔订单上限 | 订单被削减，成交数量 ≤ 上限 |
| I2 | 当日亏损限制 | 触发后当日不再开新仓 |
| I3 | 最大回撤强平 | 触发后持仓清零、不再开仓 |
| I4 | 回撤减仓 | 触发后敞口降至约一半 |
| I5 | 单票上限 | 无订单使单票市值超过阈值 |
| I6 | 诊断输出 | `diagnostics` 含拒单率/误杀率/触发延迟 |
| I7 | 关闭风控 | `risk.enabled=false` → 一律放行 |
