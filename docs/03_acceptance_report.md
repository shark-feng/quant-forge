# 第一阶段验收报告（R1~R5 交付）

> 范围：M0 基础设施、M1 数据层、M2 回测引擎、M3 策略层、M4 组合层、M5 风控 RMS。
> 验收口径：合同 §四（策略规格）、§五（数学模型）、§六（引擎）、§七（风控 RMS）、§八（偏差与测试）、§九（评价与验收）、§十（A 股硬约束）。
> 结论：**本轮交付范围内的验收项全部通过**；评价层（M6）与报告层（M7）尚未实现，不在本轮范围内。

---

## 1. 测试执行结果

| 项目 | 结果 |
|---|---|
| 测试运行方式 | `python tests/run_tests.py`（零依赖运行器，与 pytest 双兼容） |
| 用例总数 | **398** |
| 通过 | **398** |
| 失败 | 0 |
| 测试模块 | 24 个 |
| 代码规模 | `src/aqs` 44 个文件 8,379 行；`tests` 29 个文件 4,836 行 |

分模块用例数：

| 模块 | 用例数 | 覆盖重点 |
|---|---|---|
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

---

## 2. 合同 §八「回测偏差」逐条验收

### 2.1 未来函数测试

| 要求 | 实现防线 | 验收用例 | 结果 |
|---|---|---|---|
| 任何信号使用的数据时间戳 ≤ 决策时间 | `PITView` 在查询处断言上界，越界抛 `LookaheadError` 并记入 `violations` | `test_as_of_view_blocks_future_request`、`test_as_of_view_blocks_future_bars_on_and_universe`、`test_store_level_as_of_guard` | ✅ |
| T 日收盘信号只能 T+1 成交 | 引擎强制 `submit_date = next_trading_day(signal_date)`；`Broker.create_order` 对 `submit_date <= signal_date` 抛 `FutureFunctionError` | `test_signal_fills_on_next_trading_day_open`、`test_no_fill_on_signal_day`、`test_order_time_semantics_enforced` | ✅ |
| 财务数据使用公告日而非报告期 | `select_fundamentals_asof` 只保留 `announce_date <= as_of`，同报告期取最新公告 | `test_fundamentals_point_in_time_visibility`（报告期已过但未公告 → 不可见）、`test_fundamentals_follow_announce_date` | ✅ |
| 策略层物理上无法取到未来数据 | 策略上下文只暴露 `PITView`；违规策略会被抛错终止 | `test_strategy_cannot_read_future_data` | ✅ |

### 2.2 幸存者偏差测试

| 要求 | 实现 | 验收用例 | 结果 |
|---|---|---|---|
| 股票池包含已退市股票 | `all_listed(day)` 返回当日有行情的全部标的（含退市股在退市前的交易日） | `test_delisted_symbol_included_before_delisting` | ✅ |
| 退市后不可交易 | 撮合按 `delist_date` 拒绝且**不重试**；日历顺延跳过退市 | `test_delisted_is_not_retryable`、`test_delisted_symbol_absent_after_delist_date` | ✅ |
| 使用历史指数成分 | `index_members` 表按 `effective_from/effective_to` 生效；`mode=index` 使用当日实际成分 | `test_index_mode_uses_historical_membership`、`test_index_members_effective_window` | ✅ |

### 2.3 过拟合 / 稳健性测试（部分，需 M6 完善）

| 要求 | 本轮状态 | 说明 |
|---|---|---|
| 样本内/样本外分割 | ⏭ 待 M6 | 引擎已支持任意 `start/end`，可直接切分区间；评价指标待 M6 |
| 滚动窗口回测 | ⏭ 待 M6 | 同上 |
| 参数敏感性分析 | 🟢 基础设施就绪 | 全部参数由配置驱动：`BaseConfig.with_overlay` 批量生成参数组合，`StrategySpec.params` 直接构造策略；三种策略的窗口/倍数均可覆盖 |
| 蒙特卡洛模拟 | ⏭ 第三阶段 | — |
| **交易成本加倍后策略是否仍有效** | ✅ 已具备 | `costs.scale` + 场景文件 `configs/costs.yaml`；`test_cost_doubling_doubles_total_cost`、`test_cost_doubling_scales_total_by_two` 验证成本严格翻倍；演示脚本支持 `--cost-scale 2.0` |

### 2.4 压力测试与成本敏感性

| 要求 | 本轮状态 | 说明 |
|---|---|---|
| 2008 金融危机 / 2015 股灾 / 2020 疫情 | 🟡 数据侧就绪 | `aqs.data.synthetic.CRISIS_WINDOWS` 内置三段危机窗口（漂移 + 波动放大），可用 `crisis=["gfc_2008"]` 生成对应压力行情；真实数据接入后可直接切片 |
| 极端波动、流动性枯竭 | ✅ 可构造 | 合成数据的停牌/一字涨停/低成交量场景 + 撮合的参与率上限（`test_partial_fill_spreads_over_days`、`test_participation_cap_creates_partial_fill`） |
| 滑点/佣金/印花税加倍 | ✅ | `configs/costs.yaml` 的 `double_cost` / `triple_cost` / `high_slippage` / `pessimistic_commission` 场景 |

---

## 3. 合同 §十「A 股硬约束」逐条验收

| 约束 | 实现位置 | 验收用例 | 结果 |
|---|---|---|---|
| T+1（买入当日不可卖） | `Account.apply_fill` 不增加可卖量；每日 PRE_OPEN `unlock_t1()` | `test_t_plus_one_blocks_same_day_sell`、`test_buy_then_sell_respects_t_plus_one` | ✅ |
| 涨跌停买不进 | 开盘价触及涨停则买单不成交并顺延 | `test_limit_up_blocks_buy`、`test_limit_up_defers_execution` | ✅ |
| 涨跌停卖不出 | 开盘价触及跌停则卖单不成交并顺延 | `test_limit_down_blocks_sell` | ✅ |
| 涨停可卖 / 跌停可买 | 只有逆势方向被封板阻断 | `test_limit_up_allows_sell`、`test_limit_down_allows_buy` | ✅ |
| 停牌 | 停牌/零成交量不成交并顺延 | `test_suspension_blocks_and_defers`、`test_suspension_defers_execution` | ✅ |
| ST / 退市 | 股票池剔除 ST；退市不可交易 | `test_universe_applies_all_filters`、`test_delisted_is_not_retryable` | ✅ |
| 复权 | 原始价用于成交/涨跌停/费用，后复权价用于信号；除权日现金入账 | `test_adjusted_prices_use_first_bar_as_base`、`test_adjusted_prices_available_from_store` | ✅ |
| 最小 100 股 | 买入向下取整到整手，不足一手拒绝；卖出允许零股 | `test_buy_quantity_floored_to_lot`、`test_odd_lot_sell_is_allowed` | ✅ |
| 卖空限制 | 卖出数量受可卖持仓约束，无持仓直接拒绝 | `test_sell_without_position_is_terminal`、`test_sell_reduces_to_available_quantity` | ✅ |
| 印花税 / 佣金 / 过户费 | 成本模型（含印花税按日期分段、最低佣金） | `test_cost.py` 全部 23 个用例 | ✅ |
| 程序化交易合规与券商接口限制 | RMS 已实现下单频率与撤单率限制规则；本系统不连接任何券商接口 | `test_order_frequency_rule`、`test_cancel_ratio_rule`、`test_risk_config_rules_sorted_and_unique` | ✅ |

---

## 4. 回测引擎验收（合同 §九）

| 验收项 | 验收用例 | 结果 |
|---|---|---|
| 事件顺序正确 | `test_event_sequence_within_a_day`、`test_event_counts_match_engine_loop`、`test_market_data_event_order_is_open_then_close` | ✅ |
| 无未来函数 | 见 §2.1 | ✅ |
| T+1 正确 | `test_signal_fills_on_next_trading_day_open`、`test_buy_then_sell_respects_t_plus_one` | ✅ |
| 涨跌停正确 | `test_limit_up_defers_execution`、`test_limit_down_blocks_sell` | ✅ |
| 停牌顺延正确 | `test_suspension_defers_execution`、`test_order_expires_after_max_defer_days` | ✅ |
| 成本后收益正确 | `test_costs_reduce_total_value`、`test_cost_doubling_doubles_total_cost`、`test_impact_cost_present_when_adv_available` | ✅ |
| 撮合统计可审计 | `res.diagnostics["match_stats"]` 记录 filled/limit_up/suspended/t1_lock/… 次数 | ✅ |
| 风控已接入且可关闭/观察 | `diagnostics.risk_engine`、`risk_rules`、`risk.*` 指标；`risk.enabled=false` 与 `mode=observe` 均有测试 | ✅ |

---

## 5. 回测引擎验收补充：现金约束（回归）

| 场景 | 验收用例 | 结果 |
|---|---|---|
| 单笔订单可买数量必须与成本模型完全一致（含冲击成本） | `test_affordable_quantity_includes_impact_cost` | ✅ |
| 同一交易日多笔买单同时撮合不得透支现金 | `test_multiple_buy_orders_same_day_never_overdraw_cash` | ✅ |
| 全区间逐日现金非负 | `test_cash_never_negative_across_whole_backtest` | ✅ |

---

## 6. M3 策略层验收（合同 §四）

| 合同要求 | 实现 | 验收用例 | 结果 |
|---|---|---|---|
| 均线交叉：MA5 上穿 MA20 买入、下穿卖出 | `MACrossStrategy`（窗口可配） | `test_signals_match_oracle_every_day`（与独立 oracle 逐日比对）、`test_entry_only_on_golden_cross_day` | ✅ |
| 价格突破：收盘价 > 过去 20 日最高价（**不含 T 日**） | `BreakoutStrategy` | `test_excludes_today_high_proving_no_lookahead`、`test_equal_to_prior_high_does_not_trigger`、`test_excludes_today_false_is_rejected` | ✅ |
| 成交量配合：成交量 > 过去 20 日均量 × 1.5 | `VolumeStrategy`（`volume_ma_exclude_today` 默认 true） | `test_volume_ma_excludes_current_day`、`test_ratio_boundary_is_strict` | ✅ |
| 放量 + 价涨买入 / 放量 + 价跌卖出 | `VolumeStrategy` | `test_volume_up_triggers_entry`、`test_volume_down_triggers_exit`、`test_low_volume_up_does_not_trigger` | ✅ |
| 过滤：剔除 ST、上市不足 60 日、20 日均额 < 5000 万 | 由数据层股票池完成，策略只遍历 `ctx.universe` | `filters_to_data_overrides` 映射测试 + `test_only_universe_symbols_get_signals` + `test_signals_never_precede_universe_membership` | ✅ |
| 执行：T+1 开盘成交；涨停/停牌顺延 | 引擎强制（M2） | `test_every_fill_is_one_day_after_signal`、`test_suspension_defers_execution`、`test_limit_up_defers_execution` | ✅ |
| 仓位：等权、最多 N 只、单票上限 10% | ⏳ M4（配置文件段位已就绪：`spec.portfolio`） | 演示脚本用临时实现跑通 | 🟡 |
| 所有策略可配置：均线周期、突破窗口、放量倍数、持仓数、单票上限、滑点、手续费 | ✅ 全部由 YAML 驱动 | `test_all_shipped_configs_are_valid_and_buildable`、`test_unknown_param_rejected`、`test_invalid_windows_rejected` | ✅ |
| 无未来函数 | 策略只用 `PITView`；突破/量比明确不含当日 | `test_strategy_context_is_point_in_time`（越界抛 `LookaheadError`） | ✅ |

### 6.1 本次交付中发现并修复的缺陷（回归用例已补）

| 缺陷 | 影响 | 修复 | 回归用例 |
|---|---|---|---|
| `MatchingEngine.affordable_quantity` 未计入**冲击成本** | 同一交易日多笔买单撮合时可能现金透支（实测 -0.23 元，`Account` 抛 `EngineError` 中断回测） | 改为用成本模型**精确校验**（滑点+佣金+过户费+冲击成本）并逐手回退，另加可配置现金缓冲 | `test_affordable_quantity_includes_impact_cost`、`test_multiple_buy_orders_same_day_never_overdraw_cash`、`test_cash_never_negative_across_whole_backtest` |
| `BreakoutStrategy` 的 `low_field` 校验用了错误的候选集合 | `configs/strategies/breakout.yaml` 无法构建（配置与实现不一致） | 拆分 `_HIGH_FIELDS` / `_LOW_FIELDS` | `test_all_shipped_configs_are_valid_and_buildable`（该用例正是为捕捉此类问题而写） |
| 组合层只按**已成交持仓**计算仓位名额，未计入**在途买单** | 涨停/停牌顺延中的买单过几天才成交，导致持仓数突破 `max_positions`（实测 6 > 5） | 名额同时计入持仓与在途买单；卖出未成交不释放名额（保守口径，保证硬上限） | `test_position_count_never_exceeds_max_positions`、`test_max_positions_limits_holdings` |
| 风控规则只在**下单时**被评估 | 当天没有订单时，回撤/当日亏损规则根本不会触发（回撤 6% 也不强平） | 引擎每日盘后独立调用 `evaluate_controls`，执行 `portfolio_level=True` 的规则 | `test_daily_loss_limit_pauses_trading`、`test_max_drawdown_forces_liquidation` |
| `gross_exposure` 规则对「卖出超过持仓」直接整单拒绝 | 合法的清仓卖出被误杀（T+1 测试失败） | 卖空限制以**持仓总量**判断，超量时削减而非拒绝；真正的 T+1 约束由撮合层在执行当日强制 | `test_gross_exposure_blocks_short_selling`、`test_buy_then_sell_respects_t_plus_one` |

---

## 7. M4 组合层验收（合同 §四 仓位 + §五 模型）

| 合同要求 | 实现 | 验收用例 | 结果 |
|---|---|---|---|
| 等权 | `weighting: equal`，权重 `min(E/n, 单票上限)` | `test_equal_weights`、`test_single_entry_produces_lot_sized_buy` | ✅ |
| 单票上限（默认 10%） | `max_weight_per_symbol` | `test_max_weight_per_symbol_respected`、`test_single_name_weight_cap_respected` | ✅ |
| 最多持有 N 只（默认 10） | `max_positions`，按 score 降序取前 N | `test_max_positions_limits_new_positions`、`test_position_count_never_exceeds_max_positions` | ✅ |
| 现金管理 | `cash_buffer` + 卖出回款预估 + 卖单先于买单 | `test_cash_buffer_limits_total_investment`、`test_sells_come_before_buys`、`test_cash_never_negative` | ✅ |
| **凯利公式简化版（半凯利）** | `kelly_binary(p,b)=p-q/b`（含连续版 μ/σ²），半凯利系数 0.5，敞口上限、样本不足保护 | `test_kelly_binary_hand_computed`、`test_half_kelly_fraction_applied`、`test_kelly_cap_limits_exposure`、`test_kelly_insufficient_sample_uses_initial_exposure` | ✅ |
| 参数可配置 | `portfolio:` 段严格校验（未知键报错） | `test_unknown_key_rejected`、`test_invalid_weighting_rejected`、`test_strategy_section_overrides_defaults` | ✅ |
| 卖出与 T+1 | 卖出受可卖量约束；不可卖时不生成订单 | `test_exit_signal_sells_sellable_quantity`、`test_t_plus_one_blocked_position_produces_no_sell`、`test_buy_then_sell_respects_t_plus_one` | ✅ |
| 调仓（可选） | `rebalance=True` 时目标集合由当日信号决定 | `test_rebalance_increases_turnover`、`test_rebalance_enabled_sells_non_targets` | ✅ |

---

## 8. M5 风控 RMS 验收（合同 §七）

| 合同要求 | 实现 | 验收用例 | 结果 |
|---|---|---|---|
| 接口 `CheckOrder → accept/reject_reason/modified_quantity/action` | `RiskEngine.check_order` + `CheckOrder` 别名，返回 `RiskDecision` | `test_risk_engine.py` 全部 19 个用例 | ✅ |
| 单笔订单最大数量 | `max_order_quantity`（reduce/reject） | `test_max_order_quantity_rule`、`test_max_order_quantity_reduces_fill` | ✅ |
| 单日最大交易量 | `max_trade_volume_daily`（ADV × 比例） | `test_max_trade_volume_daily_rule` | ✅ |
| 单票最大持仓 | `max_position_per_symbol` | `test_max_position_per_symbol_rule`、`test_single_name_weight_cap_enforced_by_rms` | ✅ |
| 行业暴露上限 | `industry_exposure`（无行业数据时跳过并记录） | `test_industry_exposure_rule` | ✅ |
| 总市值敞口 | `gross_exposure`（含卖空限制） | `test_gross_exposure_rule`、`test_gross_exposure_blocks_short_selling` | ✅ |
| 价格偏离限制 | `price_deviation` | `test_price_deviation_rule` | ✅ |
| 下单频率限制 | `order_frequency`（日频下仅当日笔数有效，分钟级需 Tick 回放） | `test_order_frequency_rule` | ✅（含限制声明） |
| 撤单率限制 | `cancel_ratio`（样本不足不判定） | `test_cancel_ratio_rule` | ✅ |
| 当日最大亏损 | `daily_loss_limit`（触发暂停） | `test_daily_loss_limit_rule`、`test_daily_loss_limit_pauses_trading` | ✅ |
| 最大回撤触发动作 | `max_drawdown_action`（预警 / 减仓至半仓 / 强制平仓） | `test_max_drawdown_rule_bands`、`test_max_drawdown_forces_liquidation`、`test_max_drawdown_reduces_exposure` | ✅ |
| **VaR/ES 历史模拟法**（§五 第一阶段必做） | `historical_var_es` + `portfolio_var_limit` 规则 | `test_risk_var.py` 16 个用例；**突破率 5.45% vs 期望 5%** | ✅ |
| 规则优先级 | `priority` 升序，`reject/pause/force_close` 短路，`reduce` 取最严格 | `test_priority_order_is_respected`、`test_reject_short_circuits`、`test_reduce_accumulates_to_minimum` | ✅ |
| 审计日志 | `AuditStream` 记录每条决策（规则名/动作/数量/原因） | `test_audit_log_records_decisions` | ✅ |
| 热更新 | `update_rules` / `reload_rules`（重新读取配置文件） | `test_hot_reload_replaces_rules`、`test_engine_wires_config_file` | ✅ |
| 实时预警 | `alerts()` / `drain_alerts()`（暂停、强平事件） | `test_alerts_are_generated_and_drainable` | ✅ |
| 暂停交易 | `pause()/resume()`，暂停后订单被拒、买入计划被丢弃 | `test_pause_stops_further_orders`、`test_risk_engine_integration.py` | ✅ |
| 强制平仓 | `request_force_close()` → 清仓全部持仓并停止开新仓 | `test_force_close_sets_zero_exposure`、`test_max_drawdown_forces_liquidation` | ✅ |
| 可扩展（不只简单示例类） | `RiskRule` 抽象 + 注册表 + 配置驱动 + `observe` 模式 | `test_register_custom_portfolio` 同类机制、`test_unknown_rule_rejected`、`test_risk_observe_mode_allows_but_records` | ✅ |
| RMS 验收：拒单率 | `RiskStats.reject_rate` | `test_stats_counting`、演示脚本输出 | ✅ |
| RMS 验收：误杀率 | `evaluate_rejections`（事后 N 日方向判定，口径已在文档声明） | `test_risk_stats.py` 6 个用例 | ✅ |
| RMS 验收：触发延迟 | `RiskTrigger.latency_days`（日频下控制类动作 = 1 个交易日） | `test_latency_summary_for_control_rules`、`test_max_drawdown_forces_liquidation` | ✅ |

---

## 9. 演示回测（合成数据，系统自检）

命令：`python examples/demo_backtest.py --strategy <name> --symbols 30`
（M3 策略 + M4 组合 + **M5 风控已接入**）

| 策略 | 累计收益 | 年化 | 最大回撤 | 成交笔数 | 累计成本(元) | 订单检查 | 拒单率 | 削减次数 | 误杀率(事后5日) |
|---|---|---|---|---|---|---|---|---|---|
| ma_cross | 2.20% | 1.09% | -10.26% | 261 | 78,107.15 | 644 | 1.55% | 80 | 53.12%（32 笔） |
| breakout | -7.49% | -3.81% | -16.31% | 349 | 39,705.31 | 762 | 0.39% | 36 | 50.00%（4 笔） |
| volume | -12.37% | -6.39% | -17.80% | 576 | 92,122.65 | 1256 | 1.04% | 87 | 42.86%（14 笔） |

> ⚠️ **该结果基于合成数据（随机行情），仅用于验证系统链路，不代表任何策略在真实市场上的表现**。
> 误杀率在合成数据上接近 50%（等价于抛硬币），符合预期——它只有在真实行情上才有解释力。
> 评价指标体系（夏普/Calmar/Sortino/容量/滑点敏感性）待 M6 交付；报告图表待 M7 交付。

---

## 10. 本轮未完成项（下一轮开工清单）

1. **M6 评价层**：年化、夏普、信息比率、最大回撤、Calmar、Sortino、换手率、成本后收益、容量估计、滑点敏感性、与沪深300/中证500/中证1000 对比。
2. **M7 报告层**：净值曲线、回撤曲线、持仓、交易明细、风险暴露、归因、RMS 验收报告。
3. **偏差与压力测试套件**：样本内/外分割、滚动窗口回测、参数敏感性、2008/2015/2020 极端行情（数据侧 `CRISIS_WINDOWS` 已就绪）。
4. 真实数据接入（实现 `BarDataLoader` 协议）与涨跌停/ST 规则在真实数据上的回归校验。
5. 第二阶段：多因子（FF/Barra）、组合优化（均值-方差 + 换手惩罚）、GARCH(1,1)、协整配对。
