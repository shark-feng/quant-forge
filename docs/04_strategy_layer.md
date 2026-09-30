# M3 策略层设计：接口 / 伪代码 / 测试用例

> **状态：已实现并通过验收（75 个策略相关用例全绿）。**
> 对应代码：`src/aqs/strategy/`（`indicators.py` / `ma_cross.py` / `breakout.py` / `volume.py` / `registry.py`）
> 对应配置：`configs/strategies/{ma_cross,breakout,volume}.yaml`
> 验收门禁：`tests/test_strategy_*.py` 全绿。

---

## 1. 职责边界（先划清）

| 策略层**做** | 策略层**不做** |
|---|---|
| 用 ≤T 的数据计算指标与信号 | 不下单、不做订单时间语义（引擎负责 T+1） |
| 输出 `SignalIntent`（方向 / 分数 / 原因 / 证据） | 不查询资金、不计算仓位（组合层 M4） |
| 只遍历 `ctx.universe`（已由数据层过滤） | 不重复实现 ST/上市天数/流动性过滤 |
| 参数全部来自配置，非法参数立即报错 | 不做风控判断（RMS M5） |

**数据入口**：`ctx.data` 是 `PITView`，任何 `> T` 的取数都会抛 `LookaheadError`。
**执行时点**：T 日收盘后出信号，引擎统一在 T+1 开盘撮合（`submit_date = next_trading_day(T)`）。

---

## 2. 数据结构与接口

### 2.1 信号（沿用 M0 定义）

```python
@dataclass(frozen=True, slots=True)
class SignalIntent:
    symbol: str
    direction: SignalDirection      # ENTRY=+1 / EXIT=-1
    signal_date: date               # 信号产生日 T
    score: float = 0.0              # 供组合层排序（越大越优先）
    target_weight: float | None = None
    reason: str = ""                # golden_cross / breakout_high / volume_up ...
    meta: Mapping[str, Any] = {}    # 证据：指标数值，供报告层归因
```

### 2.2 策略基类

```python
class BaseStrategy(ABC):
    name: str
    def __init__(self, params: Mapping[str, Any] | None = None) -> None
    @property
    def min_history(self) -> int            # 需要的最小历史 K 线数
    @abstractmethod
    def on_bar(self, ctx: StrategyContext) -> Sequence[SignalIntent]
    def prepare(self, ctx: StrategyContext) -> None      # 可选钩子
```

参数校验规则（构造时执行，非法即 `ConfigError`）：
- 窗口类参数必须 `>= 2`；
- MA 快线必须 `<` 慢线；
- `price_field` 必须是数据层真实存在的列（`close_adj` / `close` / `high_adj` / …）；
- **未知参数键直接报错**（防止拼写错误导致参数静默失效）。

### 2.3 指标层（纯函数，全部 `shift/offset` 语义显式）

```python
# 尾部窗口版（策略逐日使用，O(window × symbols)）
def tail_window(frame, window, *, offset=0) -> DataFrame      # offset=1 → 不含最后一行
def tail_mean(frame, window, *, offset=0) -> Series
def tail_max(frame, window, *, offset=0) -> Series
def tail_min(frame, window, *, offset=0) -> Series

# 整段序列版（用于研究/作图/一致性测试）
def sma(frame, window, *, min_periods=None) -> DataFrame
def rolling_max(frame, window, *, exclude_current=True) -> DataFrame
def rolling_min(frame, window, *, exclude_current=True) -> DataFrame
def volume_ratio(volume, window, *, exclude_current=True) -> DataFrame
def cross_over(fast, slow) -> DataFrame[bool]     # (fast>slow) & (fast.shift(1)<=slow.shift(1))
def cross_under(fast, slow) -> DataFrame[bool]
```

**一致性约束**：`tail_mean(frame, w, offset=0)` 必须等于 `sma(frame, w).iloc[-1]`；
`tail_max(frame, w, offset=1)` 必须等于 `rolling_max(frame, w).iloc[-1]`。
测试用显式用例锁死（见 T-I4、T-I5）。

NaN 语义：窗口内出现 `NaN`（标的缺失行情）→ 该标的当日结果 `NaN` → **不产生信号**。

### 2.4 注册表与配置驱动构建

```python
STRATEGY_REGISTRY: dict[str, type[BaseStrategy]]

@dataclass(slots=True)
class StrategySpec:                     # 一个策略配置文件的完整语义
    name: str
    params: dict[str, Any]
    filters: dict[str, Any]             # → 数据层股票池口径
    execution: dict[str, Any]           # → 引擎撮合口径（M2 已实现）
    portfolio: dict[str, Any]           # → 组合层口径（M4）
    signal: dict[str, Any]              # 人类可读的信号表达式（仅文档用途）

def build_strategy(spec_or_mapping) -> BaseStrategy
def load_strategy(path: str | Path) -> BaseStrategy
def load_spec(path: str | Path) -> StrategySpec
def filters_to_data_overrides(filters: Mapping) -> dict[str, Any]   # → DataConfig 覆盖
```

---

## 3. 三种策略的精确定义（对齐合同 §四）

> 统一约定：价格为**后复权价**（`close_adj` 等），成交/涨跌停/费用仍走原始价（M2 负责）。
> 股票池已剔除 ST、停牌、上市不足 60 个交易日、20 日日均成交额 < 5000 万的标的。

### 3.1 均线交叉（`ma_cross`）

```
fast[T]  = mean(close_adj[T-fast+1 .. T])
slow[T]  = mean(close_adj[T-slow+1 .. T])          # 默认 fast=5, slow=20
买入(ENTRY): fast[T] > slow[T] 且 fast[T-1] <= slow[T-1]      # 金叉当日
卖出(EXIT) : fast[T] < slow[T] 且 fast[T-1] >= slow[T-1]      # 死叉当日
score = (fast[T] - slow[T]) / slow[T]                          # 乖离率，供组合层排序
min_history = slow + 1
```

### 3.2 价格突破（`breakout`）

```
prior_high[T] = max(high_adj[T-window .. T-1])     # 不含 T 日，默认 window=20
prior_low[T]  = min(low_adj[T-window .. T-1])
买入(ENTRY): close_adj[T] > prior_high[T]          # 严格大于
卖出(EXIT) : close_adj[T] < prior_low[T]
可选确认   : confirm_volume=True 时还要求 volume[T] > MA(volume, volume_window)[T-1] × 阈值
score      : 突破 (close/prior_high - 1)；跌破 (close/prior_low - 1)（负值）
min_history = window + 1
```

`exclude_today` 必须为 `True`（合同明确「不含 T 日」）；配置为 `False` 时构造即报错 —— 避免把未来函数写进配置。

### 3.3 成交量配合（`volume`）

```
vol_ma[T] = mean(volume[T-window .. T-1])          # 过去 window 日均量，**不含 T 日**
放量     : volume[T] > vol_ma[T] × ratio           # 默认 window=20, ratio=1.5
买入(ENTRY): close_adj[T] > close_adj[T-1] 且 放量
卖出(EXIT) : close_adj[T] < close_adj[T-1] 且 放量
score = volume[T] / vol_ma[T]                       # 量比
min_history = window + 2
```

> **口径说明**：合同写「过去 20 日平均成交量」，因此基准**不含当日**。
> 若含当日，放量日会抬高基准、削弱信号（常见实现陷阱）。
> 该口径由参数 `volume_ma_exclude_today`（默认 `true`）控制，并有专门测试锁定。

---

## 4. 伪代码（以均线交叉为例）

```
on_bar(ctx):
    symbols = ctx.universe                     # 数据层已过滤，无需重复过滤
    if not symbols: return []
    panel = ctx.panel(min_history, price_field, symbols)   # PIT：只含 ≤ T 的行
    if panel.shape[0] < min_history: return []             # 历史不足 → 不出信号

    fast      = tail_mean(panel, fast_window, offset=0)
    fast_prev = tail_mean(panel, fast_window, offset=1)
    slow      = tail_mean(panel, slow_window, offset=0)
    slow_prev = tail_mean(panel, slow_window, offset=1)

    signals = []
    for sym in panel.columns:
        if any of (fast, fast_prev, slow, slow_prev) is NaN: continue   # 数据不足/缺失
        if fast > slow and fast_prev <= slow_prev:
            signals.append(SignalIntent(sym, ENTRY, ctx.date, score=(fast-slow)/slow,
                                        reason="golden_cross", meta={...}))
        elif fast < slow and fast_prev >= slow_prev:
            signals.append(SignalIntent(sym, EXIT, ctx.date, score=(fast-slow)/slow,
                                        reason="dead_cross", meta={...}))
    return signals
```

---

## 5. 测试用例

> 实际执行结果：`test_strategy_indicators` 17 / `test_strategy_ma_cross` 14 /
> `test_strategy_breakout` 13 / `test_strategy_volume` 13 / `test_strategy_registry` 12 /
> `test_strategy_engine` 6 —— **共 75 个，全部通过**。
>
> 说明：三种策略的判定逻辑都用**独立实现的纯 Python 循环 oracle** 逐日比对
> （不使用被测的 pandas 指标代码），避免「用被测代码验证被测代码」。

### 5.1 指标层（`tests/test_strategy_indicators.py`）

| # | 用例 | 断言 |
|---|---|---|
| I1 | `tail_mean` 手算 | 与手工均值一致；`offset=1` 不含最后一行 |
| I2 | `tail_max/tail_min` 手算 | 与手工极值一致 |
| I3 | 窗口不足 | 返回 `NaN`（不抛异常、不静默截断） |
| I4 | **一致性**：`tail_mean(panel,w)` == `sma(panel,w).iloc[-1]` | 逐列相等 |
| I5 | **一致性**：`tail_max(panel,w,offset=1)` == `rolling_max(panel,w,exclude_current=True).iloc[-1]` | 逐列相等 |
| I6 | 含 NaN 的窗口 | 结果为 `NaN`（严格 skipna=False） |
| I7 | `cross_over/cross_under` | 只在交叉当日为 `True`，次日为 `False` |
| I8 | `volume_ratio` | `exclude_current=True` 时基准不含当日 |

### 5.2 均线交叉（`tests/test_strategy_ma_cross.py`）

| # | 用例 | 断言 |
|---|---|---|
| A1 | 金叉当日出 ENTRY | 恰好 1 条、`direction=ENTRY`、`signal_date=T` |
| A2 | 金叉前一日不出信号 | 前一日无 ENTRY |
| A3 | 死叉当日出 EXIT | 方向为 EXIT |
| A4 | 历史不足 | 返回空列表（不报错） |
| A5 | 参数化 | `fast=3, slow=10` 生效（信号日随参数移动） |
| A6 | 非法参数 | `fast >= slow`、窗口 < 2 → `ConfigError` |
| A7 | 未知参数键 | `ConfigError`（防拼写错误） |
| A8 | 只对 `ctx.universe` 内的标的出信号 | 池外标的即使金叉也无信号 |
| A9 | `score` 数值 | `(fast-slow)/slow`，金叉为正 |
| A10 | 停牌/缺失行情标的 | 该标的当日无信号 |

### 5.3 价格突破（`tests/test_strategy_breakout.py`）

| # | 用例 | 断言 |
|---|---|---|
| B1 | 收盘价高于前 20 日最高价 → ENTRY | 1 条；`meta.prior_high` 等于手工值 |
| B2 | **等于**前高 → 不触发 | 严格大于 |
| B3 | 跌破前 20 日最低价 → EXIT | 1 条 |
| B4 | 当日创新高但当日 high 才是最高 → 不触发（不含 T 日） | 证明无未来函数 |
| B5 | `exclude_today=False` | 构造即 `ConfigError` |
| B6 | `confirm_volume=True` | 未放量时不出信号；放量时出信号 |
| B7 | 历史不足 | 空列表 |

### 5.4 成交量配合（`tests/test_strategy_volume.py`）

| # | 用例 | 断言 |
|---|---|---|
| C1 | 放量上涨 → ENTRY | 1 条、`score` 等于量比 |
| C2 | 放量下跌 → EXIT | 1 条 |
| C3 | 缩量上涨 → 无信号 | — |
| C4 | 量比恰好等于阈值 → 不触发 | 严格大于 |
| C5 | 基准不含当日 | 当日巨量不会抬高自身基准（对比含当日口径的差异） |
| C6 | `volume_ma_exclude_today=False` | 口径切换后基准改变（可复现） |

### 5.5 注册表与配置（`tests/test_strategy_registry.py`）

| # | 用例 | 断言 |
|---|---|---|
| R1 | 三个内置策略都可注册与构建 | `available_strategies()` 含三个名字 |
| R2 | 从 `configs/strategies/*.yaml` 加载 | 参数与文件一致 |
| R3 | 未知策略名 | `ConfigError` |
| R4 | `filters_to_data_overrides` | 正确映射到 `DataConfig` 字段 |
| R5 | `StrategySpec` 解析 | `filters/execution/portfolio/signal` 段都能读到 |
| R6 | 无效 YAML 段 | `ConfigError` |

### 5.6 引擎联调（`tests/test_strategy_engine.py`）

| # | 用例 | 断言 |
|---|---|---|
| G1 | 合成数据 + 均线策略跑完整回测 | 有成交，事件序列正确 |
| G2 | 信号日与成交日 | 成交日 == `next_trading_day(信号日)` |
| G3 | 策略无法访问未来数据 | 越界访问抛 `LookaheadError` |
| G4 | 三种策略都能跑通 | 各自产生 ≥ 1 条信号 |
| G5 | 参数化回测可复现 | 同种子两次结果完全一致 |

---

## 6. 与其它模块的接口

| 下游 | 消费什么 | 说明 |
|---|---|---|
| M4 组合层 | `SignalIntent.score` / `direction` | 按 score 排序选前 N，等权/半凯利分配 |
| M5 风控 | — | 策略不参与风控 |
| M2 引擎 | `StrategyOnBarProtocol` | `BacktestEngine(strategy=...)`，订单时间语义由引擎强制 |
| M6/M7 报告 | `SignalIntent.meta` | 指标数值可用于信号归因 |
