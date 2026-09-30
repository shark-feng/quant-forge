# AQS（quant-forge）—— A 股量化研究系统

> **仅用于量化研究与教育，不构成投资建议。** 实盘交易需符合中国证监会、交易所与券商合规要求。
> 第三阶段（Tick/订单簿、最优执行、蒙特卡洛）仅作模拟研究，不得直接实盘。
> 详见 [`docs/RISK_DISCLAIMER.md`](docs/RISK_DISCLAIMER.md)。

本仓库按「一模块一交付」的方式实现一个可运行、可测试、可扩展的 A 股量化研究系统。
目标仓库：<https://github.com/shark-feng/quant-forge>

---

## 当前进度

### 第一轮（已交付）

| 模块 | 内容 | 状态 |
| --- | --- | --- |
| M0 基础设施 | 枚举 / 数据模型 / 事件 / 交易日历 / 异常 / 日志 / 配置 | ✅ 已交付（含单测） |
| M1 数据层 | PIT 存储、后复权、停牌/退市/ST、历史指数成分、公告日财务数据、股票池动态调整、质量校验、合成行情 | ✅ 已交付（含单测） |
| M2 回测引擎 | 6 类事件、事件循环、撮合（涨跌停/停牌/T+1/整手/参与率）、成本模型（佣金/印花税/过户费/滑点/冲击）、账户与 T+1 账务、日频主循环 | ✅ 已交付（含单测） |
| M3 策略层 | 指标库 + 均线交叉 / 价格突破 / 成交量配合，参数全配置化、配置驱动注册表 | ✅ 已交付（含单测） |
| M4 组合层 | 等权 / 分数加权、单票上限、最大持仓数、现金管理、凯利公式（半凯利）、调仓开关 | ✅ 已交付（含单测） |
| M5 风控 RMS | 13 条可扩展规则 + 优先级/短路、审计日志、热更新、实时预警、暂停/减仓/强平、VaR/ES（历史模拟）、验收指标 | ✅ 已交付（含单测） |

### 第二轮（进行中）

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| R2-01 | 第一轮 12 条缺陷修复 + 回归测试 | ✅ 已交付 |
| R2-02 | 工程化：LICENSE / NOTICE / README / .gitignore / 依赖声明 / GitHub 上传 | ⏳ 进行中 |
| R2-03 | 框架选型矩阵（AKQuant / PyBroker / Backtrader vs 自研） | ✅ 设计已定稿 |
| R2-04 | DataProvider 抽象（缓存 / 限流 / 重试 / 降级 / 契约测试） | ⏳ 设计已定稿 |
| R2-05 | AKShare 数据源适配 + 数据质量报告 | ⏳ 设计已定稿 |
| R2-06 | M6 评价层（指标 / 基准 / 暴露 / 归因 / 稳健性） | ⏳ 设计已定稿 |
| R2-07 | M7 报告层（HTML / Markdown / 验收报告） | ⏳ 待设计 |
| R2-08 | 偏差与压力测试套件 | ⏳ 待设计 |
| R2-09 | 第二阶段设计（多因子 / 优化 / GARCH / 协整） | ⏳ 待设计 |

测试：**678 个单元测试用例**（48 个测试模块）。

> **关于 skip**：少数用例是**环境门控**的（例如「pyarrow 缺失时应报错并给出安装提示」这类
> 负面路径用例，在已装 pyarrow 的机器上无法成立）。运行器会单独列出跳过项与原因，
> **跳过不计入通过** —— 避免「环境缺依赖」被读成「验证通过」。
> 本机当前结果：通过 677 / 跳过 1 / 失败 0。用例总数（678）是**收集到的用例数**，与是否跳过无关。

### 文档索引

| 文档 | 内容 |
| --- | --- |
| [`docs/00_system_design.md`](docs/00_system_design.md) | 系统总体设计：模块图 / 数据结构 / 接口 / 配置 / 模型映射表 |
| [`docs/01_data_layer.md`](docs/01_data_layer.md) | M1 数据层：PIT 存储、复权、股票池、质量校验 |
| [`docs/02_engine_skeleton.md`](docs/02_engine_skeleton.md) | M2 引擎：撮合规则、成本模型、事件优先级 |
| [`docs/03_acceptance_report.md`](docs/03_acceptance_report.md) | 阶段验收报告（合同逐条映射） |
| [`docs/04_strategy_layer.md`](docs/04_strategy_layer.md) | M3 策略层：三种策略的精确口径与测试 |
| [`docs/05_portfolio_layer.md`](docs/05_portfolio_layer.md) | M4 组合层：仓位计算、目标权重、凯利 |
| [`docs/06_risk_rms.md`](docs/06_risk_rms.md) | M5 风控：13 条规则、VaR/ES、验收指标 |
| [`docs/10_round2_design.md`](docs/10_round2_design.md) | 第二轮设计总纲（含 12 条缺陷修复设计） |
| [`docs/11_akshare_provider.md`](docs/11_akshare_provider.md) | AKShare 数据源设计（字段映射 / 缓存 / 限流 / 质量） |
| [`docs/12_framework_matrix.md`](docs/12_framework_matrix.md) | 框架选型矩阵与主引擎边界 |
| [`docs/12_appendix_verification.md`](docs/12_appendix_verification.md) | 框架选型待核实项 V1~V6 的可执行清单（宿主机联网执行） |
| [`docs/13_metrics.md`](docs/13_metrics.md) | M6 评价层设计（指标口径 / 容量 / 归因） |
| [`docs/17_round3_diagnostics.md`](docs/17_round3_diagnostics.md) | 第三轮诊断报告（D1 口径 / D2 整手 / D3 字段语义 / D4 可复现性） |
| [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) | 开发方式合同（一模块一交付、验收门禁、报告与数字纪律） |
| [`CHANGELOG.md`](CHANGELOG.md) | 更新日志（按轮次记录新增/修复/文档变更） |
| [`NOTICE`](NOTICE) | 第三方署名、依赖清单、合规声明、待联网核实项 |
| [`LICENSE`](LICENSE) | MIT License |

---

## 快速开始

```powershell
# 1) 依赖（核心仅需 numpy / pandas / PyYAML）
python -m pip install -r requirements.txt

# 2) 运行测试（零依赖运行器；装了 pytest 也可以直接 pytest -q）
python tests\run_tests.py

# 3) 跑演示回测（合成数据，三种策略任选）
python examples\demo_backtest.py --strategy ma_cross --generate-symbols 30
python examples\demo_backtest.py --strategy breakout --generate-symbols 30
python examples\demo_backtest.py --strategy volume   --generate-symbols 30 --cost-scale 2.0
```

演示脚本在 `reports/<策略名>/` 下输出 `equity_curve.csv`、`trades.csv`、`orders.csv`、
`universe_stats.csv`、`summary.json`。

> **参数口径提示**：`--generate-symbols` 是「生成多少只标的」；
> 最终**每日入池**数量还会经过指数成分、ST/停牌、上市不足 60 日、流动性四层过滤。
> 输出中的「数据口径」一行会同时打印：生成 / 有行情 / 指数成分(日均) / 入池(日均)。

---

## AKShare 数据准备

> 状态：**设计已定稿**（[`docs/11_akshare_provider.md`](docs/11_akshare_provider.md)），
> 适配层在第三轮 **M5** 交付。本节给出「联网环境需要做什么」的完整 runbook；
> **CI 与离线测试一律不依赖网络**（使用 `tests/fixtures/akshare/*` 脱敏样例）。

### 1. 安装依赖

```powershell
pip install -e ".[data]"     # 等价于 akshare>=1.12 + pyarrow>=14.0
```

### 2. 探测数据源能力（先探测，再抓取）

AKShare 的接口名与字段**随版本变化**，因此本项目**不照搬其接口**，
而是先按数学模型所需字段反推数据规格（`docs/11` §3），再用探测脚本核对可用性：

```powershell
python tools\probe_akshare.py --out reports\_probe\akshare_capabilities.json
```

输出为 `capabilities` 报告：每个候选接口的**可用性 / 字段清单 / 行数量级 / 速率限制表现**。
**未通过探测的接口不会进入字段映射表。**

### 3. 抓取数据到本地缓存

```powershell
# 全量抓取（首次；耗时较长，受速率限制）
python tools\fetch_data.py --start 2020-01-01 --end 2025-12-31

# 增量更新（只补缺失区间）
python tools\fetch_data.py --start 2025-01-01 --end 2025-12-31 --incremental
```

- 缓存位置：`data/cache/`（Parquet + manifest，**已 gitignore**）；
- 限流：令牌桶 + 指数退避重试（`data/ratelimit.py`）；
- 失败降级：单接口失败不影响其它接口，缺失项写入质量报告。

### 4. 数据质量报告（Q1~Q12）

```powershell
python tools\fetch_data.py --report reports\_probe\data_quality.json
```

重点检查项（`docs/11` §7）：

| 编号 | 检查 | 为何必须 |
| --- | --- | --- |
| **Q3** | **成交量单位「手」→「股」换算守卫** | 部分接口返回「手」，若误当「股」会让成交额/参与率/冲击成本全部错 100 倍 |
| **Q11** | 涨跌停 / 停牌 / ST 标记与官方规则一致性 | 直接影响「买不进/卖不出」判定 |
| **Q12** | **幸存者偏差自检**：退市股票是否仍在历史池中 | 用当前成分回溯历史会系统性高估收益 |

### 5. 用真实数据跑回测

```powershell
python examples\demo_backtest.py --strategy ma_cross --provider akshare --output reports\ma_cross_real
```

> ⚠️ 首次使用真实数据时，`data.listing_date.policy` **必须保持默认 `strict`**
> （缺 `list_date` 直接报错，而不是静默用代理口径）——
> 这正是缺陷 #8 的修复要求，目的是不让「数据不全」悄悄变成「结果偏差」。

### 6. 数据版权与合规

抓取到的原始数据**版权归原始数据提供方**，本项目**不分发**原始数据
（`data/` 全部在 `.gitignore` 中）。署名与第三方清单见 [`NOTICE`](NOTICE)。

---

## 目录结构

```
configs/            配置文件（数据/引擎/成本/风控/策略）
docs/               设计文档、开发合同、风险声明、验收报告、第三轮诊断报告
examples/           演示脚本
src/aqs/
  core/             枚举、数据模型、事件、交易日历、异常、日志、数量归一化、运行溯源
  config/           强类型配置 schema 与 YAML 加载器
  data/             PIT 数据仓库、复权、股票池、质量校验、合成行情
  engine/           成本、撮合、经纪商、账务、敞口控制、事件循环、回测主循环
  strategy/         指标库 + 均线交叉/价格突破/成交量配合 + 配置驱动注册表
  portfolio/        仓位计算（等权/凯利）+ 目标权重组合 + 注册表
  risk/             规则引擎 + 13 条风控规则 + VaR/ES + 验收指标
tests/              单元测试（678 个用例）
tools/              诊断与数据工具（口径体检 / 订单时间线 / 确定性比对 / AKShare 探测与抓取）
LICENSE             MIT
NOTICE              第三方署名、依赖清单、合规声明
CHANGELOG.md        更新日志
```

---

## 报告与数字纪律

本项目的**每个数字必须可追溯**（`docs/DEVELOPMENT.md` §8），这是第三轮 D1 诊断的直接产物：

| 规则 | 落地方式 |
| --- | --- |
| 数字必须带「口径 + 来源文件 + 生成命令」 | 报告与文档中逐条标注；`docs/03` §9.0 给出术语定义表 |
| 禁止术语混用（如用 `exit=0` 表示进程返回码） | `exit` 在本项目是 `tag` 枚举值，报告统一写「进程返回码 0」 |
| 结果必须可复现 | `tests/test_determinism.py` 用**两个不同 `PYTHONHASHSEED` 的子进程**比对逐日决策指纹 |
| 产物自带溯源 | `reports/<run>/summary.json` 的 `diagnostics.invocation` 记录命令行 / Git HEAD / Python / 哈希种子 |
| 文档数字必须与落盘一致 | `tests/test_defect_16_provenance.py` 直接比对 `docs/03` 与 `reports/*/trades.csv` |
| 报告层指标自带定义 | M7 交付时每个指标必须携带 `definition` 字段（公式 + 口径 + 是否年化） |

> 为什么这么严：第三轮诊断发现，修复前同一命令连续三次运行会产生 **313 / 537 / 463** 张订单
> （根因：`set` 迭代顺序 + Python 字符串哈希加盐），
> 且验收报告里的数字曾无法用任何落盘文件复核。详见 [`docs/17_round3_diagnostics.md`](docs/17_round3_diagnostics.md)。

---

## 关键设计约束（代码级强制）

| 约束 | 实现位置 |
| --- | --- |
| **禁未来函数**：策略只能通过 `PITView` 取数，越界抛 `LookaheadError` | `data/store.py` |
| **T+1**：订单 `submit_date` 必须严格晚于 `signal_date`，否则 `FutureFunctionError` | `engine/broker.py` |
| **涨跌停买不进/卖不出**：优先用数据自带涨跌停价，缺失时按板块规则推算 | `data/schema.py` |
| **停牌不可成交**：按 `max_defer_days` 顺延；`TimeInForce.DAY` 当日过期（不计入拒单） | `engine/matching.py`、`engine/broker.py` |
| **财务数据用公告日**：只保留 `announce_date <= 决策时间` 的记录 | `data/schema.py` |
| **股票池动态调整**：剔除 ST/停牌/上市不足 60 日/20 日日均成交额不足 5000 万 | `data/universe.py` |
| **上市日不得静默兜底**：缺 `list_date` → 默认报错；降级需显式并全程披露 | `data/store.py`、`config/schema.py` |
| **幸存者偏差**：`all_listed` 含已退市股票，`mode=index` 使用历史成分 | `data/store.py` |
| **最小 100 股**：买入向下取整到整手，卖出允许零股 | `engine/matching.py` |
| **现金不透支**：可买数量按剩余现金 + 全部成本（含冲击成本）精确计算 | `engine/matching.py` |
| **成本可加倍**：`costs.scale` 统一放大所有成本科目 | `engine/cost.py` |
| **双价格口径**：成交/涨跌停/费用用原始价，信号/收益用后复权价，分红现金入账 | `core/models.py`、`engine/account.py` |
| **风控可扩展**：规则可注册、优先级可配、支持热更新与 observe 模式；审计落盘 | `risk/rules.py`、`risk/engine.py` |
| **减仓可追溯**：T+1/停牌导致的减仓缺口记入 pending 并逐日补偿 | `engine/control.py` |

---

## 合规声明

本系统仅用于量化研究与教育，不构成投资建议。第一、二阶段不实现高频做市、延迟套利、
跨市场实盘套利、裸卖空、期权做市等 A 股无法合规落地的功能。任何实盘决策须独立评估，
并遵守监管机构与券商的合规要求。
