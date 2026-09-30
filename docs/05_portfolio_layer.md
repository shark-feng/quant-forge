# M4 组合层设计：接口 / 伪代码 / 测试用例

> **状态：已实现并通过验收（62 个组合相关用例全绿）。**
> 对应代码：`src/aqs/portfolio/`（`base.py` / `sizing.py` / `target_weight.py` / `registry.py`）
> 对应配置：`configs/base.yaml` 的 `portfolio:` 段、各策略文件中的 `portfolio:` 段
> 验收门禁：`tests/test_portfolio_*.py` 全绿。

---

## 1. 职责边界

| 组合层**做** | 组合层**不做** |
|---|---|
| 信号 → **目标权重** → 订单计划（`OrderPlan`） | 不下单、不决定信号日/成交日（引擎负责 T+1） |
| 仓位约束：等权、单票上限、最大持仓数、现金缓冲 | 不做风控否决（RMS 在订单提交前独立检查） |
| 仓位总量：凯利公式简化版（半凯利） | 不修改策略信号（只做排序与取舍） |
| 卖出/清仓/调仓逻辑（含 T+1 可卖约束） | 不做均值-方差优化（第二阶段 M8） |

**关键约定**：组合层只产出 `OrderPlan`（symbol/side/quantity），
订单的 `signal_date / submit_date` 由引擎赋值，T+1 语义只在一个地方强制，
组合层无法绕过。

---

## 2. 数据结构

```python
@dataclass(frozen=True, slots=True)
class TargetWeight:
    symbol: str
    weight: float            # 占总资产的目标比例
    score: float = 0.0
    reason: str = ""

@dataclass(frozen=True, slots=True)
class OrderPlan:
    symbol: str
    side: Side
    quantity: float
    order_type: OrderType = MARKET
    limit_price: float | None = None
    tag: str = ""            # entry / exit / rebalance / risk_reduce
    reason: str = ""
    meta: Mapping[str, Any] = {}

@dataclass(slots=True)
class SizingConfig:
    weighting: str = "equal"          # equal | score_prop（凯利用于控制**总敞口**）
    max_positions: int = 10
    max_weight_per_symbol: float = 0.10
    cash_buffer: float = 0.02          # 现金缓冲（不投入交易）
    allow_reentry: bool = True         # 卖出后是否允许再次买入同一标的
    rebalance: bool = False            # True=目标集合由当日信号决定；False=仅信号驱动卖出
    lot_size: int = 100
    kelly: KellySizing = KellySizing() # 凯利总敞口控制
```

> **权重方式与凯利的分工**：`weighting` 决定**如何在候选之间分配**（等权 / 按分数），
> `kelly.enabled` 决定**总敞口**是多少；两者相乘得到每个新仓位的目标权重，再受单票上限约束。

---

## 3. 仓位计算（`sizing.py`）

全部是**纯函数**，输入信号与账户状态，输出权重，便于单独测试。

### 3.1 等权 + 单票上限

```
有效敞口 E = (1 - cash_buffer) × kelly_exposure
n = 目标持仓总数（含已有持仓）
w_i = min(E / n, max_weight_per_symbol)
```

上限削下来的部分**不重新分配**（保守：宁可留现金，也不放大个股风险）。

### 3.2 分数加权（score_prop）

```
raw_i ∝ max(score_i, 0)          # 只在新建仓候选之间分配
w_i = min(raw_i × E, max_weight_per_symbol)
```

score 全为非正时退化为等权。

### 3.3 凯利公式简化版（半凯利，第一阶段必须实现）

**二元版（本项目默认）**：

```
f* = p - q / b            p = 胜率，q = 1 - p，b = 赔率（平均盈利 / 平均亏损）
敞口 = clip(half_kelly × f*, 0, cap)
```

**连续版（研究用途，可切换）**：

```
f* = μ / σ²               μ、σ 为期望收益与方差（同一频率）
f  = half_kelly × f*
```

统计量来源：账户中**已平仓交易**的已实现收益率序列（`Account.closed_trades`）。

- 胜率 `p` = 盈利平仓笔数 / 总平仓笔数
- 赔率 `b` = 平均盈利 / |平均亏损|（亏损为空时取下限 1.0）
- **样本不足**（少于 `min_trades`，默认 20 笔）时使用 `initial_exposure`，
  并在 `describe()["last_kelly"]` 中标注「样本不足」—— 避免用 3 笔交易算出满仓。

| 参数 | 默认 | 说明 |
|---|---|---|
| `mode` | binary | binary（胜率/赔率）或 continuous（μ/σ²） |
| `fraction` | 0.5 | 半凯利系数 |
| `cap` | 1.0 | 总敞口上限 |
| `min_trades` | 20 | 启用凯利所需的最小平仓样本数 |
| `initial_exposure` | 1.0 | 样本不足时的敞口 |

**验收**：`f*` 与手算一致；敞口 ≤ cap；单票权重 ≤ `max_weight_per_symbol`。

---

## 4. 组合实现（`target_weight.py`）

```
generate_orders(signals, ctx):
    # ① 退出信号 → 清仓（受 T+1 可卖量约束）
    for s in EXIT 信号且持有: 生成 SELL(sellable(s))    # 可卖量为 0 → 不生成（引擎无法卖）

    # ② 目标持仓集合（名额口径保守：持仓 + 在途买单都占名额，卖出未成交不释放名额）
    held_like = 当前持仓 ∪ 在途买单标的
    slots = max_positions - len(held_like)
    new_candidates = [ENTRY 信号 且 不在 held_like 且 (allow_reentry 或 首次交易)]
    new_candidates.sort(by score desc)
    new_targets = new_candidates[:slots]
    if rebalance:                                     # 当日无 ENTRY 信号的老持仓全部卖出
        for sym in 持仓 and sym not in 今日 ENTRY 信号: 生成 SELL(sellable(sym))

    # ③ 可投资金：现金 + 预计卖出回款，扣除现金缓冲
    investable = (cash + 预计卖出回款) × (1 - cash_buffer)

    # ④ 目标权重：有效敞口 E = (1 - cash_buffer) × kelly_exposure
    for sym in new_targets:
        w = min(E / n_target, max_weight) 或 min(score占比 × E, max_weight)
        budget = min(w × total_value, investable 剩余)
        qty = floor(budget / price / lot) × lot
        if qty >= lot: 生成 BUY(qty)

    # ⑤ 顺序：先卖后买（卖出回款在 T+1 开盘先到账，买单才能用上）
    return sells + buys
```

**顺序很关键**：引擎在 T+1 开盘按提交顺序（FIFO）撮合，
卖单先成交 → 释放现金 → 买单再用；若买单在前，会被 `affordable_quantity` 削减。

**「仅信号驱动卖出」是默认行为**：策略只在交叉/跌破当日发 EXIT，
若每天都把「当日没有 ENTRY 信号」的持仓卖掉，会造成无意义的高换手。
`rebalance=True` 时目标集合完全由当日信号决定（老持仓会被卖出，换手显著提高）。

**为什么名额口径是保守的**：涨停/停牌会让卖单顺延，若卖出信号一发出就释放名额，
「顺延未成交的卖单 + 同日新开仓」会让持仓数突破 `max_positions`。
因此名额只在持仓真正消失（卖出成交）后的下一个交易日释放。

---

## 5. 配置驱动构建（`registry.py`）

```python
PORTFOLIO_REGISTRY: dict[str, type[BasePortfolio]]
def build_portfolio(config: Mapping | PortfolioSpec) -> BasePortfolio
def load_portfolio(path) -> BasePortfolio           # 读取策略文件的 portfolio: 段
def sizing_from_params(params, *, lot_size, defaults) -> SizingConfig
```

`portfolio:` 段支持键：`weighting` / `max_positions` / `max_weight_per_symbol` /
`cash_buffer` / `allow_reentry` / `rebalance` / `kelly.{enabled,fraction,cap,min_trades}`。
**未知键直接报错**（与策略层一致）。

---

## 6. 测试用例

> 实际执行结果：`test_portfolio_sizing` 23 / `test_portfolio_target_weight` 20 /
> `test_portfolio_registry` 10 / `test_portfolio_engine` 9 —— **共 62 个，全部通过**。

### 6.1 仓位计算（`tests/test_portfolio_sizing.py`）

| # | 用例 | 断言 |
|---|---|---|
| S1 | 等权 | 5 个标的 → 每个 0.2 |
| S2 | 单票上限 | 20 个标的、上限 0.10、最多 10 只 → 10 × 0.10 |
| S3 | 最大持仓数截断 | 信号 15 条、上限 10 → 只取 score 前 10 |
| S4 | score 加权 | 权重与 score 成正比，且归一化 |
| S5 | score 全为 0 | 退化为等权 |
| S6 | 凯利二元版手算 | p=0.6, b=1.5 → f*=0.3333，半凯利 0.1667 |
| S7 | 凯利连续版手算 | μ=0.002, σ=0.02 → f*=5.0，受 cap 限制 |
| S8 | 半凯利系数 | fraction=0.25 → f = 0.25×f* |
| S9 | 敞口上限 | f* > cap → 取 cap |
| S10 | 无亏损样本 | 赔率取下限，不出现除零 |
| S11 | 样本不足 | 使用 `initial_exposure` 并置 `kelly_ready=False` |
| S12 | 非法参数 | 权重方式未知、上限越界 → `ConfigError` |

### 6.2 目标权重组合（`tests/test_portfolio_target_weight.py`）

| # | 用例 | 断言 |
|---|---|---|
| T1 | 买入信号 → 买单 | 数量为整手，金额 ≈ 权重 × 总资产 |
| T2 | 单票上限生效 | 目标市值 ≤ `max_weight × total_value` |
| T3 | 最大持仓数生效 | 新开仓数量使总持仓不超过 N |
| T4 | 现金缓冲 | 买入总额 ≤ 可用资金 × (1 - cash_buffer) |
| T5 | 卖出信号 → 清仓（可卖量内） | 数量 = `account.sellable` |
| T6 | T+1 不可卖 | 可卖量为 0 → 不生成卖单（不报错） |
| T7 | 已持有标的不重复买入 | 不产生同标的重复买单 |
| T8 | `allow_reentry=False` | 卖出后不再买入同一标的 |
| T9 | `rebalance=False`（默认） | 当日无 ENTRY 信号的老持仓不被卖出（低换手） |
| T10 | `rebalance=True` | 目标外持仓被卖出 |
| T11 | 卖单在买单之前 | `plans` 中所有 SELL 的索引 < BUY |
| T12 | 无信号 | 返回空列表 |
| T13 | 凯利敞口 | 半凯利敞口下买入总额相应缩小 |
| T14 | 价格缺失/停牌标的 | 跳过，不产生订单 |

### 6.3 注册表（`tests/test_portfolio_registry.py`）

| # | 用例 | 断言 |
|---|---|---|
| R1 | 三个策略文件的 `portfolio:` 段可构建 | 参数与文件一致 |
| R2 | 未知权重方式 | `ConfigError` |
| R3 | 未知键 | `ConfigError` |
| R4 | 缺省值来自 `base.yaml` 的 `portfolio:` 段 | 一致 |

### 6.4 引擎联调（`tests/test_portfolio_engine.py`）

| # | 用例 | 断言 |
|---|---|---|
| G1 | 正式组合层 + 策略 + 引擎端到端 | 有成交，持仓数 ≤ max_positions |
| G2 | 现金约束 | 全程现金 ≥ 0，买入总额 ≤ 初始资金 |
| G3 | 单票权重上限 | 每笔成交后单票市值占比 ≤ 上限 + 容差 |
| G4 | T+1 | 成交日 = 信号日 + 1 个交易日 |
| G5 | 换手可控 | 关闭 rebalance 时换手显著低于开启时 |
