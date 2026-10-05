# 更新日志（CHANGELOG）

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的组织方式，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> 说明：`0.x` 阶段以「轮次」推进，每轮完成一组里程碑；
> 版本号只在对外可安装形态发生变化时递增。

---

## [Unreleased]

### 修复（同 API 不同参数路径产出不同内在状态：`generate_market_data`）

- **标的属性序列与代码分配解耦**（V2）：旧实现所有位置共用一个 `rng` 顺序抽样，
  抽样次数取决于**标的数量与遍历顺序** —— 同一个位置的属性（上市窗口 / 退市 / ST /
  价格路径 / 财务数值）会随「是否传 `symbols` / 传几个 / 什么顺序」漂移。
  后果不是报错，而是**做「两个入口一致性」比较时得出假结论**（M4-10 首次一致性尝试即被骗）。
  现位置 `i` 的每次抽样均来自 `default_rng([seed, i])`（`_position_rng`），
  不变量成立：第 `i` 个位置的属性只由 `(seed, i)` 决定。
  - **语义边界（刻意）**：属性属于**位置**，代码由调用方列表顺序决定；
    故「同一代码在不同顺序下拿到不同属性」是预期行为 —— 一致性比较须两侧同序或都不传 `symbols`。
  - 顺带修掉两处静默错误：**重复 `symbols` 不再产出主键重复行**（改为 `ValueError`）；
    **`symbols=[]` 不再静默退化为「按 n_symbols 自动生成」**（改为 `ValueError`，不传才是自动生成）。
  - 回归用例 8 条：`tests/test_synthetic_parameters.py`（正例 + 数量截断 + 顺序语义 + 非法入参 + 确定性）。

### 新增（第三轮 M4：DataProvider 取数层，M4-1~M4-10）

- **取数抽象** `src/aqs/data/provider.py`：`ProviderCapabilities`（12 项能力显式声明 + `missing_required`
  按需判定）、`Provenance`（溯源，时间戳统一带时区 UTC）、`ProviderHealth`、
  `DataProvider` 协议 + **多标的取数约定**、`degradation_notes`、`filter_effective_window`（区间相交）。
- **本地缓存** `cache.py`：Parquet 优先 / pyarrow 降级 CSV、参数哈希（**不含日期区间**）、
  schema 版本隔离（旧版本不读也不删）、四态命中、增量合并（断言不丢数据）、manifest、原子写入。
- **限流与重试** `ratelimit.py`：令牌桶（`clock`/`sleeper` 可注入）+ 指数退避重试
  （`retry_on_empty` 默认关闭：空结果多半是停牌/退市，重试白耗配额）。
- **数据质量** `quality.py`：Q1~Q12（含 Q3 手/股量级、Q12 幸存者偏差自检）+ 阈值可配 + 报告结构。
- **三个本地实现**：`synthetic_provider.py`（声明全部日频能力）、`file_provider.py`
  （CSV/Parquet，能力**由实际列推断**）、`akshare_provider.py`（映射骨架 + 快照累积状态，
  **不联网**；接口名与列名是候选值，M5 探测后单点修改 `ENDPOINTS`/`MAPPERS`）。
- **注册表** `registry.py`：`register_provider` / `build_provider` / `available_providers` /
  `provider_capabilities`；未知名抛 `ConfigError` **不回退**，构建期做协议闸门。
- **适配层** `loader.py::ingest_from_provider` + `IngestReport`：九步流程、`step_status`
  （`ok|degraded|failed|skipped`，`skipped` 只表示「用户没请求」）、逐条口径披露、缓存清单、
  `has_errors` / `overall_status`。
- 契约测试 `tests/contracts/provider_contract.py` + `tests/test_provider_contract.py`：
  **4 个 provider × 8 项检查 = 32 条**（能力声明↔行为一致 / `missing_required` 边界 /
  降级披露自洽 / **成分窗口区间相交** / 字段规范 / 主键唯一 / PIT 列与区间 / `Provenance` 契约）。
- 成文文档 `docs/14_provider_layer.md`：接口、能力矩阵、缓存/限流/降级策略、契约清单、与 `DataStore` 的边界。

### 修复（测试基础设施：**不报错但结果失真**的一类问题）

- **运行器收集不到继承的 `test_*` 方法**：`vars(Class)` 只含类自身 `__dict__`，
  mixin 里的契约检查会被收集 0 条而 pytest 收集 32 条 —— 收集阶段不一致**不报错**，
  只会让「看起来全绿」变成「实际没跑」。已改为 `inspect.getmembers(..., isfunction)`
  （沿 MRO 查找、`@property` 不算方法），并加正反例守护与 pytest/unittest 等价性比对。
- **「用例收集」存在三份实现且互相印证**：运行器 / `tools/sync_doc_counts.py` /
  `test_defect_11_docs_consistency.py`（AST 计数）各自统计；其中两份**各自都错**，
  于是文档数字少算 2 条而 4 条文档守护全绿。现统一为**唯一实现** `run_tests._iter_tests`，
  并加「工具计数 == 运行器计数（含逐模块）」的等价守护。
- **契约测试抓出的三处 provider 不一致**（都属「不报错但语义/记录悄悄不同」）：
  ① `FileProvider.fetch_bars` 在「请求的标的全都不存在」时抛异常，违反多标的约定
  （应为空表 + warning；与「源本身不可用」区分，后者仍报错）；
  ② `fetch_index_members` 区间过滤为空时不给 warning；
  ③ `fetch_trading_calendar` 的 `Provenance.rows` 沿用了行情行数而非交易日数（日历 `symbols` 也应为 0）。
- **UTC 时间戳贯通**：`FileProvider.fetch_bars` 的增量路径曾因 `bars_hfq` 帧没有 `symbol` 列
  而在合并时 `KeyError`（合并键硬编码）；`cache_hit` 语义原为「没发过网络请求」，
  与「降级回退时 `cache_hit=True`」冲突，已改为「最终供给数据的是缓存」。

### 计划中

- **M5** AKShare 联网适配：`tools/probe_akshare.py` 探测后固化接口名与列名 + 真实抓取 + runbook
- **M6** 评价层 `src/aqs/metrics/`（收益 / 风险 / 相对 / 交易 / 容量 / 暴露 / 归因 / 敏感性 / 分割 / 蒙特卡洛）
- **M7** 报告层 `src/aqs/report/`（内嵌 SVG，不引入 matplotlib/plotly）
- **M8** 偏差与压力测试套件（未来函数 / T+1 / 涨跌停 / 幸存者偏差 + 三段危机窗口）
- **M9** 第二阶段设计 `docs/16_phase2_design.md`（多因子 / 组合优化 / GARCH / 协整 / 因子风险）

---

## [0.1.0] - 2026-09-30

第三轮：诊断修复（D1~D4）+ 工程化。

### 修复

- **#13 订单数量未整手（四层根因，P0）**
  
  - RMS 削减量本身是小数，且 `_place_orders` 与 `broker.process_open` 两处消费点都只做 `min()` 不取整；
  - 部分成交后「剩余不足一手」被当成**硬拒单**，覆盖 `PARTIALLY_FILLED` 状态并虚增拒单率
    （实测占 ma_cross 拒单数的 80%）；
  - 减仓路径 `_round_down` 的零股分支返回小数股 → 小数股成交 → **小数持仓**并逐日传染；
  - 撮合前削减忽略 `filled_quantity` → `filled > quantity` 的自相矛盾订单 + 只顺延的「僵尸订单」。
  - 修复：新增 `aqs.core.quantity`（全项目唯一取整实现）；`RiskContext.lot_size` 由
    `EngineConfig.lot_size` 注入；`RuleRiskEngine.check_order` 统一收口归一化；
    新增 `OrderStatus.DROPPED` 与 `RejectReason.BELOW_LOT`（削减后不足一手 → 丢弃，
    **不计入拒单率与误杀率**）；`Broker._apply_reduce` 按 `remaining` 封顶（保证 INV-6）；
    `MatchingEngine` 零剩余不再可重试、并显式暴露 `residue_quantity`；
    `Account.apply_fill` 拒绝小数股成交。
  - 结果（30 只标的 / 2 年）：BUY 非整手 61/26/75 → **0**；小数股卖单 116/0/3 → **0**；
    「剩余不足一手被硬拒」46/18/47 → **0**；拒单数 57/24/68 → **1/0/0**。
  - 回归用例 28 条：`tests/test_defect_13_lot_rounding.py`。

- **#14 回测结果不可复现（P0）**
  
  - `data/synthetic.py` 迭代 `set` 后把结果交给 `rng.choice`，而 Python 对 `str` 哈希默认加盐
    （`PYTHONHASHSEED` 逐进程变化）→ 同一 seed 生成**不同的指数成分历史**。
  - 实测同一命令连续三次运行得到 313 / 537 / 463 张订单；固定哈希种子后 276 / 630（仍互不相同）。
  - 修复：`cur_list = sorted(...)`（1 行）。
  - 回归用例 4 条（**跨进程**、两个不同 `PYTHONHASHSEED`）：`tests/test_determinism.py`。

- **#15 `orders.csv` 的 `reject_reason` 语义易被误读（P2）**
  
  - `Order.defer()` 把顺延原因写入 `reject_reason` 且成交后不清理，导致
    `status=filled` + `reject_reason=suspended` 这类记录被读成「订单被拒单」。
  - 修复：`to_dict()` 拆出 `final_status` / `last_reject_reason` / `deferred_reasons` /
    `rejected_on` / `rejected_before_final`，并保留 `status` / `reject_reason` 作为过渡期别名。
  - 回归用例 8 条：`tests/test_defect_15_order_field_semantics.py`。

- **#16 报告数字不可追溯（P1，诊断 D1）**
  
  - 验收报告曾出现「表内数字来自 30 只标的的运行，落盘的却是 12 只标的的运行」。
  - 修复：新增 `aqs.core.provenance`，把命令行 / Git HEAD / Python 版本 / `PYTHONHASHSEED`
    写入 `reports/<run>/summary.json` 的 `diagnostics.invocation`；
    重新生成三份报告并回写真实数字；新增「文档数字 ← 落盘产物」一致性红灯。
  - 回归用例 11 条：`tests/test_defect_16_provenance.py`。

- **术语歧义**：报告中不再用 `exit=0` 表示进程返回码（`exit` 在本项目是 `tag` 的枚举值）。
  详见 `docs/03_acceptance_report.md` §9.1。

### 工程化

- `LICENSE`：**MIT**（与 `docs/12_framework_matrix.md` §5 的许可证策略一致）。
- `NOTICE`：AKShare 与数据源署名、第三方依赖清单、合规声明、**待联网核实项**、
  提交前密钥扫描流程。
- `.gitignore`：补齐 `data/cache/`、`reports/bench/`、`*.parquet|feather|h5|hdf5`、
  `*.log`、`.tmp_*/`、凭据类文件（`.env` / `*.pem` / `*.key` / `secrets.*`）。
- `pyproject.toml`：依赖分组 `core` / `science` / `data` / `dev` / `all`；
  补充作者、分类器、项目链接与 `license-files`；包名改为 `quant-forge`（import 名仍为 `aqs`）。
- `requirements.txt`：与 `pyproject.toml` 分组一一对应（禁止两处漂移）。
- 新增机械守护 `tests/test_packaging.py`：许可证一致性、依赖分组一致性、
  `.gitignore` 覆盖度、**主包不得依赖第三方回测框架**。
- 新增机械守护 `tests/test_no_mojibake.py`：把「文件编码不得被文本管道写坏」变成红灯
  （本项目曾三次因 PowerShell 文本往返把中文 UTF-8 文件写成乱码）。

### 文档

- 新增 `docs/17_round3_diagnostics.md`：D1/D2/D3 诊断报告 + D4 新发现 + 实施结果与前后对比。
- `docs/03_acceptance_report.md` §9 重写：补「口径定义」小节、更正记录、
  重新生成后的真实数字与修复前后对比。
- `docs/02_engine_skeleton.md` §5.1：订单字段语义表（含两个易误读样例的正确读法）。
- `docs/DEVELOPMENT.md`：新增 §8「报告与数字纪律」（数字必须带口径+来源+生成命令）；
  硬性禁令新增「不得迭代 set 后把顺序带进决策或抽样」「不得用 PowerShell 文本管道改中文文件」；
  第三轮进度看板。

### 测试

- 用例数 **398 → 783**（0.1.0 轮次新增 385 条）。按模块点算的构成：
  - 缺陷回归：#13 整手不变量 31 条、#14 跨进程确定性 4 条、#15 字段语义 8 条、#16 运行溯源 11 条；
  - 工程化与文档守护：`test_packaging.py` 20 条、`test_no_mojibake.py` 8 条、
    `test_defect_11_docs_consistency.py` 12 条（含 docs/00、CHANGELOG 数字守护与选型附录指引）；
  - 其余为第二轮缺陷回归（#1~#12）与既有模块用例。
- 测试：收集 **783** 个用例（54 个测试模块）；环境门控用例以 skip 列出，不计入通过。
  M4 阶段新增：配置 7、抽象 8、缓存 14、限流 15、质量 20、provider 实现 8（含 1 条环境门控）。

> 第三轮续接的 5 条新增用例（Q1 的 DROPPED 终态守护 3 条、Q3 上传脚本健壮性 1 条、
> M3 选型附录指引 1 条）编写于**沙箱 shell 不可用**期间，尚未在本机执行；
> 数量由静态计数（逐文件点算 `^def test_`）得出，需在宿主机跑一次复核。

---

## [0.0.1] - 2026-09-20

第二轮 M1：12 条缺陷修复。

### 修复

- **#1** `latency_days` 恒为 0 → 订单级动作 `latency=0`、控制类动作
  `acted_on=下一交易日, latency=1`（交易日由 `bind_calendar` 注入；未绑定时标 `latency_approx`）。
- **#2** `board` 列被推断值覆盖 → 优先取数据源值，仅缺失/非法行推断。
- **#3** DAY 订单未成交被误标 `REJECTED` → 改为 `EXPIRED`（不计入拒单）。
- **#4** RMS 减仓不达标且无记录 → 新增 `engine/control.py`，每日按目标敞口重算 + `pending` 欠账 + 预警。
- **#5** `PROJECT_ROOT` 硬编码 → 环境变量 → 向上发现 `pyproject.toml`/`.git` → 兜底。
- **#6** `audit_log` 未生效 → 默认跟随 `risk.yaml`，回测结束 `risk.close()` 落盘 JSONL。
- **#7** `NullRiskEngine` 统计口径与规则引擎不一致 → `RiskStats` 上移并加 `enabled`。
- **#8** 缺 `list_date` 静默兜底 → `policy=strict` 构造即报错；`proxy` 降级时四处披露。
- **#9** 量比极小基准爆炸 → 指标层 `min_base` + 策略层 `volume_min_base`。
- **#10** 测试坏味道 → 改用 `Side.BUY` 枚举、删除冗余调用、新增 AST 守护。
- **#11** 文档与代码不一致 → 规则数 12→13、延迟口径重写、新增文档漂移红灯。
- **#12** `--symbols` 口径歧义 → `--generate-symbols` / `--index-size` + 数据口径四元组输出。

### 新增

- `docs/10_round2_design.md`（第二轮设计总纲）、`docs/11_akshare_provider.md`（AKShare 设计）、
  `docs/12_framework_matrix.md`（框架选型矩阵）、`docs/13_metrics.md`（M6 评价层设计）。

---

## [0.0.0] - 2026-09-10

第一轮 M0~M5 初始交付。

### 新增

- **M0** core（枚举 / 数据模型 / 事件 / 交易日历 / 异常 / 日志）+ config（强类型 schema + YAML 加载）
- **M1** 数据层：PIT 存储与未来函数防护、双价格体系（原始价成交 / 后复权价算信号）、
  复权因子、停牌/ST/涨跌停/退市标记、历史指数成分（规避幸存者偏差）、公告日口径财务数据、
  数据质量校验、确定性合成行情生成器
- **M2** 引擎骨架：事件驱动循环（6 类事件 + 日内步骤优先级）、撮合（T+1 / 涨跌停 / 停牌 / 整手 /
  参与率上限 / 部分成交）、成本模型（佣金 / 印花税按生效日 / 过户费 / 滑点 / 冲击成本）、
  经纪商与订单生命周期、账户与持仓
- **M3** 策略层：均线交叉 / 价格突破 / 成交量配合 + 指标库 + 配置驱动注册表
- **M4** 组合层：等权 / 打分权重 / 单票上限 / 最大持仓数 / 现金缓冲 / 凯利公式 + 目标权重组合
- **M5** 风控 RMS：13 条规则 + 优先级短路 + 削减/暂停/强平 + 审计流 + VaR/ES 历史模拟
  + 拒单率 / 误杀率 / 触发延迟三项验收指标

[Unreleased]: https://github.com/shark-feng/quant-forge/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/shark-feng/quant-forge/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/shark-feng/quant-forge/compare/v0.0.0...v0.0.1
[0.0.0]: https://github.com/shark-feng/quant-forge/releases/tag/v0.0.0
