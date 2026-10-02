# 阶段验收报告（第一轮 M0~M5 + 第二轮 M1 缺陷修复）

> 范围：M0 基础设施、M1 数据层、M2 回测引擎、M3 策略层、M4 组合层、M5 风控 RMS、
> 第二轮 R2-01（第一轮 12 条缺陷修复）。
> 验收口径：合同 §四（策略规格）、§五（数学模型）、§六（引擎）、§七（风控 RMS）、
> §八（偏差与测试）、§九（评价与验收）、§十（A 股硬约束）。
> 结论：**本轮交付范围内的验收项全部通过**；评价层（M6）与报告层（M7）尚未实现，不在本轮范围内。

---

## 1. 测试执行结果

| 项目 | 结果 |
| --- | --- |
| 测试运行方式 | `python tests/run_tests.py`（零依赖运行器，与 pytest 双兼容） |
| 用例总数 | **720** |
| 本机实测结果 | 通过 715 / **跳过 1** / 失败 0（跳过项为环境门控，见下） |
| 测试模块 | 50 个 |
| 代码规模 | `src/aqs` 46 个文件约 9.0k 行；`tests` 41 个文件约 6.5k 行 |

> **关于 skip（不要读成通过）**：少数用例是**环境门控**的 —— 例如
> `test_parquet_provider_requires_pyarrow_with_actionable_error` 验证的是
> 「未装 pyarrow 时应报错并提示安装」，在本机已装 pyarrow 的前提下无法成立。
> 运行器把跳过项**单独计数并列出原因**；上表的「用例总数」是**收集数**，与是否跳过无关，
> 因此不随机器变化（这也是文档数字守护能稳定比对的原因）。

分模块用例数：

| 模块 | 用例数 | 覆盖重点 |
| --- | --- | --- |
| `test_config.py` | 15 | 配置强类型校验、未知键报错、CLI/环境变量覆盖、成本场景、RMS 规则配置 |
| `test_data_schema.py` | 21 | 列规范、复权、涨跌停推算、质量校验、**公告日口径** |
| `test_calendar.py` | 12 | 交易日查询、顺延、时段时间戳 |
| `test_data_store.py` | 28 | **PIT 访问与未来函数防护**、停牌/退市、ADV、指数成分 |
| `test_universe.py` | 14 | ST/停牌/上市天数/流动性过滤、**幸存者偏差**、历史成分 |
| `test_events.py` | 16 | 事件排序、FIFO、录制、订阅、错误策略、日内步骤优先级 |
| `test_cost.py` | 23 | 佣金/印花税/过户费/滑点/冲击成本、**成本加倍** |
| `test_matching.py` | 34 | 涨跌停、停牌、T+1、整手、部分成交、限价、资金约束、**现金不透支** |
| `test_engine_backtest.py` | 25 | **端到端 T+1、顺延、事件顺序、成本后收益、现金约束回归** |
| `test_strategy_indicators.py` | 17 | 指标手算一致性、offset 语义、NaN 严格性、两套实现一致性 |
| `test_strategy_ma_cross.py` | 14 | 与独立 oracle 逐日比对、参数校验、股票池边界 |
| `test_strategy_breakout.py` | 13 | 不含当日（未来函数防线）、严格大于、放量确认 |
| `test_strategy_volume.py` | 13 | 放量口径、阈值边界、均量基准不含当日 |
| `test_strategy_registry.py` | 12 | 配置文件与策略类一致性、未知策略/参数报错、filters 映射 |
| `test_strategy_engine.py` | 6 | 三种策略端到端、T+1 成交日、PIT 越界、可复现性 |
| `test_portfolio_sizing.py` | 23 | 等权/单票上限、**凯利公式手算**、参数校验 |
| `test_portfolio_target_weight.py` | 20 | 订单计划、约束、卖出/调仓、T+1、凯利敞口、卖先于买 |
| `test_portfolio_registry.py` | 10 | 配置驱动构建、未知键报错、默认值来源 |
| `test_portfolio_engine.py` | 9 | 组合层端到端、持仓数上限、单票上限、换手对比 |
| `test_risk_var.py` | 16 | VaR/ES 手算、持有期缩放、**突破率 ≈ 1-置信度** |
| `test_risk_rules.py` | 18 | 13 条规则逐条触发/不触发/边界 |
| `test_risk_engine.py` | 19 | 优先级、短路、reduce 累积、暂停、强平、热更新、预警、observe |
| `test_risk_stats.py` | 10 | 拒单率、**误杀率**、触发延迟 |
| `test_risk_engine_integration.py` | 10 | RMS 与引擎联调：削减、暂停、减仓、强平、诊断输出 |
| `test_defect_01_risk_latency.py` | 10 | **回归 #1** 触发延迟口径 |
| `test_defect_02_board_column.py` | 9 | **回归 #2** board 列不被覆盖 |
| `test_defect_03_order_tif.py` | 12 | **回归 #3** DAY/GTC 订单过期语义 |
| `test_defect_04_exposure_control.py` | 13 | **回归 #4** 减仓可达性与跨日补偿 |
| `test_defect_05_project_root.py` | 11 | **回归 #5** 项目根目录解析 |
| `test_defect_06_risk_audit.py` | 8 | **回归 #6** 风控审计落盘 |
| `test_defect_07_risk_stats_schema.py` | 8 | **回归 #7** stats 口径一致性 |
| `test_defect_08_listing_date.py` | 11 | **回归 #8** 上市日不得静默兜底 |
| `test_defect_09_volume_ratio_guard.py` | 13 | **回归 #9** 量比基准阈值 |
| `test_defect_10_test_hygiene.py` | 10 | **回归 #10** 测试代码坏味道（AST 守护）+ **收集规则一致性守护**（运行器 ↔ pytest 规则 ↔ unittest 加载器；含继承方法、`@property`、非 `Test*` 类的正反例） |
| `test_defect_12_demo_scope.py` | 12 | **回归 #12** 数据口径四元组与 CLI |
| `test_defect_11_docs_consistency.py` | 12 | **回归 #11** 文档与代码一致性（AST/正则守护；含 docs/00 与 CHANGELOG 数字守护、选型待核实项附录指引） |
| `test_defect_15_order_field_semantics.py` | 8 | **回归 #15** 订单字段语义（final_status / last_reject_reason / 顺延历史） |
| `test_defect_13_lot_rounding.py` | 31 | **回归 #13** 整手不变量 INV-1~INV-6（削减取整 / 零头过期 / 小数股防线 / 端到端 / DROPPED 终态） |
| `test_defect_16_provenance.py` | 11 | **回归 #16** 运行溯源（命令/Git/哈希种子写入 summary.json）+ 文档数字一致性 |
| `test_determinism.py` | 4 | **回归 #14** 跨进程可复现（两个 PYTHONHASHSEED 下数据与回测完全一致） |
| `test_no_mojibake.py` | 9 | 编码守护（UTF-8 可解码 / 无 U+FFFD / 无乱码特征串），把 PowerShell 教训变成红灯 |
| `test_packaging.py` | 22 | **M2 工程化**：许可证一致、依赖分组一致、`.gitignore`/`.gitattributes` 覆盖度、**禁止第三方回测框架进依赖**、上传脚本健壮性、README 链接与 AKShare runbook |
| `test_provider_config.py` | 7 | **M4-1** 取数行为配置：缓存/限流/重试/单位换算的默认值、`base.yaml` 携带、非法值报错、overlay 可达 |
| `test_provider_abstraction.py` | 8 | **M4-2** 数据源抽象：能力**显式声明**与缺失判定、溯源结构、降级披露文本、协议可判定 |
| `test_data_cache.py` | 14 | **M4-3** 本地缓存：参数哈希（**不含日期区间**）、schema 版本隔离、TTL 四态判定、增量合并**不丢数据**、manifest、原子写入、key 越界防护 |
| `test_ratelimit.py` | 15 | **M4-4** 限流与重试：令牌桶（时钟/睡眠可注入）、指数退避+抖动、**空结果默认不重试**、不可重试异常直抛 |
| `test_data_quality.py` | 20 | **M4-5** 质量检查 Q1~Q12（含 **Q3 手/股量级**、**Q12 幸存者偏差自检**）、阈值可配、报告结构与退出码、纯函数不改输入 |
| `test_provider_implementations.py` | 8 | **M4-6** synthetic/CSV/Parquet 三实现：能力**由实际列推断**、指数成分**区间相交**过滤（正反例）、pyarrow 环境门控 |
| `test_provider_registry.py` | 13 | **M4-7** 注册表：未知名报错**不回退**、重名需显式 overwrite、构建期协议闸门、配置层与运行层分工、能力查询 strict/degraded、root 解析口径 |
| `test_akshare_provider.py` | 23 | **M4-8** AKShare 映射骨架（全离线）：**手→股**单位换算、代码归一与非法值报错、复权因子归一（缺失不猜测）、上市日缺失报错、公告日缺失丢行、**快照累积区间闭合/同日幂等**、缓存命中不发请求 / 增量收窄 / 降级回退、重试与失败比例升级、进度审计、能力如实声明（`index_members=False`） |

---

## 2. 第二轮 M1：12 条缺陷修复验收

| # | 缺陷 | 修复要点 | 回归用例 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | `latency_days` 恒为 0 | 订单级 `acted_on=触发日, latency=0`；控制类 `acted_on=下一交易日, latency=1`；未绑定时 +1 自然日并标 `latency_approx` | `test_defect_01_*`（10 条，含跨周末） | ✅ |
| 2 | `board` 列被推断值覆盖 | 优先数据源取值，仅对缺失/非法行推断；删除死代码 fillna；`validate_bars` 报非法值 | `test_defect_02_*`（9 条） | ✅ |
| 3 | DAY 订单被误标 REJECTED | DAY 当日未成交 → EXPIRED（不计入拒单）；GTC 顺延超限 → EXPIRED；硬约束仍 REJECTED | `test_defect_03_*`（12 条） | ✅ |
| 4 | T+1 冻结下减仓不达标且无记录 | 新增 `engine/control.py`：每日按目标敞口重算、缺口记 `pending`、连续未达标触发预警、全过程入诊断 | `test_defect_04_*`（13 条） | ✅ |
| 5 | `PROJECT_ROOT` 硬编码 | 环境变量 `AQS_PROJECT_ROOT` → 向上查找 `pyproject.toml`/`.git` → 兜底 `parents[3]` | `test_defect_05_*`（11 条） | ✅ |
| 6 | RMS `audit_log` 未生效 | 默认跟随 `risk.yaml`；`engine.risk_audit_log` / 构造参数可覆盖；回测结束 `risk.close()` 落盘 | `test_defect_06_*`（8 条） | ✅ |
| 7 | `NullRiskEngine` stats 口径不明 | `RiskStats` 上移 + `enabled` 字段；两种模式诊断结构完全一致 | `test_defect_07_*`（8 条） | ✅ |
| 8 | 缺 `list_date` 静默用 `bar_seq` 兜底 | `listing_date.policy=strict`（默认）直接报错；`proxy` 降级时四处披露（source/质量/股票池/诊断） | `test_defect_08_*`（11 条） | ✅ |
| 9 | 量比极小基准导致爆炸 | 指标层 `min_base` + 策略层 `volume_min_base`（默认 10000 股） | `test_defect_09_*`（13 条） | ✅ |
| 10 | 测试代码坏味道 | `test_events.py` 改用 `Side.BUY`；`test_portfolio_engine.py` 删除冗余 `load_portfolio`；新增 AST 守护 | `test_defect_10_*`（6 条） | ✅ |
| 11 | 文档与实现不一致 | 规则条数 12→13、触发延迟口径重写、模型表 ❌→✅、用例数自动核对 | `test_defect_11_*`（9 条） | ✅ |
| 12 | `--symbols` 口径歧义 | 改为 `--generate-symbols`（保留别名+废弃提示）、新增 `--index-size`、输出「生成/有行情/指数成分/入池」四元组、`_symbol_codes` 确定性分配 | `test_defect_12_*` | ✅（见注） |

> 注：缺陷 #12 的回归用例写在 `tests/test_defect_12_demo_scope.py`。
> 两处与原始清单不符已核实并记录在 `docs/10_round2_design.md` §4：
> 缺陷 #10 的位置是 `test_events.py`（非 `test_engine_backtest.py`）；
> 缺陷 #12 的根因是「三个口径混用」而非「生成器丢标的」（实测 30→30）。

---

## 3. 合同 §八「回测偏差」逐条验收

### 3.1 未来函数测试

| 要求 | 实现防线 | 验收用例 | 结果 |
| --- | --- | --- | --- |
| 任何信号使用的数据时间戳 ≤ 决策时间 | `PITView` 在查询处断言上界，越界抛 `LookaheadError` 并记入 `violations` | `test_as_of_view_blocks_future_request`、`test_store_level_as_of_guard` | ✅ |
| T 日收盘信号只能 T+1 成交 | 引擎强制 `submit_date = next_trading_day(signal_date)`；违反抛 `FutureFunctionError` | `test_signal_fills_on_next_trading_day_open`、`test_order_time_semantics_enforced` | ✅ |
| 财务数据使用公告日而非报告期 | `select_fundamentals_asof` 只保留 `announce_date <= as_of` | `test_fundamentals_point_in_time_visibility` | ✅ |
| 策略层物理上无法取到未来数据 | 策略上下文只暴露 `PITView` | `test_strategy_cannot_read_future_data`、`test_strategy_context_is_point_in_time` | ✅ |

### 3.2 幸存者偏差测试

| 要求 | 实现 | 验收用例 | 结果 |
| --- | --- | --- | --- |
| 股票池包含已退市股票 | `all_listed(day)` 含退市股在退市前的交易日 | `test_delisted_symbol_included_before_delisting` | ✅ |
| 退市后不可交易 | 撮合按 `delist_date` 拒绝且不重试 | `test_delisted_is_not_retryable` | ✅ |
| 使用历史指数成分 | `index_members` 按 `effective_from/effective_to` 生效 | `test_index_mode_uses_historical_membership` | ✅ |
| 数据源自检 | 候选池含退市标的（`docs/11` Q12 检查） | 设计已定义，随 AKShare 接入交付 | 🟡 |

### 3.3 过拟合 / 稳健性（部分，需 M6 完善）

| 要求 | 本轮状态 | 说明 |
| --- | --- | --- |
| 样本内/样本外分割 | ⏭ 待 M6 | 引擎支持任意 `start/end`；分割与衰减率见 `docs/13_metrics.md` |
| 滚动窗口回测 | ⏭ 待 M6 | 同上 |
| 参数敏感性分析 | 🟢 基础设施就绪 | 全参数配置驱动 + `StrategySpec`/`PortfolioConfig` 覆盖 |
| **成本加倍后策略是否仍有效** | ✅ 已具备 | `costs.scale` + `configs/costs.yaml`；`test_cost_doubling_doubles_total_cost` |
| 蒙特卡洛模拟 | ⏭ 第三阶段 | 设计见 `docs/13_metrics.md` §6（Block Bootstrap） |

### 3.4 压力测试与成本敏感性

| 要求 | 本轮状态 | 说明 |
| --- | --- | --- |
| 2008 金融危机 / 2015 股灾 / 2020 疫情 | 🟡 数据侧就绪 | `aqs.data.synthetic.CRISIS_WINDOWS` 内置三段窗口，`crisis=[...]` 可生成 |
| 极端波动、流动性枯竭 | ✅ 可构造 | 停牌/一字涨停/低成交量 + 参与率上限；`test_partial_fill_spreads_over_days` |
| 滑点/佣金/印花税加倍 | ✅ | `configs/costs.yaml` 四个场景 |

---

## 4. 合同 §十「A 股硬约束」逐条验收

| 约束 | 实现位置 | 验收用例 | 结果 |
| --- | --- | --- | --- |
| T+1（买入当日不可卖） | `Account.apply_fill` + 每日 PRE_OPEN `unlock_t1()` | `test_t_plus_one_blocks_same_day_sell` | ✅ |
| 涨跌停买不进 | 开盘触及涨停 → 不成交并顺延 | `test_limit_up_blocks_buy`、`test_limit_up_defers_execution` | ✅ |
| 涨跌停卖不出 | 开盘触及跌停 → 不成交并顺延 | `test_limit_down_blocks_sell` | ✅ |
| 涨停可卖 / 跌停可买 | 只有逆势方向被封板阻断 | `test_limit_up_allows_sell`、`test_limit_down_allows_buy` | ✅ |
| 停牌 | 停牌/零成交量不成交并顺延 | `test_suspension_blocks_and_defers` | ✅ |
| ST / 退市 | 股票池剔除 ST；退市不可交易 | `test_universe_applies_all_filters` | ✅ |
| 复权 | 原始价用于成交/涨跌停/费用，后复权价用于信号；除权日现金入账 | `test_adjusted_prices_use_first_bar_as_base` | ✅ |
| 最小 100 股 | 买入整手，不足一手拒绝；卖出允许零股 | `test_buy_quantity_floored_to_lot` | ✅ |
| 卖空限制 | 卖出受持仓约束；无持仓直接拒绝 | `test_gross_exposure_blocks_short_selling` | ✅ |
| 印花税 / 佣金 / 过户费 | 成本模型（印花税按日期分段、最低佣金） | `test_cost.py`（23 条） | ✅ |
| 上市日 / 幸存者偏差数据完整性 | `listing_date.policy` + 退市股入池 + 质量检查 | `test_defect_08_*`、`test_universe.py` | ✅ |
| 程序化交易合规与券商接口限制 | RMS 下单频率与撤单率规则；系统不连接任何券商接口 | `test_order_frequency_rule`、`test_cancel_ratio_rule` | ✅ |

---

## 5. 回测引擎验收（合同 §九）

| 验收项 | 验收用例 | 结果 |
| --- | --- | --- |
| 事件顺序正确 | `test_event_sequence_within_a_day`、`test_event_counts_match_engine_loop` | ✅ |
| 无未来函数 | 见 §3.1 | ✅ |
| T+1 正确 | `test_signal_fills_on_next_trading_day_open`、`test_buy_then_sell_respects_t_plus_one` | ✅ |
| 涨跌停正确 | `test_limit_up_defers_execution`、`test_limit_down_blocks_sell` | ✅ |
| 停牌顺延正确 | `test_suspension_defers_execution`、`test_order_expires_after_max_defer_days` | ✅ |
| 订单有效期语义正确（新增） | `test_defect_03_*`（DAY 过期 / GTC 顺延 / 硬拒绝） | ✅ |
| 成本后收益正确 | `test_costs_reduce_total_value`、`test_cost_doubling_doubles_total_cost` | ✅ |
| 现金不透支 | `test_affordable_quantity_includes_impact_cost`、`test_cash_never_negative_across_whole_backtest` | ✅ |
| 撮合统计可审计 | `diagnostics.match_stats`（filled/limit_up/suspended/t1_lock…） | ✅ |
| 风控已接入且可关闭/观察 | `diagnostics.risk_engine`、`risk.*`；`enabled=false` 与 `mode=observe` 均有测试 | ✅ |
| 减仓可追溯 | `test_defect_04_*`（pending / deferred_events / unmet_days / history） | ✅ |
| RMS 审计落盘 | `test_defect_06_*`（JSONL 文件 + `audit_records`） | ✅ |

---

## 6. M3 策略层验收（合同 §四）

| 合同要求 | 实现 | 验收用例 | 结果 |
| --- | --- | --- | --- |
| 均线交叉（MA5 上穿 MA20 买、下穿卖） | `MACrossStrategy`（窗口可配） | `test_signals_match_oracle_every_day`（与独立 oracle 逐日比对） | ✅ |
| 价格突破（收盘 > 过去 20 日最高价，**不含 T 日**） | `BreakoutStrategy` | `test_excludes_today_high_proving_no_lookahead`、`test_equal_to_prior_high_does_not_trigger` | ✅ |
| 成交量配合（量 > 过去 20 日均量 × 1.5） | `VolumeStrategy`（均量不含当日 + `volume_min_base` 阈值） | `test_volume_ma_excludes_current_day`、`test_defect_09_*` | ✅ |
| 过滤：ST / 上市不足 60 日 / 20 日均额 < 5000 万 | 由数据层股票池完成，策略只遍历 `ctx.universe` | `test_only_universe_symbols_get_signals`、`test_signals_never_precede_universe_membership` | ✅ |
| 执行：T+1 开盘；涨停/停牌顺延 | 引擎强制 | `test_every_fill_is_one_day_after_signal` | ✅ |
| 仓位：等权、最多 N 只、单票上限 10% | M4 组合层 | `test_max_positions_limits_new_positions`、`test_single_name_weight_cap_respected` | ✅ |
| 全参数可配置 | YAML 驱动 + 未知键报错 | `test_all_shipped_configs_are_valid_and_buildable` | ✅ |

---

## 7. M4 组合层验收（合同 §四 仓位 + §五 模型）

| 合同要求 | 实现 | 验收用例 | 结果 |
| --- | --- | --- | --- |
| 等权 | `min(E/n, 单票上限)` | `test_equal_weights`、`test_single_entry_produces_lot_sized_buy` | ✅ |
| 单票上限（默认 10%） | `max_weight_per_symbol` | `test_max_weight_per_symbol_respected` | ✅ |
| 最多持有 N 只（默认 10） | `max_positions` + 在途买单占名额 | `test_position_count_never_exceeds_max_positions` | ✅ |
| 现金管理 | `cash_buffer` + 卖出回款预估 + 卖单先于买单 | `test_sells_come_before_buys`、`test_cash_never_negative` | ✅ |
| **凯利公式简化版（半凯利）** | `kelly_binary`/`kelly_continuous` + 样本不足保护 | `test_kelly_binary_hand_computed`、`test_kelly_exposure_scales_down_buys` | ✅ |
| 卖出与 T+1 | 卖出受可卖量约束 | `test_t_plus_one_blocked_position_produces_no_sell` | ✅ |
| 调仓（可选） | `rebalance=True` 由当日信号决定目标集合 | `test_rebalance_increases_turnover` | ✅ |

---

## 8. M5 风控 RMS 验收（合同 §七）

| 合同要求 | 实现 | 验收用例 | 结果 |
| --- | --- | --- | --- |
| 接口 `CheckOrder → accept/reject_reason/modified_quantity/action` | `RiskEngine.check_order` + `CheckOrder` 别名 | `test_risk_engine.py`（19 条） | ✅ |
| 单笔订单最大数量 / 金额 | `max_order_quantity` / `max_order_notional` | `test_max_order_quantity_reduces_fill` | ✅ |
| 单日最大交易量 | `max_trade_volume_daily`（ADV × 比例） | `test_max_trade_volume_daily_rule` | ✅ |
| 单票最大持仓 / 行业暴露 / 总敞口 | `max_position_per_symbol` / `industry_exposure` / `gross_exposure` | `test_risk_rules.py`（18 条） | ✅ |
| 价格偏离 / 下单频率 / 撤单率 | `price_deviation` / `order_frequency` / `cancel_ratio` | 同上 | ✅ |
| 当日最大亏损 / 最大回撤动作 | `daily_loss_limit`（pause）/ `max_drawdown_action`（预警/减仓/强平） | `test_daily_loss_limit_pauses_trading`、`test_max_drawdown_forces_liquidation` | ✅ |
| **VaR/ES 历史模拟法** | `historical_var_es` + `portfolio_var_limit` 规则 | `test_risk_var.py`（16 条）；**突破率 5.45% vs 期望 5%** | ✅ |
| 规则优先级 / 短路 / 审计 / 热更新 / 预警 / 暂停 / 强平 | `RuleRiskEngine` | `test_risk_engine.py` | ✅ |
| 可扩展（非示例类） | `RiskRule` 抽象 + 注册表 + 配置驱动 + observe 模式 | `test_unknown_rule_rejected`、`test_risk_observe_mode_allows_but_records` | ✅ |
| 验收指标：拒单率 | `RiskStats.reject_rate`（含 `enabled` 口径） | `test_defect_07_*` | ✅ |
| 验收指标：误杀率 | `evaluate_rejections`（事后 N 日方向判定） | `test_risk_stats.py` | ✅ |
| 验收指标：触发延迟 | `RiskTrigger.latency_days`（订单级 0 / 控制类 1 个交易日） | `test_defect_01_*` | ✅ |

---

## 9. 演示回测（合成数据，系统自检）

### 9.0 口径定义（本节所有数字均按此口径，且必须能由落盘文件复核）

| 术语 | 严格定义 |
| --- | --- |
| **进程返回码** | `python examples/demo_backtest.py ...` 的 OS 退出码。0 表示命令正常结束、未抛异常，**与交易结果无关** |
| 成交笔数 | `reports/<run>/trades.csv` 的**行数**（一笔成交 = 一行；部分成交按实际成交次数各占一行） |
| 平仓笔数 | `trades.csv` 中 `tag='exit'` 的行数 |
| 开仓笔数 | `trades.csv` 中 `tag='entry'` 的行数 |
| 拒单率 | `risk.stats.reject_rate = 风控拒单数 / 订单检查数`，口径见 `docs/06_risk_rms.md` §4 |
| 误杀率 | 被拦截订单在事后 N 个交易日的方向判定比例，口径见 `docs/06_risk_rms.md` §4 |

> ⚠️ **不得再用「exit=0」表述进程返回码** —— `exit` 在本项目中是领域字段值
> （`tag ∈ {entry, exit, rebalance, risk_reduce, risk_close}`），同名会造成误读。
> 详见 `docs/17_round3_diagnostics.md` §1。

### 9.1 更正记录（第三轮 D1）

1. **「三种策略均 exit=0」的表述有误**：其原意是「三种策略的**进程返回码**均为 0」，
   与 `tag='exit'` 的平仓笔数无关。平仓笔数实际为 ma_cross 54、breakout 22、volume 75
   （来源：`reports/<run>/trades.csv`，实测见 `docs/17_round3_diagnostics.md` §1.2）。
2. **原 §9 指标表已作废**，原因有两条，缺一不可：
   - **不可追溯**：该表为「30 只标的」的运行结果，但 `reports/` 下落盘的是一次
     **12 只标的 / 259 个交易日** 的运行（`reports/ma_cross/summary.json` 中
     `data_scope.generated=12`、`sessions=259`），表内数字无法用任何落盘文件复核；
   - **不可复现**：修复缺陷 #14 之前，同一命令连续三次运行的订单数为 313 / 537 / 463
     （根因与证据见 `docs/17_round3_diagnostics.md` §5），因此该表在修复前无法通过重跑校验。
3. 修正动作：缺陷 #14 修复 + 缺陷 #13 修复完成后，用**固定命令**重新生成三份报告并落盘，
   再把真实数字写回本节，同时增加「文档数字 ← `reports/*/summary.json`」的一致性红灯测试。

### 9.2 当前落盘数据的真实数字（缺陷 #13/#14/#15 修复后重新生成）

**生成命令**（三份报告使用同一条命令，仅策略名不同）：

```powershell
python examples\demo_backtest.py --strategy <ma_cross|breakout|volume> --generate-symbols 12 --end 2022-12-30
```

**数据口径**：合成数据，生成 12 只 / 有行情 12 只 / 指数成分(日均) 12.0 只 / 入池(日均) 6.8 只（0~10）；
区间 2022-01-04 ~ 2022-12-30，共 259 个交易日；初始资金 1,000,000 元；`engine.lot_size=100`。

**溯源**：`reports/<run>/summary.json` 的 `diagnostics.invocation` 记录了完整命令行、
Git HEAD、Python 版本与 `PYTHONHASHSEED`。本次三份报告的 `git` 均为 `d15ab7c`。

| run | trades.csv 行数 | 开仓(entry) | 平仓(exit) | 减仓(risk_reduce) | orders.csv 行数 | filled | expired | rejected | 累计收益 | 年化 | 最大回撤 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ma_cross | 112 | 58 | 54 | 0 | 112 | 112 | 0 | 0 | -5.61% | -5.47% | -8.01% |
| breakout | 48 | 26 | 22 | 0 | 48 | 48 | 0 | 0 | +8.82% | +8.57% | -2.92% |
| volume | 211 | 80 | 75 | 56 | 222 | 211 | 11 | 0 | -15.94% | -15.55% | -16.63% |

进程返回码：三者均为 **0**。

RMS：订单检查 225 / 96 / 499 次，风控拒单 **0 / 0 / 0**，`below_lot` **0 / 0 / 0**，
`orders_dropped_below_lot` **0 / 0 / 0**。
撮合统计 `match_stats`：ma_cross `{filled:112, suspended:1}`、breakout `{filled:48}`、
volume `{filled:211, no_quote:66}`（volume 的 11 笔 `expired` 来自 `no_quote` 顺延超过
`max_defer_days`，属**正常过期**，不计入拒单）。

**与修复前的对比（同一命令）**：

| run | 修复前 rejected | 修复后 rejected | 修复前 lot_size 误判 | 修复后 |
| --- | --- | --- | --- | --- |
| ma_cross | 12 | **0** | 8 | **0** |
| breakout | 14 | **0** | 9 | **0** |
| volume | 23 | **0** | 14 | **0** |

> 修复前的「拒单」几乎全部是缺陷 #13 造成的**误判**（部分成交后剩余不足一手被当成硬拒单），
> 并非真实风控拦截。诊断与证据见 `docs/17_round3_diagnostics.md` §2。

> ⚠️ 上表仅用于证明**链路可跑通**与**口径可复核**，不作为策略绩效结论。
> 其中「指数成分(日均) 12.0 只」意味着 12 只全部进入指数 —— 因此这组报告**不覆盖**
> 指数定期调整（换入换出）路径；覆盖该路径的用例见 `tests/test_defect_12_demo_scope.py`
> 与 `docs/17_round3_diagnostics.md` §5.2。
>
> ⚠️ **该结果基于合成随机行情，仅用于验证系统链路，不代表任何策略在真实市场上的表现**。
> 误杀率在随机数据上接近 50%（等价抛硬币），符合预期 —— 它只有在真实行情上才有解释力。
> 评价指标体系（夏普/Calmar/Sortino/容量/滑点敏感性）待 M6 交付；报告图表待 M7 交付。

---

## 10. 本轮未完成项（下一轮开工清单）

1. **R2-02 工程化**：文件已全部交付（LICENSE/NOTICE/.gitignore/.gitattributes/依赖分组/
   CHANGELOG/README runbook/上传脚本）并**本地提交 `2c46504`**；
   **仅剩推送**——本机开发环境对外 HTTPS 全部不可达，需在联网环境执行
   `pwsh -File tools\upload_github.ps1 -NoCommit`。
2. **R2-03 框架选型**：边界纪律已写入 `docs/DEVELOPMENT.md` §7；
   许可证/版本核实项仍待联网执行（`docs/12_framework_matrix.md` §6 的 V1~V6）。
3. **R2-04~R2-05 数据源**：DataProvider 抽象、缓存/限流/重试、AKShare 适配与数据质量 Q1~Q12。
4. **R2-06 M6 评价层**：年化/夏普/Sortino/Calmar/信息比率/换手/成本后收益/容量/滑点敏感性、
   基准对比（沪深300/中证500/中证1000）、行业与风格暴露、成本/信号/因子归因、
   样本内外分割、滚动窗口、蒙特卡洛。
5. **R2-07 M7 报告层**：净值曲线、回撤曲线、持仓、交易明细、风险暴露、归因、RMS 与引擎验收报告。
6. **R2-08 偏差与压力测试套件**：三类偏差 + 三段危机窗口 + 极端波动 + 流动性枯竭 + 成本敏感性。
7. **R2-09 第二阶段设计**：多因子（FF/Barra）、组合优化（均值-方差 + 换手惩罚）、
   GARCH(1,1)、协整配对、因子风险模型。
8. 真实数据接入与涨跌停/ST 规则在真实数据上的回归校验（依赖 R2-04/R2-05）。
9. `PYTHONHASHSEED` 未由 CLI 固定：当前实现已做到与哈希顺序无关，
   但 CLI 仍未打印提示行（`summary.json` 已记录该环境变量）。

---

## 11. 第三轮交付摘要（D1~D4 + M2）

| 项目 | 结果 |
| --- | --- |
| 缺陷 #13 订单整手（四层根因） | 已修复；不变量 I1~I5 全部归零；拒单数 57/24/68 → **1/0/0** |
| 缺陷 #14 回测不可复现 | 已修复（`set` 迭代 → `sorted`）；跨进程守护 4 条 |
| 缺陷 #15 订单字段语义 | 已修复（`final_status` / `last_reject_reason` / 顺延历史） |
| 缺陷 #16 报告数字可追溯 | 已修复（`summary.json. diagnostics.invocation`） |
| D1 口径更正 | `docs/03` §9 重写：口径定义 + 更正记录 + 重新生成后的真实数字 |
| M2 工程化 | 文件全部交付并本地提交 `2c46504`；**推送待联网环境** |
| 测试 | **收集 720 个用例**（50 个模块；第一轮 398 → 本轮 720） |
| 新增机械守护 | 整手不变量 / 跨进程确定性 / 乱码编码 / 工程化一致性 / 文档数字一致性 |
