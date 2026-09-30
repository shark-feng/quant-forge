# M2 回测引擎骨架设计：接口 / 伪代码 / 测试用例

> 对应代码：`src/aqs/engine/`（`cost.py` / `matching.py` / `broker.py` / `account.py` / `event_loop.py` / `backtest.py`）
> 验收门禁：`tests/test_events.py`、`test_cost.py`、`test_matching.py`、`test_engine_backtest.py` 全绿。

---

## 1. 设计目标

1. **事件驱动、可审计**：所有状态变化都由事件触发，事件顺序可录制并在测试中断言。
2. **A 股硬约束内置**：T+1、涨跌停、停牌、整手 100 股、无卖空、退市不可交易。
3. **时间规则不可绕过**：T 日收盘信号只能 T+1 成交；引擎在订单提交处做日期断言。
4. **成本模型可加倍**：所有费率/滑点/冲击参数来自配置，支持成本敏感性测试。
5. **撮合可替换**：`MatchingEngine` 是策略对象（市价/限价/参与率/TWAP/VWAP 子单），第三阶段可换成订单簿撮合。

---

## 2. 组件与职责

| 组件 | 文件 | 职责 |
|---|---|---|
| `EventLoop` | `event_loop.py` | 事件队列、按 `(ts, priority, seq)` 排序、处理器分发、事件录制、异常策略 |
| `CostModel` | `cost.py` | 佣金/印花税/过户费/滑点/冲击成本 |
| `MatchingEngine` | `matching.py` | 可成交性判定 + 成交价 + 成交量（含部分成交） |
| `Broker` | `broker.py` | 订单生命周期、风控回调、顺延、撤单、成交回报 |
| `Account`/`Portfolio` | `account.py` | 现金、持仓、T+1 可卖、成本、估值、已实现/未实现盈亏 |
| `BacktestEngine` | `backtest.py` | 日频主循环、事件发布顺序、策略/组合/风控装配 |

---

## 3. 撮合规则（`MatchingEngine`）

### 3.1 可成交性判定顺序（短路，先判先拒）

```
1. 数据存在？        无 Bar → NO_QUOTE（顺延）
2. 已退市？          date > delist_date → DELISTED（拒绝，不顺延）
3. 停牌？            is_suspended or volume <= 0 → SUSPENDED（顺延）
4. 涨跌停：          买入 && close/open 触及涨停 → LIMIT_UP（顺延）
                     卖出 && 触及跌停 → LIMIT_DOWN（顺延）
5. T+1：             卖出 && 可用数量不足 → T1_LOCK（削减数量，削减后为 0 则拒绝）
6. 整手：            买入数量向下取整到 100 股；不足 100 → LOT_SIZE 拒绝
                     （卖出允许零股全量卖出）
7. 资金/持仓：       买入现金不足 → INSUFFICIENT_CASH（削减到可买数量）
                     卖出持仓不足 → INSUFFICIENT_POSITION（削减）
8. 参与率上限：      可成交数量 ≤ bar.volume × max_participation → 部分成交
```

**资金约束的一致性要求（重要）**：`affordable_quantity` 必须与成本模型**完全一致** ——
滑点、佣金（含最低佣金）、过户费、**冲击成本**都要算进去，并用 `CostModel.compute` 做精确校验后逐手回退，
保证 `成交量 × 成交价 + 全部成本 ≤ 可用现金`。
否则同一交易日多笔买单会共用同一份现金估算并最终透支（本仓库曾出现 -0.23 元透支的缺陷，
已修复并补了 3 个回归用例）。

### 3.2 涨跌停判定细则

- 优先使用数据中的 `limit_up` / `limit_down`（最准确）。
- 缺失时按规则推算：`主板 10% / 创业板科创板 20% / ST 5%`，四舍五入到 0.01 元。
- 判定：`买入` 时不成交当且仅当 `open >= limit_up`（开盘即封板，无法买入）；`卖出` 时不成交当且仅当 `open <= limit_down`。
- 允许通过配置 `limit_up_fill_prob`（默认 0.0）模拟“涨停板偶尔能买到”，用于敏感性测试。

### 3.3 成交价

| `execution_price` | 计算 |
|---|---|
| `open`（默认） | `bar.open` |
| `vwap` | `bar.amount / bar.volume`（量价齐备时），否则退化为 open |
| `close` | `bar.close`（仅用于诊断，不建议作为默认） |

限价单：买单成交价 = `min(限价, 基准价)`；卖单 = `max(限价, 基准价)`。

### 3.4 顺延（“无法成交则顺延到下一个可成交日”）

- 订单带 `max_defer_days`（默认 5）：每次因 `SUSPENDED / LIMIT_UP / LIMIT_DOWN / NO_QUOTE` 未成交则 `deferred_days += 1`。
- 超过上限 → `EXPIRED`（记入统计，用于评估策略可执行性）。
- **顺延不改变订单意图**，但重新执行时会再次走 T+1 与风控检查（价格偏离限制可拦掉“过期信号”）。

---

## 4. 成本模型（`CostModel`）

```
总成本 = 固定费用 + 佣金 + 印花税 + 过户费 + 滑点 + 冲击成本

固定费用     = fixed_fee_per_order                       （默认 0）
佣金         = max(|成交额| × commission_rate, min_commission)   （买卖双向）
印花税       = 卖出成交额 × stamp_tax_rate(date)          （仅卖出，按日期生效税率）
过户费       = 成交额 × transfer_fee_rate                （双向）
滑点         = 成交额 × (fixed_bps + prop_bps) / 10000
               且成交价按方向偏移：买入价×(1+s)、卖出价×(1-s)
冲击成本     = 成交额 × η × (Q / ADV)^θ
               Q=订单股数，ADV=前 20 日日均成交股数，η、θ 可配置
```

- 印花税按 **生效日期分段**：1990-01-01 起 0.1%，2023-08-28 起 0.05%（可通过配置覆盖历史，支持 2008/2015 等长周期回测）。
- **成本加倍测试**：`CostConfig.scale(k)` 一次性把所有费率与滑点乘 k（默认 1.0，测试用 2.0）。
- 输出 `CostBreakdown` 明细，便于在报告中做成本归因。

---

## 5. 事件循环与主循环（见 `00_system_design.md` §5.2）

订单生命周期：

```
策略/组合 → OrderEvent(decision_ts=d, signal_date=d, submit_date=d+1)
   → RiskCheckEvent → RiskEngine.CheckOrder(order, account, snapshot)
        allow   → SUBMITTED（进入 working_orders）
        reduce  → 修改数量后 SUBMITTED
        reject  → RISK_REJECTED（记录审计）
        pause   → 当日不提交，保留至解除
        force_close → 生成反向平仓订单
   → 次日 OPEN 撮合 → 部分成交（PARTIALLY_FILLED，剩余继续顺延）/ 完全成交（FILLED）
   → 撤单 / 过期 / 拒绝
```

**引擎在 `submit_order` 中断言**：`t_plus_one=True` 时 `submit_date > signal_date`，否则抛 `FutureFunctionError`。

---

## 6. 事件优先级：类型优先级 vs 日内步骤优先级

事件队列按 `(timestamp, priority, 入队序号)` 排序，其中 `priority` 有两个来源：

1. **事件类型优先级**（`EventType` 整数值）：Timer(0) < MarketData(10) < Signal(20) < RiskCheck(30)
   < Order(40) < Fill(50)，用于「同一时间戳下按事件类型排序」的通用语义；
2. **日内步骤优先级**（`engine.StepPriority`）：引擎发布事件时显式指定，解决
   「同一时间戳下不同类型的先后」问题 —— 例如 15:00 的**收盘行情**必须先于 15:00 的**收盘定时器**，
   15:10 的**信号**必须先于 15:10 的**风控检查**与**下单**。

```
PRE_OPEN_TIMER=0 → OPEN_MARKET_DATA=10 → OPEN_RISK_CHECK=20 → OPEN_ORDER=30 → OPEN_FILL=40
→ CLOSE_MARKET_DATA=50 → CLOSE_TIMER=60 → POST_CLOSE_TIMER=61
→ POST_CLOSE_SIGNAL=70 → POST_CLOSE_RISK_CHECK=80 → POST_CLOSE_ORDER=90
```

同一交易日内，事件时间戳严格单调：
`09:00 定时器 → 09:30 行情 → 09:30 风控/成交 → 15:00 行情 → 15:00 定时器 → 15:10 定时器 → 15:10 信号/风控/订单`。

引擎在盘后阶段使用 **POST_CLOSE 快照**（同一批收盘行情，时间戳 15:10）做下单与风控决策，
因此订单事件的时间戳不会回退到 15:00。

---

## 7. 测试用例（已实现）

下表每一行都对应 `tests/` 下真实执行的用例；事件 / 成本 / 撮合 / 端到端相关用例共 **94 个**，全部通过。

| | 用例 | 断言 |
|---|---|---|
| E1 | 事件排序：同时间戳按 priority 排序 | Timer < MarketData < Signal < RiskCheck < Order < Fill |
| E2 | 事件排序：同时间戳同优先级按入队序 | FIFO |
| E3 | 事件录制器记录完整顺序 | `recorder.types() == [...]` |
| E4 | 未注册处理器的类型 | 静默忽略 + 计数 |
| E5 | 错误策略 | `on_error="raise"` 抛出、`"collect"` 收集 |
| E6 | 日内步骤优先级 | 09:00 定时器 → 09:30 行情 → 15:00 行情 → 15:00 定时器 → 15:10 定时器 |
| C1 | 佣金：万 2.5 + 最低 5 元 | 小额订单取 5 元 |
| C2 | 印花税：仅卖出，0.05% | 买入 0；卖出正确 |
| C3 | 印花税日期分段 | 2023-08-25 前 0.1%，之后 0.05% |
| C4 | 过户费：双向 0.001% | 买卖均收 |
| C5 | 滑点：买入抬价、卖出压价 | 成交价与成本双向一致 |
| C6 | 冲击成本：η(Q/ADV)^θ | 手算一致；ADV=0 时退化为 0 并告警 |
| C7 | 成本加倍 | `scale(2)` 后各项为 2 倍 |
| M1 | 停牌 | 不成交，状态顺延计数 +1 |
| M2 | 涨停买入 | 不成交，顺延；跌停卖出同理 |
| M3 | 跌停买入 / 涨停卖出 | **可成交**（对手盘充足） |
| M4 | T+1 | 当日买入的股票当日卖出被判 `T1_LOCK` |
| M5 | 次日卖出 | 次日前置解锁后可卖 |
| M6 | 整手 100 股 | 买入 250 → 200 股；卖出零股全量 |
| M7 | 部分成交（参与率 10%） | 成交量 = floor(volume×10%/100)×100，剩余顺延 |
| M8 | 涨跌停价推算 | 主板 10%、创业板 20%、ST 5%，四舍五入到分 |
| B1 | 端到端 T+1 | T 日收盘信号 → T 日无成交 → T+1 开盘成交 |
| B2 | 未来函数防护 | `signal_date == submit_date` 提交时抛 `FutureFunctionError` |
| B3 | 事件顺序（端到端） | 每日事件序列符合设计文档 §5.2 |
| B4 | 成本后净值 | 含费用后的账户总资产 < 不含费用；成本加倍后总成本比值为 2 |
| B5 | 停牌顺延端到端 | 信号次日停牌 → 成交发生在复牌首日 |
| B6 | 涨停顺延端到端 | 信号次日一字涨停 → 次日无法成交，第三天成交 |
| B7 | 顺延上限 | `max_defer_days=0` 且次日停牌 → 订单 `EXPIRED`，无成交 |
| B8 | 部分成交跨日 | 参与率上限下 1000 股分两天各成交 500 股，同一订单号 |
| B9 | T+1 卖出 | T+1 买入、T+1 收盘卖出信号 → T+2 开盘成交 |
| B10 | 策略取数越界 | 策略访问未来数据 → 抛 `LookaheadError` |
| B11 | 最后一日订单 | 最后一个交易日的订单计划被丢弃并计入诊断指标 |
