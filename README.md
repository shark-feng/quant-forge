# AQS —— A 股量化研究系统

> **仅用于量化研究与教育，不构成投资建议。** 实盘交易需符合中国证监会、交易所与券商合规要求。
> 第三阶段（Tick/订单簿、最优执行、蒙特卡洛）仅作模拟研究，不得直接实盘。
> 详见 [`docs/RISK_DISCLAIMER.md`](docs/RISK_DISCLAIMER.md)。

本仓库按「一模块一交付」的方式实现一个可运行、可测试、可扩展的 A 股量化研究系统。

---

## 当前进度（第一阶段）

| 模块        | 内容                                                                       | 状态         |
| --------- | ------------------------------------------------------------------------ | ---------- |
| M0 基础设施   | 枚举 / 数据模型 / 事件 / 交易日历 / 异常 / 日志 / 配置                                     | ✅ 已交付（含单测） |
| M1 数据层    | PIT 存储、后复权、停牌/退市/ST、历史指数成分、公告日财务数据、股票池动态调整、质量校验、合成行情                     | ✅ 已交付（含单测） |
| M2 回测引擎   | 6 类事件、事件循环、撮合（涨跌停/停牌/T+1/整手/参与率）、成本模型（佣金/印花税/过户费/滑点/冲击）、账户与 T+1 账务、日频主循环 | ✅ 已交付（含单测） |
| M3 策略层    | 指标库 + 均线交叉 / 价格突破 / 成交量配合，参数全配置化、配置驱动注册表                                 | ✅ 已交付（含单测） |
| M4 组合层    | 等权 / 分数加权、单票上限、最大持仓数、现金管理、凯利公式（半凯利）、调仓开关                                  | ✅ 已交付（含单测） |
| M5 风控 RMS | 13 条可扩展规则 + 优先级/短路、审计日志、热更新、实时预警、暂停/减仓/强平、VaR/ES（历史模拟）、验收指标              | ✅ 已交付（含单测） |
| M6 评价层    | 年化/夏普/Calmar/Sortino/换手/容量/滑点敏感性                                         | ⏳ 未开工      |
| M7 报告层    | 净值曲线、回撤、交易明细、持仓、暴露、归因                                                    | ⏳ 未开工      |

测试：**398 个单元测试用例全部通过**（24 个测试模块）。

设计文档：[`docs/00_system_design.md`](docs/00_system_design.md)；
数据层：[`docs/01_data_layer.md`](docs/01_data_layer.md)；
引擎：[`docs/02_engine_skeleton.md`](docs/02_engine_skeleton.md)；
策略层：[`docs/04_strategy_layer.md`](docs/04_strategy_layer.md)；
组合层：[`docs/05_portfolio_layer.md`](docs/05_portfolio_layer.md)；
风控 RMS：[`docs/06_risk_rms.md`](docs/06_risk_rms.md)；
验收报告：[`docs/03_acceptance_report.md`](docs/03_acceptance_report.md)；
开发方式合同：[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)。

---

## 快速开始

```powershell
# 1) 依赖（仅需 numpy / pandas / PyYAML；scipy 等留待第二阶段）
python -m pip install -r requirements.txt

# 2) 运行测试（零依赖运行器；装了 pytest 也可以直接 pytest -q）
python tests\run_tests.py

# 3) 跑演示回测（合成数据，三种策略任选）
python examples\demo_backtest.py --strategy ma_cross  --symbols 30
python examples\demo_backtest.py --strategy breakout  --symbols 30
python examples\demo_backtest.py --strategy volume    --symbols 30 --cost-scale 2.0   # 成本加倍敏感性
```

演示脚本在 `reports/<策略名>/` 下输出 `equity_curve.csv`、`trades.csv`、`orders.csv`、
`universe_stats.csv`、`summary.json`。

---

## 目录结构

```
configs/            配置文件（数据/引擎/成本/风控/策略）
docs/               设计文档、开发合同、风险声明、验收报告
examples/           演示脚本
src/aqs/
  core/             枚举、数据模型、事件、交易日历、异常、日志
  config/           强类型配置 schema 与 YAML 加载器
  data/             PIT 数据仓库、复权、股票池、质量校验、合成行情
  engine/           成本、撮合、经纪商、账务、事件循环、回测主循环
  strategy/         指标库 + 均线交叉/价格突破/成交量配合 + 配置驱动注册表
  portfolio/        仓位计算（等权/凯利）+ 目标权重组合 + 注册表
  risk/             规则引擎 + 13 条风控规则 + VaR/ES + 验收指标
tests/              单元测试（398 个用例）
```

---

## 关键设计约束（代码级强制）

| 约束                                                                     | 实现位置                                      |
| ---------------------------------------------------------------------- | ----------------------------------------- |
| **禁未来函数**：策略只能通过 `PITView` 取数，越界抛 `LookaheadError`                     | `data/store.py`                           |
| **T+1**：订单 `submit_date` 必须严格晚于 `signal_date`，否则 `FutureFunctionError` | `engine/broker.py`、`engine/backtest.py`   |
| **涨跌停买不进/卖不出**：优先用数据自带涨跌停价，缺失时按板块规则推算                                  | `data/schema.py`、`engine/matching.py`     |
| **停牌不可成交**：停牌/无成交量的标的被撮合拒绝并按 `max_defer_days` 顺延                       | `engine/matching.py`、`engine/broker.py`   |
| **财务数据用公告日**：只保留 `announce_date <= 决策时间` 的记录                           | `data/schema.py`                          |
| **股票池动态调整**：剔除 ST/停牌/上市不足 60 日/20 日日均成交额不足 5000 万                      | `data/universe.py`                        |
| **幸存者偏差**：`all_listed` 含已退市股票，`mode=index` 使用历史成分                      | `data/store.py`、`data/universe.py`        |
| **最小 100 股**：买入向下取整到整手，卖出允许零股                                          | `engine/matching.py`                      |
| **现金不透支**：可买数量按剩余现金 + 全部成本（含冲击成本）精确计算                                  | `engine/matching.py::affordable_quantity` |
| **成本可加倍**：`costs.scale` 统一放大所有成本科目                                     | `engine/cost.py`                          |
| **双价格口径**：成交/涨跌停/费用用原始价，信号/收益用后复权价，分红现金入账                              | `core/models.py`、`engine/account.py`      |
| **策略参数严格校验**：未知参数键、非法窗口、非法价格字段一律报错                                     | `strategy/base.py`、`strategy/registry.py` |
| **仓位上限硬约束**：名额同时计入已持仓与在途买单；卖出未成交不释放名额                                  | `portfolio/target_weight.py`              |
| **风控每日独立评估**：回撤/当日亏损/撤单率不依赖订单，每个交易日都会检查                                | `risk/engine.py::evaluate_controls`       |
| **风控可扩展**：规则可注册、优先级可配、支持热更新与 observe 模式                                 | `risk/rules.py`、`risk/registry.py`        |

---

## 演示脚本说明

`examples/demo_backtest.py` 使用完整的 M3 策略 + M4 组合 + M5 风控链路，
参数分别来自 `configs/strategies/*.yaml` 与 `configs/risk.yaml`，并输出 RMS 验收指标
（拒单率、削减次数、暂停/强平状态、触发延迟、事后误杀率）。

---

## 合规声明

本系统仅用于量化研究与教育，不构成投资建议。第一、二阶段不实现高频做市、延迟套利、
跨市场实盘套利、裸卖空、期权做市等 A 股无法合规落地的功能。任何实盘决策须独立评估，
并遵守监管机构与券商的合规要求。
