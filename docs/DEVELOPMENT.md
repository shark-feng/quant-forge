# 开发方式合同（DEVELOPMENT.md）

本文件定义本项目的协作规则，**优先级高于任何一次性生成代码的冲动**。

## 1. 交付节奏

```
需求 → 设计（接口 + 数据结构 + 伪代码 + 测试用例） → 【人工确认】 → 实现 → 单测全绿 → 交付说明 → 下一模块
```

- **一次只交付一个模块**。禁止把多个模块混在一次提交里。
- 每个模块必须先有：接口签名、数据结构、伪代码、测试用例清单（写在 `docs/` 下对应文件）。
- 未确认前不写实现代码；已确认的模块必须在**同一次交付内附单元测试**。

## 2. 验收门禁（Definition of Done）

一个模块视为完成，当且仅当：

1. 该模块的单测全部通过（`python tests/run_tests.py` 或 `pytest -q`）。
2. 未破坏既有测试（回归全绿）。
3. 影响回测结果的参数 **全部来自配置文件**，代码中无魔法数字。
4. 关键行为有日志或可审计记录（事件录制器 / RMS 审计日志）。
5. 文档同步更新（`docs/` 与代码一致性）。
6. 若涉及未来函数、T+1、涨跌停、幸存者偏差，必须有对应的显式测试用例。

## 3. 硬性禁令

| 禁令 | 原因 |
|---|---|
| 不得使用未来数据（任何 `> 决策时间` 的数据） | 最致命的回测偏差 |
| 不得用后复权价做涨跌停判定或费用计算 | 口径错误 |
| 不得用报告期代替公告日 | 财务数据未来函数 |
| 不得用当前指数成分回溯历史 | 幸存者偏差 |
| 不得在策略层直接下单/查资金 | 分层破坏，风控失效 |
| 不得在 `metrics`/`report` 中修改回测路径 | 结果不可信 |
| 不得把第三阶段执行模型接到真实交易接口 | 合规 |
| 不得迭代 `set`/`frozenset` 后把顺序带进任何决策或抽样 | Python 字符串哈希默认加盐，结果随进程变化（缺陷 #14） |
| 不得用 PowerShell 文本管道修改含中文的 UTF-8 文件 | GBK 误读回写 → 整文件乱码（已发生 3 次） |
| 不得把命令输出重定向到仓内文本文件（`.tmp_*` 草稿除外） | Windows PowerShell 5.1 的 `>` 以 **UTF-16LE** 落盘（首字节 `FF FE`），会被 UTF-8 守护判为损坏文件；要留证据请写到仓外临时目录并设 `PYTHONIOENCODING=utf-8`，或用 `cmd /c "... > 文件"` |
| 测试运行器（`tests/run_tests.py`）与 pytest 必须在**收集阶段**也保持一致 | 引入新的测试组织方式（继承、参数化、fixture、装饰器）时，必须同时验证两种运行器的**收集数**与执行结果一致。收集阶段不一致**不报错**，只会让「看起来全绿」变成「实际没跑」——实测 `vars(Class)` 不含继承方法，mixin 里的契约检查会被运行器收集 0 条、而 pytest 收集 32 条（机械守护见 `tests/test_defect_10_test_hygiene.py`） |
| 不得复制第二份「用例收集」实现 | 收集逻辑只有 `tests/run_tests.py::_iter_tests` 一份：文档同步工具与文档一致性守护都必须**调用它**。本项目曾有三份独立实现（运行器 / `tools/sync_doc_counts.py` / `test_defect_11` 的 AST 计数），其中两份**各自都错却互相印证**，于是文档数字少算了 2 条而守护全绿 |
| 不得把 `skip` 机制绑定到只被一种运行器识别的异常 | `tests/compat.py::skip` 统一抛 `unittest.SkipTest`（两套运行器都识别）。实测（V1，pytest 9.1.1）：绑成 `pytest.skip` 后，因 `Skipped` 继承自 **`BaseException`**，零依赖运行器在第一次 `skip()` 时**整体中止且不打印摘要**（退出码 1、无「通过/失败」行）——"没有 FAIL 字样"会被误读成"没有失败"。守护见 `test_defect_10_test_hygiene.py::test_skip_helper_raises_unittest_skiptest` |
| 禁止同一 API 的不同参数路径产出不同的**内在状态**（而非仅参数值不同） | 典型例子：`generate_market_data` 传/不传 `symbols` 时，标的的属性序列必须**按位置相同**（上市窗口 / 退市 / ST / 价格路径 / 财务数值）。理由：这类缺陷只在「一条路径传参、一条不传」（或数量/顺序不同）时暴露，**做一致性测试时会得出假结论** —— 本项目已实际因此误判过一次（M4-10 首次一致性比较把「股票池没对齐」读成「两个入口数据不同」）。正确做法：把每个位置的内在属性绑定到 `(seed, 位置序号)` 的**独立随机流**，而不是共用一个顺序流。守护见 `tests/test_synthetic_parameters.py`；另注意「属性属于位置、代码由调用方顺序决定」是刻意语义（见 `docs/17` §12.4） |
| 禁止测试基础设施的 `skip`/`xfail`/`fixture` 语义**依赖第三方包的存在性** | **存在性只决定「走哪条 fallback」，不决定「抛什么类型的异常」**。若第三方包未安装时抛 A、安装后抛 B，等同于让同一测试在不同环境表现不同。正确做法：**语义统一**（如一律抛 `unittest.SkipTest`），第三方特性只在被显式调用时生效。实测（V1，pytest 9.1.1）：`skip` 曾随「装没装 pytest」在 `unittest.SkipTest` 与 `pytest.skip.Exception` 之间切换，而后者继承自 `BaseException` → 零依赖运行器在装好 pytest 后**整体中止且不打印摘要**。守护见 `test_defect_10_test_hygiene.py::test_compat_skip_shows_as_skipped_under_pytest`（子进程实跑 pytest，断言显示为 `SKIPPED` 而非 `ERROR`） |

> **测试执行约定**：`pyproject.toml` 已设 `addopts = "-q"`，故执行 pytest **不要再传 `-q`**
> （`-q` + `-q` = `-qq` 会抑制 `N passed` 摘要行，表现为"有进度点、退出码 0、却没有结果行"）。
> 需要详细输出时用 `-v` 或 `-rA`。详见 `docs/17_round3_diagnostics.md` §10.3。

## 4. 代码规范

- Python 3.10+，**全量类型注解**，`from __future__ import annotations`。
- 数据容器用 `@dataclass`；对外接口用 `Protocol`/`ABC`；枚举用 `Enum`。
- 时间统一 `datetime.date`（日频决策）或 `pandas.Timestamp`；禁止裸字符串日期比较。
- 金额单位统一 **元**；数量单位统一 **股**；比率统一小数（0.00025 = 万 2.5）。
- 日志用 `logging`，禁止 `print`（测试与 CLI 除外）。
- 禁止在库代码里 `sys.exit`；异常用 `src/aqs/core/exceptions.py` 中的类型。

## 5. 分支与提交（建议）

- 一个模块一个提交，提交信息：`feat(M1): 数据层 PIT 存储与股票池过滤`。
- 提交前必须跑通测试；测试红不入库。

## 6. 当前进度看板

### 第一轮（已交付）

| 模块 | 文档 | 实现 | 测试 | 状态 |
|---|---|---|---|---|
| M0 core + config | ✅ | ✅ | ✅ | 已交付 |
| M1 数据层 | ✅ | ✅ | ✅ | 已交付 |
| M2 引擎骨架 | ✅ | ✅ | ✅ | 已交付 |
| M3 策略层 | ✅ `04_strategy_layer.md` | ✅ | ✅ | 已交付 |
| M4 组合层 | ✅ `05_portfolio_layer.md` | ✅ | ✅ | 已交付 |
| M5 风控 RMS | ✅ `06_risk_rms.md` | ✅ | ✅ | 已交付（398 用例全绿） |

### 第二轮（设计已完成 → 待确认后逐个实现）

| 里程碑 | 模块 | 文档 | 实现 | 测试 | 状态 |
|---|---|---|---|---|---|
| M1 | R2-01 缺陷修复 12 条 | ✅ `10_round2_design.md` §4 | ✅ | ✅ | **已交付**（12 条全部修复，110 条回归用例） |
| M2 | R2-02 工程化 / GitHub | ✅ §8 | ✅ 工程化文件全部就绪（README/NOTICE/CHANGELOG/LICENSE/`.gitignore`/`.gitattributes`/上传脚本） | — | **已交付**（推送由宿主机执行） |
| M3 | R2-03 框架选型矩阵 | ✅ `12_framework_matrix.md` | — | — | ⏳ 待确认（含许可证与 V1~V6 核实项） |
| M4 | R2-04 DataProvider 抽象 | ✅ `11_akshare_provider.md` + `18_m4_dataprovider.md` + `14_provider_layer.md` | ✅ M4-1~M4-10 | — | **已交付**（详见第三轮表） |
| M5 | R2-05 AKShare 适配 | ✅ `11_akshare_provider.md` | ⏳ 仅映射骨架（M4-8） | — | ⏳ 待确认（**联网探测与真实抓取由使用者执行**） |
| M6 | R2-06 M6 评价层 | ✅ `13_metrics.md` | — | — | 待确认 |
| M7 | R2-07 M7 报告层 | ⏳ `14_report.md` | — | — | 待设计 |
| M8 | R2-08 偏差与压力套件 | ⏳ `15_testing_suite.md` | — | — | 待设计 |
| M9 | R2-09 第二阶段设计 | ⏳ `16_phase2_design.md` | — | — | 待设计 |

**当前测试状态**：799 个用例（55 个测试模块）。环境门控用例以 skip 列出，不计入通过。

### 第三轮（诊断 + 修复 + 工程化）

| 里程碑 | 模块 | 文档 | 实现 | 测试 | 状态 |
|---|---|---|---|---|---|
| D1 | 报告口径澄清与更正 | ✅ `17_round3_diagnostics.md` §1 | ✅ 文档已更正 | ✅ 数字一致性红灯 | **已交付** |
| D2 | 缺陷 #13 订单整手（四层根因） | ✅ `17_round3_diagnostics.md` §2~§3、§8.2 | ✅ 全部修复 | ✅ 31 条 | **已交付** |
| D3 | `reject_reason` 字段语义 | ✅ `17_round3_diagnostics.md` §4、`02_engine_skeleton.md` §5.1 | ✅ 字段拆列 | ✅ 8 条 | **已交付** |
| D4 | 缺陷 #14 结果不可复现 | ✅ `17_round3_diagnostics.md` §5 | ✅（1 行 + 跨进程守护） | ✅ 4 条 | **已交付** |
| — | 缺陷 #16 运行溯源 | ✅ `17_round3_diagnostics.md` §8.3 | ✅ `core/provenance.py` | ✅ 11 条 | **已交付** |
| — | 开工前确认 Q1~Q3 | ✅ `17_round3_diagnostics.md` §9 | ✅ `DROPPED` 已在终态集合（无需修复）；Q3 脚本三处修正 | ✅ +5 条守护 | **已交付** |
| M2 | R2-02 工程化 / GitHub 同步 | ✅ README / NOTICE / CHANGELOG | ✅ 工程化文件全部就绪 | ✅ `test_packaging.py` 23 条 | ⚠️ **推送由宿主机执行** |
| M3 | R2-03 框架选型与边界纪律 | ✅ `12_framework_matrix.md` + `12_appendix_verification.md` | ✅ 见本文件 §7 | — | ⏳ **V1~V6 待宿主机联网执行** |
| M4 | R2-04 DataProvider 抽象 | ✅ `18_m4_dataprovider.md` + **`14_provider_layer.md`（成文）** | ✅ M4-1~M4-10 全部交付（`102fb3e`…`905dae4`） | ✅ **157 条**（含 32 条四实现契约） | ✅ **已交付** |
| M5 | R2-05 AKShare 联网适配 | ✅ `11_akshare_provider.md` | ✅ **阶段 A**：`tools/probe_akshare.py`（M5-1，离线 dry-run 可验收） | ✅ `test_probe_akshare.py` 15 条 | ⏳ **阶段 B 待宿主机执行**：联网探测 → 按报告修正 `ENDPOINTS`/`MAPPERS` → `tools/fetch_data.py` 抓取 |
| M5~M9 | 见第二轮里程碑表 | — | — | — | 待开工 |

> ⚠️ **M5 的阶段划分（不要混用结论）**：
> **阶段 A（本仓交付、完全离线）**=`tools/probe_akshare.py` 工具 + dry-run 验收 + `docs/data/probe_report.schema.json`；
> **阶段 B（必须由宿主机联网执行）**=真实探测 → 按报告在 `ENDPOINTS`/`MAPPERS` **单点修正** → `tools/fetch_data.py` 抓取。
> 阶段 A **从未发起过任何网络请求**，因此任何「AKShare 接口可用」的结论都还不成立。
>
> ```powershell
> # 阶段 A 的离线自检（CI 可跑；不装 akshare、不联网；退出码恒 0）
> python tools\probe_akshare.py --dry-run
> # 阶段 B：真实探测（产物落 docs\data\，应提交）；退出码 ≥1 个端点可用 → 0，全部失败 → 2
> python tools\probe_akshare.py --out docs\data --symbols 600000,000001 --index 000300.SH
> ```
>
> dry-run 产物固定在 `docs\data\dry-run\`（**已 gitignore**）：fixture 是手工构造的样例，
> 把它的结论当真实结论就等于「验证通过」造假。

> ⚠️ **M2 推送说明**：本机开发环境**对外网络被隔离**（沙箱设计，非故障）。
> 因此 M2 的工程化文件已全部交付并**本地提交**，但 `git push` 必须在**宿主机**执行：
>
> ```powershell
> pwsh -File tools\upload_github.ps1
> ```
>
> 脚本内置密钥扫描、测试门禁与 `git pull --rebase`（避免覆盖远端已有内容）；
> 无人值守场景加 `-NonInteractive`，已有提交只推送时加 `-NoCommit`。

> ⚠️ **测试执行说明（第三轮续接）**：本会话的 `pwsh` 调用在沙箱初始化阶段即失败
> （`SetNamedSecurityInfoW failed (Win32 5): grantWrite(D:\Quantify)`），
> 连 `git status` 都无法执行，因此**本轮新增的用例未在本机运行**。
> 已交付代码的「测试全绿」结论需由宿主机执行 `python tests\run_tests.py` 复核；
> 交付说明中已逐处标注哪些数字来自静态计数（而非实测）。

> ⚠️ **协作教训（第二轮记录，第三轮又违反两次）**：不要用 PowerShell 的 `Get-Content`/`Set-Content` 管道
> 对含中文的 UTF-8 文件做批量替换 —— 会以 GBK 解码再回写，造成整文件乱码。
> 文件修改统一使用编辑器/`edit` 工具。
> **第三轮补充**：即使 `pwsh 7`（`Get-Content` 默认 UTF-8）也会因 `-replace` + `Set-Content` 组合
> 破坏中文与换行，实测三次均导致文件不可用。纪律改为**绝对禁止**，并新增机械化红灯
> `tests/test_no_mojibake.py`（9 条）——本次它已成功拦住过我自己的错误写法。
>
> **第四种变体（同一类问题，2026-09-30 实测）**：**含中文的 `.ps1` 必须带 UTF-8 BOM**。
> BOM 缺失时，PowerShell 5.1 与 `Parser::ParseFile` 会按系统 ANSI（GBK）读取脚本：
> 中文变乱码、字符串终止符被破坏，进而报出一串**假语法错误**
> （实测 `tools/upload_github.ps1` 被报 12 处错误，正确解码后实际 0 错误）。
> 处置：该文件已加 BOM，并由 `tests/test_packaging.py` 断言守护。
>
> **第五种变体（2026-10-02 实测，M4-7）**：**不要用 `>` 把命令输出重定向到仓内文本文件**。
> Windows PowerShell 5.1 的 `>` 默认以 **UTF-16LE** 落盘（首字节 `FF FE`），
> `tests/test_no_mojibake.py` 会据此报「不是合法 UTF-8」。这不是产品缺陷而是**工具链陷阱**：
> 留证据请写到仓外临时目录（`$env:TEMP`），或用 `cmd /c "... > 文件"` 并设 `PYTHONIOENCODING=utf-8`。
> 守护本身同步收紧：`.tmp_*` 草稿被排除在扫描外（正反例见该模块的
> `test_scratch_skip_rule_excludes_only_drafts`），交付物仍全量扫描。
>
> **读文件验证时的陷阱**：用 `Parser::ParseFile` / `Get-Content`（默认编码）检查中文文件会**误报**。
> 正确做法是显式指定编码：
> `[System.IO.File]::ReadAllText($p, [System.Text.Encoding]::UTF8)`，
> 或直接用本仓库的文件读取工具。

## 7. 框架边界纪律（第二轮新增，来源 `docs/12_framework_matrix.md`）

1. 主引擎只有一个：自研 `aqs.engine`；**禁止**把第三方回测框架写进主包 `dependencies`；
2. 框架脚本只能读我们导出的 CSV，不得 import `aqs` 内部状态；
3. 框架输出固定写入 `reports/bench/<framework>/`（已 gitignore），**不得**出现在正式回测报告中；
4. 交叉验证结论必须能回到自研引擎复现，才允许写入文档与报告；
5. 禁止复制 GPL 代码进 `src/aqs`。

## 8. 报告与数字纪律（第三轮新增）

1. **每个数字必须三件套**：口径定义 + 来源文件路径 + 生成命令。缺一不得写入文档或报告。
2. **禁止术语混用**：进程返回码写「进程返回码 0」，不得写 `exit=0`
   （`exit` 在本项目是 `tag` 的枚举值）。
3. **报告层（M7）** 输出的每个指标必须携带 `definition` 字段（公式 + 口径 + 是否年化），
   不允许出现语义模糊的指标名。
4. **可复现性**：任何用于报告的回测必须在**固定命令 + 固定 seed** 下可复现；
   可复现性测试必须**跨进程**（同进程内哈希顺序不变，测不出缺陷 #14）。
5. **不得引用未经落盘验证的数字**；文档与落盘 `summary.json` 的一致性由测试看守。
