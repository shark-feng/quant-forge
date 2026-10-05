# A 股量化研究系统 —— 系统设计文档（v0.1）

> 状态：**设计已定稿（本轮）**，对应代码只实现 **M0 基础设施 + M1 数据层 + M2 回测引擎骨架**。
> 阅读顺序：本文档 → `01_data_layer.md` → `02_engine_skeleton.md` → `DEVELOPMENT.md`（开发方式合同）。

---

## 0. 合规与定位（先读）

- 本系统仅用于 **量化研究与教育**，不构成任何投资建议。
- 第一、二阶段 **不实现**：高频做市、延迟套利、跨市场实盘套利、裸卖空、期权做市。
- 第三阶段（Tick/订单簿、Almgren-Chriss、蒙特卡洛）**仅可模拟**，输出必须带 `research_only=True` 标记，代码路径不得连接任何券商交易接口。
- 实盘相关（程序化交易报备、券商柜台 QMT/Ptrade 接口、订单频率限制）在本系统内 **只做仿真与约束校验**，不落地下单。
- 详见 `docs/RISK_DISCLAIMER.md`。

---

## 1. 总体架构（模块图）

```
                         ┌──────────────────────────────┐
                         │  M0 基础设施 (config/core)    │
                         │  配置加载 / 枚举 / 模型 / 日历  │
                         │  异常 / 日志 / 时间工具        │
                         └───────────────┬──────────────┘
                                         │ 被所有模块依赖
   ┌─────────────────────────────────────┼─────────────────────────────────────┐
   │                                     │                                     │
┌──▼───────────────┐            ┌────────▼─────────┐                 ┌─────────▼────────┐
│ M1 数据层 data    │            │ M2 回测引擎 engine│                 │ M5 风控 RMS risk  │
│ ─────────────────│            │ ─────────────────│                 │ ─────────────────│
│ schema  列规范    │  Bar       │ events  事件定义  │  Order          │ RiskEngine       │
│ loader  读盘      │──────────▶ │ event_loop 事件循环│───────────────▶ │  ├ Rule 基类      │
│ store   PIT 访问  │            │ matching 撮合     │  RiskDecision   │  ├ 规则注册/优先级 │
│ universe 股票池   │            │ cost    成本模型  │◀─────────────── │  ├ 审计日志        │
│ calendar 日历     │            │ broker  订单/成交 │                 │  ├ 热更新          │
│ quality  质量报告 │            │ account 账务      │                 │  └ 暂停/强平       │
└──┬───────────────┘            │ backtest 主循环   │                 └─────────┬────────┘
   │                            └───┬──────────┬───┘                           │
   │ 历史数据                        │ Signal   │ Fill/持仓快照                   │ 检查
   │                                ▼          ▼                               │
   │                       ┌────────────────────────┐                ┌─────────▼────────┐
   │                       │ M3 策略层 strategy      │                │ M4 组合层 portfolio│
   │                       │  base / ma_cross /      │                │  等权/上限/持仓数   │
   │                       │  breakout / volume      │                │  现金管理/再平衡    │
   │                       └────────────────────────┘                └──────────────────┘
   │
   │  回测结果（净值/交易/持仓/暴露）
   ▼
┌──────────────────┐        ┌──────────────────┐
│ M6 评价层 metrics │───────▶│ M7 报告层 report  │
│ 收益/风险/换手/容量│        │ 图/表/HTML/Markdown│
└──────────────────┘        └──────────────────┘

第二阶段扩展（M8~M11）：因子模型 / 组合优化 / 波动率 / 配对交易 / 风险模型
第三阶段扩展（M12~M14）：订单簿回放 / 最优执行 / 蒙特卡洛压力测试
```

**依赖方向（严格单向，禁止反向 import）**

```
M0 ← M1 ← M2 ← M3/M4
        ↑      ↑
       M5 ─────┘   （M5 只依赖 M0；M2 通过接口调用 M5）
M2 → M6 → M7       （M6/M7 只消费回测输出，不参与撮合）
```

---

## 2. 目录结构（本轮已落地，标 ✅；规划中 ❌）

```
D:\Quantify\
├── README.md                        ✅ 项目说明与快速开始
├── pyproject.toml                   ✅ 打包/依赖/工具配置
├── requirements.txt                 ✅
├── configs\                         ✅ 配置文件（YAML 驱动）
│   ├── base.yaml                    ✅ 主配置（数据/引擎/成本/组合/回测区间）
│   ├── risk.yaml                    ✅ RMS 规则与阈值
│   ├── costs.yaml                   ✅ 成本敏感性场景（基准/加倍）
│   └── strategies\                  ✅ 策略参数
│       ├── ma_cross.yaml            ✅
│       ├── breakout.yaml            ✅
│       └── volume.yaml              ✅
├── docs\                            ✅
│   ├── 00_system_design.md          ✅ 本文（总体设计）
│   ├── 01_data_layer.md             ✅ M1 数据层接口/伪代码/测试用例
│   ├── 02_engine_skeleton.md        ✅ M2 引擎骨架（含 §5.1 订单字段语义）
│   ├── 03_acceptance_report.md      ✅ 阶段验收报告（含 §9 口径定义与更正记录）
│   ├── 04_strategy_layer.md         ✅ M3 策略层
│   ├── 05_portfolio_layer.md        ✅ M4 组合层
│   ├── 06_risk_rms.md               ✅ M5 风控 RMS
│   ├── 10_round2_design.md          ✅ 第二轮设计总纲（含 12 条缺陷修复设计）
│   ├── 11_akshare_provider.md       ✅ AKShare 数据源设计（字段映射/缓存/限流/质量）
│   ├── 12_framework_matrix.md       ✅ 框架选型矩阵与主引擎边界
│   ├── 12_appendix_verification.md  ✅ 选型待核实项 V1~V6 可执行清单（联网执行）
│   ├── 13_metrics.md                ✅ M6 评价层设计
│   ├── 14_provider_layer.md         ✅ **M4 取数层成文**（接口/能力矩阵/缓存限流/契约/边界）
│   ├── 17_round3_diagnostics.md     ✅ 第三轮诊断报告（D1~D4 + 实施结果）
│   ├── 18_m4_dataprovider.md        ✅ M4 设计（接口 + 决策记录）
│   ├── DEVELOPMENT.md               ✅ 开发方式合同（一模块一交付、验收门禁、报告纪律）
│   └── RISK_DISCLAIMER.md           ✅ 风险与合规声明
├── data\                            ✅ 目录占位（raw/processed/index/fundamental）
│   ├── raw\.gitkeep
│   ├── index\.gitkeep
│   └── fundamental\.gitkeep
├── src\aqs\                         ✅ 主包（A-share Quant System）
│   ├── __init__.py                  ✅
│   ├── core\                        ✅ M0
│   │   ├── enums.py                 ✅ 枚举：方向/订单类型/状态/事件类型/拒单原因
│   │   ├── models.py                ✅ Bar/Order/Fill/Position/Account/MarketSnapshot/Signal/RiskDecision
│   │   ├── events.py                ✅ 6 类事件 + 优先级 + 事件队列 + 录制器
│   │   ├── calendar.py              ✅ TradingCalendar（交易日/顺延/停牌下一可交易日）
│   │   ├── exceptions.py            ✅ 含 LookaheadError
│   │   └── logging.py               ✅ 统一日志/结构化审计流
│   ├── config\                      ✅ M0
│   │   ├── schema.py                ✅ 配置 dataclass（强类型 + 校验）
│   │   └── loader.py                ✅ YAML 加载/合并/覆写/环境变量
│   ├── data\                        ✅ M1 数据层 + M4 取数层（13 个模块）
│   │   ├── schema.py                ✅ 列规范 + 校验 + 质量报告（canonical 契约）
│   │   ├── loader.py                ✅ CSV/Parquet 读取 + normalize + **provider→store 适配**（`ingest_from_provider`）
│   │   ├── store.py                 ✅ DataStore + PITView（禁未来函数）
│   │   ├── universe.py              ✅ 股票池动态调整（ST/停牌/上市天数/流动性）
│   │   ├── synthetic.py             ✅ 确定性合成数据（测试/演示/压力场景）
│   │   ├── provider.py              ✅ M4-2 取数抽象：能力声明/溯源/健康/协议/降级披露
│   │   ├── cache.py                 ✅ M4-3 本地缓存（参数哈希/版本隔离/增量合并/清单/原子写）
│   │   ├── ratelimit.py             ✅ M4-4 令牌桶限流 + 指数退避重试（时钟可注入）
│   │   ├── quality.py               ✅ M4-5 数据质量检查 Q1~Q12 + 可落盘报告
│   │   ├── synthetic_provider.py    ✅ M4-6 合成数据 provider
│   │   ├── file_provider.py         ✅ M4-6 CSV/Parquet provider（能力由实际列推断）
│   │   ├── akshare_provider.py      ✅ M4-8 AKShare 映射骨架 + 快照累积（离线；联网探测属 M5）
│   │   └── registry.py              ✅ M4-7 注册表与配置驱动构建（未知名**不回退**）
│   ├── engine\                      ✅ M2（本轮为骨架 + 撮合/成本完整实现）
│   │   ├── cost.py                  ✅ 成本模型（佣金/印花税/过户费/滑点/冲击）
│   │   ├── matching.py              ✅ 撮合规则（涨跌停/停牌/T+1/整手/参与率）
│   │   ├── broker.py                ✅ 订单生命周期 + 撮合驱动 + 顺延
│   │   ├── account.py               ✅ 账务（现金/T+1 可卖/成本/估值）
│   │   ├── event_loop.py            ✅ 事件循环 + 处理器注册 + 审计
│   │   └── backtest.py              ✅ 日频主循环（T 收盘信号 → T+1 开盘成交）
│   ├── strategy\                    ✅ M3
│   │   ├── base.py                  ✅ 基类/上下文/参数严格校验
│   │   ├── indicators.py            ✅ 指标库（尾部窗口版 + 整段序列版，一致性由测试锁定）
│   │   ├── ma_cross.py              ✅ 均线交叉
│   │   ├── breakout.py              ✅ 价格突破
│   │   ├── volume.py                ✅ 成交量配合
│   │   └── registry.py              ✅ 注册表 + 配置驱动构建 + filters→数据层映射
│   ├── portfolio\                   ✅ M4
│   │   ├── base.py                  ✅ 接口 Protocol + OrderPlan/TargetWeight
│   │   ├── sizing.py                ✅ 等权/分数加权/凯利（半凯利）
│   │   ├── target_weight.py         ✅ 目标权重组合（上限/持仓数/现金/调仓）
│   │   └── registry.py              ✅ 配置驱动构建
│   ├── risk\                        ✅ M5
│   │   ├── base.py                  ✅ RiskRule/RiskContext/DayState/RiskEngine
│   │   ├── rules.py                 ✅ 13 条规则
│   │   ├── engine.py                ✅ RuleRiskEngine（优先级/短路/审计/热更新）
│   │   ├── var.py                   ✅ VaR/ES 历史模拟 + 突破率回溯
│   │   ├── registry.py              ✅ 规则注册表
│   │   └── stats.py                 ✅ 拒单率/误杀率/触发延迟
│   ├── metrics\                     ❌ M6
│   └── report\                      ❌ M7
├── tests\                           ✅ 52 个测试模块 / 772 个用例（零依赖运行器 + pytest 双兼容）
│   ├── run_tests.py                 ✅ 零依赖运行器（**全仓唯一的用例收集实现**，支持 skip）
│   ├── conftest.py                  ✅ pytest 夹具（合成市场/配置）
│   ├── tools.py                     ✅ 测试数据构造工具
│   ├── compat.py                    ✅ pytest 兼容层（assert/raises/approx/skip）
│   ├── contracts\
│   │   └── provider_contract.py     ✅ M4-9 四 provider 共用契约（8 项 × 4 实现；自身不被收集）
│   ├── fixtures\akshare\            ✅ M4-8 离线样例 + README（**手工构造，非真实抓取数据**）
│   └── test_*.py                    ✅ 55 个（M1~M5 各层 + 19 条缺陷回归 + 工程化/编码/契约/文档数字守护）
└── tools\                           ✅ 维护脚本（文档数字同步、抓取探测、上传、诊断）
    ├── sync_doc_counts.py           ✅ 文档数字同步（复用运行器收集逻辑；规则失配即报错）
    ├── probe_akshare.py             ✅ M5-1 AKShare 端点探测（**离线 dry-run 可验收**；产出能力报告）
    ├── upload_github.ps1            ✅ 宿主机推送脚本（含密钥扫描与非交互保护）
    └── diag_*.py                    ✅ 只读诊断（表格/订单时序/确定性/整手不变量）
```

> `reports\`（回测产物，M7 阶段生成）与 `data\cache\`（取数缓存）已 gitignore，故不在树中列出。
> `docs\data\` 存放探测产物：`probe_report.schema.json`（规格，入库）与
> `akshare_capability_report.{json,md}`（**真实探测**报告，入库）；
> dry-run 的产物落在 `docs\data\dry-run\` 且**已 gitignore**（假数据不得当结论）。
> M5 阶段 B 将新增 `tools\fetch_data.py`（真实抓取）。

---

## 3. 分层职责与边界

| 模块           | 职责                          | 明确不做              |
| ------------ | --------------------------- | ----------------- |
| M0 core      | 枚举/数据结构/事件/日历/异常/日志/配置      | 不含任何业务规则          |
| M1 data      | 提供 **时间点正确（PIT）** 的历史数据与股票池 | 不计算信号、不知道策略存在     |
| M2 engine    | 事件调度、撮合、成本、账务、T+1/涨跌停/停牌约束  | 不选股、不定仓位、不做风控决策   |
| M3 strategy  | 用 ≤T 的数据产出信号意向（指标 + 交叉/突破/放量判定） | 不下单、不查账户资金、不重复做股票池过滤 |
| M4 portfolio | 信号 → 目标权重 → 订单（数量/资金/持仓数约束） | 不做风控否决（交由 M5）     |
| M5 risk      | 订单检查、限额、亏损限制、暂停/强平          | 不产生 Alpha、不修改策略参数 |
| M6 metrics   | 净值序列 → 指标体系                 | 不改变回测路径           |
| M7 report    | 图表/表格/HTML 报告               | 不做计算逻辑（只做呈现）      |

---

## 4. 核心数据结构（本轮已实现，见 `src/aqs/core/models.py`）

### 4.1 Bar（单标的单日行情，含原始与后复权两套价格）

| 字段                                  | 类型           | 说明                                                    |
| ----------------------------------- | ------------ | ----------------------------------------------------- |
| symbol                              | str          | 统一格式 `600000.SH` / `000001.SZ`                        |
| date                                | date         | 交易日                                                   |
| open/high/low/close                 | float        | **原始价**（未复权，用于涨跌停/成本/成交价）                             |
| open_adj/high_adj/low_adj/close_adj | float        | **后复权价**（用于信号计算与收益）                                   |
| prev_close                          | float        | 原始前收盘（涨跌停基准）                                          |
| volume                              | float        | 成交量（股）                                                |
| amount                              | float        | 成交额（元）                                                |
| adj_factor                          | float        | 累计复权因子（>0）                                            |
| limit_up / limit_down               | float        | 原始口径涨跌停价                                              |
| is_suspended                        | bool         | 停牌（当日不可成交）                                            |
| is_st                               | bool         | ST/*ST 标记                                             |
| listed_days                         | int          | 截至该日的上市自然交易日数                                         |
| delist_date                         | date \| None | 退市日（用于回避未来退市股与幸存者偏差验证）                                |
| trading_status                      | enum         | NORMAL / SUSPENDED / LIMIT_UP / LIMIT_DOWN / DELISTED |

**双价格原则（关键设计）**：撮合与成本一律用原始价；信号与绩效一律用后复权价。两者在同一 Bar 内共存，避免“用复权价判断涨跌停”这类错误。

### 4.2 Order

`order_id, symbol, side, qty, order_type, limit_price, decision_ts, signal_date, submit_date, status, filled_qty, avg_fill_price, deferred_days, max_defer_days, reject_reason, parent_id(算法子单), tag(策略标记)`

订单状态机：

```
CREATED ─▶ RISK_PENDING ─▶ SUBMITTED ─▶ PARTIALLY_FILLED ─▶ FILLED
                │              │              │
                ▼              ▼              ▼
          RISK_REJECTED   CANCELLED / EXPIRED / REJECTED
```

### 4.3 Fill

`fill_id, order_id, symbol, side, qty, price, gross_amount, commission, stamp_tax, transfer_fee, slippage_cost, impact_cost, total_cost, date, ts, deferred_days`

### 4.4 Account / Position

- `Position`: `symbol, total_qty, sellable_qty, avg_cost, last_price, market_value, unrealized_pnl, realized_pnl`
- `Account`: `cash, frozen_cash, positions, total_value, prev_close_value, date, daily_pnl, peak_value`
- **T+1 语义**：买入成交后 `sellable_qty` 不增加；每个交易日开盘前 `sellable_qty = total_qty`（前一日买入解禁）。

### 4.5 MarketSnapshot

`date, bars: dict[symbol, Bar], account, calendar_ref`；提供 `price(symbol, adjusted)`, `is_tradable(symbol)`, `limit_state(symbol)`。

---

## 5. 事件驱动模型（M2）

### 5.1 事件类型（全部实现）

| 事件                | 触发时机            | 载荷                       | 默认优先级 |
| ----------------- | --------------- | ------------------------ | ----- |
| `TimerEvent`      | 每日开盘前/收盘后/自定义   | timer_id, phase          | 0     |
| `MarketDataEvent` | 每日 open / close | phase, snapshot          | 10    |
| `SignalEvent`     | 策略计算完成          | 一组 SignalIntent          | 20    |
| `RiskCheckEvent`  | 订单提交前           | order, account, snapshot | 30    |
| `OrderEvent`      | 风控通过后           | order                    | 40    |
| `FillEvent`       | 撮合成交            | fill                     | 50    |

同一时间戳按 `(timestamp, priority, 入队序号)` 排序 —— 保证 **可复现的事件顺序**（验收项）。

### 5.2 单个交易日循环（T+1 正确性的核心）

```
for d in trading_days:
    # ① 开盘前
    publish(TimerEvent(d, PRE_OPEN))
    account.unlock_t1()                       # 昨日买入今日可卖
    snapshot = data.bars_on(d)

    # ② 开盘：执行 T-1 日收盘产生的订单（T+1 成交）
    publish(MarketDataEvent(d, OPEN, snapshot))
    for order in broker.working_orders:       # FIFO
        publish(RiskCheckEvent(d, OPEN, order, account, snapshot))   # 撮合前复核
        if decision.action == REJECT: continue
        fill = broker.try_fill(order, snapshot, phase=OPEN)
        if fill: publish(FillEvent(fill))     # → account 更新
        else:    order.deferred_days += 1     # 停牌/涨停/跌停 → 顺延

    # ③ 收盘：估值
    publish(MarketDataEvent(d, CLOSE, snapshot))
    account.mark_to_market(snapshot)
    publish(TimerEvent(d, CLOSE))

    # ④ 收盘后：策略产生 T 日信号 → M4 组合 → M5 风控 → 挂 T+1 开盘订单
    publish(TimerEvent(d, POST_CLOSE))
    snapshot_post = POST_CLOSE 快照（同一批收盘行情，时间戳 15:10）
    ctx = StrategyContext(data=store.as_of(d), ...)          # 只能看到 ≤ d 的数据
    signals = strategy.on_bar(ctx)
    publish(SignalEvent(d, signals))
    for plan in portfolio.generate_orders(signals, ctx):
        order = broker.create_order(..., signal_date=d, submit_date=next_trading_day(d))   # T+1 强制
        decision = risk.check_order(order, account, snapshot_post)
        publish(RiskCheckEvent(...)) → publish(OrderEvent(...)) → broker.submit(order)
```

> 事件在队列中按 `(timestamp, priority, 入队序号)` 排序。引擎发布时使用**日内步骤优先级**
> （`engine.StepPriority`）显式固定每日先后，同一交易日内事件时间戳严格单调：
> `09:00 → 09:30 → 15:00 → 15:10`；详见 `02_engine_skeleton.md` §6。

**未来函数防线（三层）**

1. `DataStore.as_of(d)` 返回 `PITView`，请求 `> d` 的数据直接抛 `LookaheadError`；策略只能拿到 PITView。
2. `order.submit_date` 必须严格大于 `order.signal_date`（`t_plus_one=True` 时），引擎在提交处断言。
3. `tests/test_data_store.py` / `test_engine_backtest.py` 用显式用例锁死这两条。

---

## 6. 数学模型 → 模块映射（含验收标准）

| 模型              | 阶段  | 模块（路径）                          | 输入                          | 输出            | 参数                   | 验收                       |
| --------------- | --- | ------------------------------- | --------------------------- | ------------- | -------------------- | ------------------------ |
| 交易成本模型          | 一   | `engine/cost.py` ✅              | side, qty, price, ADV, date | CostBreakdown | 佣金率/印花税率/过户费率/滑点/η,θ | 手算用例一致；成本加倍后策略仍有效（M6 验收） |
| VaR/ES（历史模拟）    | 一   | `risk/var.py` ✅                 | 持仓、历史收益                     | VaR, ES       | 置信度、持有期              | 突破率 ≈ 1-置信度（滚动检验）        |
| 凯利公式（半凯利）       | 一   | `portfolio/sizing.py` ✅         | 胜率/赔率或 μ,σ                  | f*            | half_kelly=0.5       | 回撤可控，不超过单票上限             |
| 均值-方差优化         | 二   | `portfolio/optimizer.py` ❌      | μ, Σ, w0, 约束                | w*            | λ, 换手上限              | 约束全部满足，样本外 IR 提升         |
| CAPM            | 二   | `factors/capm.py` ❌             | R_i, R_m                    | β, α          | 无                    | 回归 t 值显著、R² 合理           |
| 多因子模型（FF/Barra） | 二   | `factors/model.py` ❌            | 收益、暴露                       | 因子收益、Σ        | 因子列表、行业分类            | ICIR > 0.3，行业/市值中性后残差无结构 |
| GARCH(1,1)      | 二   | `volatility/garch.py` ❌         | 收益率序列                       | σ_t 预测        | p, q                 | MLE 收敛（退出码 0），预测误差合理     |
| 协整配对            | 二   | `statarb/pairs.py` ❌            | 两只股票价格                      | β, z-score    | 窗口、开平阈值              | ADF p < 0.05，样本外价差回归稳定   |
| Almgren-Chriss  | 三   | `execution/almgren_chriss.py` ❌ | Q, ADV, σ                   | 执行路径          | κ, λ                 | 滑点低于 TWAP/VWAP 基准        |
| 蒙特卡洛/压力测试       | 三   | `stress/montecarlo.py` ❌        | 收益分布、参数                     | 策略分布          | 模拟次数                 | 给出收益/回撤分布与破产概率           |

---

## 7. 配置体系

- 单一入口 `configs/base.yaml`，`loader.load_config()` 递归加载并 **强类型校验**（未知键报错，缺失键取默认值）。
- 策略参数独立在 `configs/strategies/*.yaml`，通过 `strategy:` 段引用。
- 覆盖优先级：默认值 < base.yaml < 策略文件 < 命令行 `--set a.b.c=v` < 环境变量 `AQS__A__B__C`。
- 所有影响回测结果的参数（成本、滑点、涨跌停幅度、参与率、持仓上限）**必须**可在配置中关闭/调整，以支持灵敏度与加倍测试。

---

## 8. 阶段交付计划（每阶段一模块一验收）

| 轮次  | 交付模块                                      | 状态       |
| --- | ----------------------------------------- | -------- |
| R1  | 设计文档 + 目录结构                               | ✅ 本轮     |
| R2  | M0 core + M1 数据层（PIT/复权/股票池/质量校验）+ 单测     | ✅ 已交付     |
| R3  | M2 引擎骨架（事件/循环/撮合/成本/账务/T+1）+ 单测           | ✅ 已交付     |
| R4  | M3 策略层（指标库/均线交叉/价格突破/成交量配合/注册表）+ 单测       | ✅ 已交付     |
| R5  | M4 组合层（等权/单票上限/最大持仓/现金管理/凯利）+ M5 风控 RMS（13 条规则/VaR/验收指标）+ 引擎接入 | ✅ 已交付 |
| —   | 合计 802 个单元测试用例全部通过（零依赖运行器 / pytest 双兼容）   | ✅       |
| R6  | M6 评价层 + M7 报告层，输出第一份完整回测报告               | ⏭ 待确认后开工 |
| R7  | 偏差与压力测试套件（未来函数/幸存者/过拟合/成本敏感性/极端行情）        | ⏭        |
| R8+ | 第二阶段：多因子/优化/GARCH/协整                      | ⏭        |

---

## 9. 本轮已知限制（诚实声明）

1. **pytest 无法安装**：当前环境无法访问 PyPI（镜像超时），因此 `pytest` / `pyarrow` /
   `cvxpy` / `statsmodels` 均不可用。测试通过零依赖运行器 `python tests/run_tests.py`
   执行（184 个用例全绿）；测试代码同时兼容 pytest（`assert` + `raises` + `approx`），
   装上 pytest 后可直接 `pytest -q`。
2. **风控引擎已实现（M5）**：`RuleRiskEngine` 默认启用（`configs/risk.yaml`，13 条规则），
   回测结果**已包含风控约束**；可用 `risk.enabled=false` 关闭，或在 `configs/risk.yaml` 中把
   `mode` 改为 `observe`（只记录不拦截）做对照实验。
3. 撮合为 **日频 + 参与率上限** 的近似模型，不含订单簿排队；订单簿回放属第三阶段。
4. 涨跌停判定优先使用数据中的 `limit_up/limit_down`；缺失时按板块规则推算
   （主板 10%、创业板/科创板 20%、北交所 30%、主板 ST 5%，四舍五入到分；
   上市首日不设涨跌幅）。真实数据上仍需回归校验。
5. **现金分红的近似处理**：除权日按 `D = P_{t-1} × (1 − f_{t-1}/f_t)` 把复权因子变化
   折算为现金入账。总收益正确，但送股/转增/配股会被折算成等值现金，
   现金与持股结构会与真实账户有偏差（第一阶段可接受，报告中必须声明）。
6. 数据源未接入真实供应商（Tushare/Wind/聚宽等）；本轮用确定性合成数据 + CSV/Parquet
   通用接口，接真实源只需实现 `BarDataLoader` 协议。
7. **风控的粒度限制**：分钟级下单频率限制在日频回测中无法真实生效
   （`max_orders_per_minute` 仅作配置保留，实际生效的是 `max_orders_per_day`）；
   `industry_exposure` 需要行业分类数据，缺失时自动跳过并记录；
   日频语义下的「立即」暂停/强平 = 次日开盘执行（受涨跌停/停牌约束可能被顺延）。
8. 成本模型中的冲击成本参数（η=0.1、θ=0.5）、参与率上限（10%）为经验假设，
   需要在真实数据上校准；`diagnostics.cost_missing_adv` 会提示 ADV 缺失导致的冲击成本跳过的次数。
9. 成交量策略的均量基准**不含当日**（合同「过去 20 日平均成交量」口径）。
   该选择会让放量信号比「含当日」口径更容易触发，已由参数 `volume_ma_exclude_today` 显式控制，
   并有专门测试锁定两种口径的差异。
