# 附录：框架选型待核实项的可执行清单（V1~V6）

> 本文是 [`docs/12_framework_matrix.md`](12_framework_matrix.md) §7「实现前验证清单」的展开版。
>
> **状态：全部未执行。** 本清单由**沙箱内 agent 编写**，而沙箱对外网络被隔离
> （`curl` 任意外网域名失败、`git clone`/`gh` 不可用），因此
> **agent 不执行任何拉取**，只把步骤、判据与回填位置固定下来，交由宿主机联网执行。
>
> 执行原则：**先核实、后回填**。未核实前，`docs/12` 中相关评分一律保持「待核实」，
> 且**不允许**据此把任何第三方回测框架引入主包依赖。

---

## 0. 执行方式

| 项目 | 说明 |
| --- | --- |
| 执行环境 | 宿主机（可访问 github.com / pypi.org / raw.githubusercontent.com） |
| 前置工具 | `git`（必需）、`gh`（推荐，可少写 HTTPS 细节）、`curl`（备选）、`jq`（可选，便于取字段） |
| 隔离要求 | V3/V4/V5 涉及安装第三方框架 →**必须使用独立 venv**，禁止装进本项目环境 |
| 产出位置 | 结果写入本文件 §7 的「结果记录表」；涉及 `docs/12` 的改动按各 V 项的「回填位置」执行 |
| 提交要求 | 每次回填单独提交，提交信息写清「依据 V\<n\> 的哪条原始输出」 |

推荐的独立 venv 做法（V3~V5 通用）：

```powershell
python -m venv .venv-bench          # 已被 .gitignore 忽略
.\.venv-bench\Scripts\Activate.ps1
python -m pip install -U pip
# 再安装被核实的框架；**不要**在项目根环境执行
```

> ⚠️ 本项目主环境的 `pyproject.toml` 依赖分组**禁止**加入任何第三方回测框架，
> 该约束由 `tests/test_packaging.py::test_no_third_party_backtest_framework_in_dependencies` 机械守护。

### 待核实的候选仓库（URL 为**候选**，首个动作是先确认仓库存在）

| 框架 | 候选仓库 URL | 候选 PyPI 包名 |
| --- | --- | --- |
| AKQuant | `https://github.com/akfamily/akquant`（候选，需确认命名空间） | `akquant`（候选） |
| PyBroker | `https://github.com/edtechre/pybroker` | `lib-pybroker` |
| Backtrader（原版） | `https://github.com/mementum/backtrader` | `backtrader` |
| Backtrader（社区 fork） | `https://github.com/backtrader2/backtrader`（候选） | — |
| AKShare | `https://github.com/akfamily/akshare` | `akshare` |

> URL 一旦与事实不符，**不要猜测**：把实际 URL 记入 §7 结果表，并同步修订本表。

---

## V1 三框架的**许可证原文**

**目的**：把 `docs/12` §5 的「（待核实）」替换为 SPDX 标识与条款要点，
确保「主包 MIT + 无 GPL 传染」的声明为真。

**预期文件路径**（按优先级尝试）：
`LICENSE`、`LICENSE.txt`、`LICENSE.md`、`COPYING`、`COPYING.txt`、`NOTICE`、`pyproject.toml`（`license` 字段）、`setup.py`（`license=`）、`package.json`

**核实命令**（任选，推荐 GitHub API —— 直接给 SPDX）：

```bash
# 1) GitHub API：拿 SPDX 标识
curl -sS https://api.github.com/repos/edtechre/pybroker/license | jq -r '.license.spdx_id, .license.name'

# 2) 拿许可证全文（含文件哈希，用于 §7 记录）
curl -sSL https://raw.githubusercontent.com/edtechre/pybroker/HEAD/LICENSE -o pybroker-LICENSE
git hash-object pybroker-LICENSE        # 记录 SHA-1，作为「核实的是哪一版」的证据

# 3) gh 等价写法
gh api repos/edtechre/pybroker/license --jq '.license.spdx_id'

# 4) PyPI 元数据（交叉验证，注意 PyPI 的 license 字段常不规范）
curl -sS https://pypi.org/pypi/lib-pybroker/json | jq -r '.info.version, .info.license'
```

对以下仓库各执行一遍：

| 目标 | 仓库 | 我们**假设**的许可证 | 必须确认的关键点 |
| --- | --- | --- | --- |
| AKQuant | 候选见 §0 | 未知 | 是否为标准 OSI 许可证；若是自定义条款，是否含传染/商用限制 |
| PyBroker | `edtechre/pybroker` | Apache-2.0 | 是否为 Apache-2.0 原文（而非「Apache 风格」） |
| Backtrader | `mementum/backtrader` + fork | GPL 系 | **GPL-2.0 还是 GPL-3.0**；fork 是否换证 |
| AKShare | `akfamily/akshare` | MIT | 是否为 MIT 原文 |

**回填位置**：

| 回填到 | 具体位置 | 改成什么 |
| --- | --- | --- |
| `docs/12_framework_matrix.md` | §5 表「许可证（待核实）」列 | 实际 SPDX 标识 + 文件名 + 文件哈希前 8 位 |
| `docs/12_framework_matrix.md` | §5 表「处置」列 | 若与假设不符，按下方降级处置改写 |
| `docs/12_framework_matrix.md` | §3 选型矩阵「许可证友好度」行 | 按实际 SPDX 调整评分（MIT/Apache=5、弱 copyleft=3、GPL=1） |
| `docs/12_framework_matrix.md` | §3 矩阵标题行「（自有，MIT 计划）」 | 改为「（自有，MIT，已发布 `LICENSE`）」 |
| `NOTICE` | §2 第三方清单「许可证」列 | 各依赖的实际 SPDX |
| `NOTICE` | §4 待核实项 N1~N4 | 标记为已核实 + 日期 |
| `CHANGELOG.md` | `[Unreleased]` | 记录「M3 许可证核实完成」 |

**与假设不符时的降级处置**：

| 场景 | 处置 |
| --- | --- |
| Backtrader 确认 GPL-**3**.0 | 维持「独立 venv + 独立进程 + 不链接 + 不随主包分发」；`NOTICE` §3.3 的「未包含任何 GPL 代码」必须保持为真（只读 CSV，不引入代码） |
| Backtrader 实为宽松许可证（与社区认知不符） | 仍**不提升**为主引擎（架构不匹配 + 迁移成本 ≈300 用例）；最多从「仅交叉验证」放宽为「可选 extra」，且须重新过一遍 `pyproject` 边界测试 |
| PyBroker 实为 copyleft 系 | 从「可选 extra」降级为「独立 venv 只读研究」，并在 `docs/12` §4 表格注明 |
| AKQuant 为自定义/不明条款 | 维持「只借鉴设计、零代码引入」，并在 `docs/12` §4 的「借鉴」行补一句「未引入其任何代码或数据文件」 |
| **任何**框架为 AGPL | 直接列为**禁用**（连隔离使用都不做），在 §8 反面清单新增一条 |
| AKShare 实为非 MIT | 依赖使用本身不受影响（未分发其代码），但 `NOTICE` 必须写实际许可证与署名要求；若限制商用，则在 `NOTICE` §3 增加用途限制说明 |

---

## V2 最新版本号与最近提交时间

**目的**：校准 `docs/12` §3「维护活跃度」评分（当前 AKQuant=4、PyBroker=4、Backtrader=2 均为估计值）。

**预期文件路径**：无（走 API）；可参考 `CHANGELOG.md`、GitHub Releases 页。

**核实命令**：

```bash
# 仓库活跃度
curl -sS https://api.github.com/repos/mementum/backtrader \
  | jq '{pushed_at, updated_at, archived, stargazers_count, open_issues_count, default_branch}'

# 最新版本（PyPI）
for pkg in lib-pybroker backtrader akshare; do
  echo -n "$pkg: "
  curl -sS "https://pypi.org/pypi/$pkg/json" | jq -r '.info.version, .info.requires_python'
done

# gh 等价
gh repo view mementum/backtrader --json pushedAt,isArchived,stargazerCount
```

**回填位置**：

| 回填到 | 具体位置 | 改成什么 |
| --- | --- | --- |
| `docs/12` | §2「框架简介」表中各框架的版本/状态描述 | 实际最新版本 + `pushed_at` 日期 |
| `docs/12` | §3 矩阵「维护活跃度」行 | 按判据重评（见下） |
| `docs/12` | §7 表 V2 行「产出」列 | 标记完成 |

**活跃度判据（写死，避免主观）**：

| 条件 | 维护活跃度评分 |
| --- | --- |
| `archived=true`，或 `pushed_at` 距今 > 24 个月 | **2**（并注明「已停滞/归档」） |
| `pushed_at` 距今 12~24 个月 | 3 |
| `pushed_at` 距今 6~12 个月 | 4 |
| `pushed_at` 距今 < 6 个月且未归档 | 5 |

**与假设不符时的降级处置**：

- Backtrader 原版若已归档 → 评分维持 2，并把「社区 fork 是否可用」单独记一行；
  交叉验证脚本**优先指向 fork**，但脚本必须能在两个实现上跑（否则记为不可复现）。
- 若某框架 `pushed_at` 很新但 `open_issues_count` 极高（> 500）→ 评分不升，注明「活跃但积压严重」。

---

## V3 AKQuant 的 T+1 / 涨跌停语义

**目的**：产出 **≥3 条可借鉴点**（这是 `docs/12` §4 表格「借鉴（AKQuant）」一行的验收标准）。

**预期文件路径**（clone 后阅读）：

```
docs/                      # 概念文档
akquant/ 或 src/akquant/   # 撮合/风控内核
tests/                     # 关键：从测试反推语义（比文档更可信）
```

**核实命令**：

```bash
git clone --depth 1 https://github.com/akfamily/akquant.git akquant-src
cd akquant-src
git rev-parse HEAD                       # 记录核实的 commit
rg -n "T\+1|t_plus_1|t1|涨跌停|limit_up|limit_down|停牌|suspend" --glob '!*.lock'
```

**要回答的 5 个问题**（逐条写结论，不要只写「看过」）：

1. T+1 是在**下单时**校验还是在**撮合时**校验？（对应我们的 `Order.submit_date > signal_date` 断言）
2. 涨跌停判定用的是**原始价**还是**复权价**？（对应我们的双价格体系）
3. 涨跌停价是数据自带、还是按板块比例推算？ST 是否另有比例？
4. 停牌/一字板是否被建模为「可顺延」？顺延上限如何配置？
5. 是否有**显式的未来函数防护**（PIT 视图 / as_of 参数）？

**回填位置**：

| 回填到 | 具体位置 | 改成什么 |
| --- | --- | --- |
| `docs/12` | §4 表格「借鉴（AKQuant）」行的「输出」列 | `docs/12` §9 借鉴点清单（新建小节） |
| `docs/12` | §3 矩阵「T+1 / 涨跌停 / 停牌支持」「A 股适配度」两行的 AKQuant 列 | 把「4（待核实）」改为实测结论 |
| `docs/12` | §9（新建） | ≥3 条借鉴点，每条格式：`现象 → 我们的差距 → 是否采纳 → 采纳后的测试` |

**与假设不符时的降级处置**：

| 场景 | 处置 |
| --- | --- |
| 其 T+1 语义与我们的验收点冲突（如允许当日回转） | 记为「**不借鉴**」并写明冲突点；保留自研实现（我们的口径有回归用例兜底） |
| 最小示例在独立 venv 中跑不通（依赖冲突/Rust 编译失败） | 降级为「**仅文档级借鉴**」，在 §4 表格注明「未跑通示例」，且**不得**写入任何性能对比数字 |
| 许可证为自定义条款（见 V1） | 借鉴点只允许停留在「设计思路」层面，**禁止**复制任何代码片段（`docs/12` §8 第 3 条） |

---

## V4 PyBroker 的 AKShare 集成方式

**目的**：产出 ML 研究流程草案，验证「AKShare 数据 → 特征 → walk-forward」这条链路是否可复用我们的 `DataProvider` 导出。

**预期文件路径**：

```
pybroker/ 或 src/pybroker/    # 数据适配层（找 akshare / data source 相关模块）
examples/                     # 官方示例（最省时的入口）
docs/                         # walk-forward / 训练配置说明
```

**核实命令**：

```bash
git clone --depth 1 https://github.com/edtechre/pybroker.git pybroker-src
rg -n "akshare|Alpaca|DataSource|walk_forward|train" pybroker-src/examples pybroker-src/src 2>/dev/null | head -50
```

**要回答的 3 个问题**：

1. 它接入数据源的方式是「内置 AKShare 适配器」还是「用户提供 DataFrame」？
   —— 若是后者，我们**不需要**让 PyBroker 联网，直接喂导出的 CSV 即可（更符合边界纪律）。
2. 它要求的数据 schema（列名/索引/频率）与我们的 `bars` 表差异多大？需要几行转换代码？
3. walk-forward 的切分与指标口径是否与 `docs/13_metrics.md` 的样本内外分割一致？

**回填位置**：

| 回填到 | 具体位置 | 改成什么 |
| --- | --- | --- |
| `docs/12` | §4 表格「ML 研究（PyBroker）」行 | 输入改为实际可行形式（导出 CSV vs 直连 AKShare） |
| `docs/12` | §3 矩阵「ML 集成」行 | 维持或调整 PyBroker 的 5 分，并注明依据 |
| `docs/13_metrics.md` | 样本内外分割小节 | 若其口径合理，注明「与 PyBroker 口径一致性」作为参考 |

**与假设不符时的降级处置**：

| 场景 | 处置 |
| --- | --- |
| PyBroker 内置 AKShare 适配器与我们的字段规格冲突 | **不迁就它**：改用「导出 CSV → PyBroker DataFrame」的单向路径，保持 `DataProvider` 为唯一入口 |
| 其依赖与本项目 extras 冲突（如要求特定 pandas 版本） | 维持独立 venv；在 `docs/12` §4 注明「版本不可共存，仅单向导出」 |
| walk-forward 口径与 `docs/13` 不一致 | 以 `docs/13` 为准（它是我们报告的口径来源），把差异记为参考而非标准 |

---

## V5 Backtrader 最小可跑示例与净值相关系数

**目的**：产出交叉验证脚本与**净值相关系数**（`docs/12` §4 表格的验收线：> 0.98）。

**预期输入**：我们导出的 `bars.csv`（字段见 `docs/01_data_layer.md`）与成交清单 `trades.csv`。

**核实命令**（独立 venv 内）：

```powershell
python -m venv .venv-bench
.\.venv-bench\Scripts\Activate.ps1
pip install backtrader pandas
python tools\bench_backtrader.py --bars reports\_bench_in\bars.csv --out reports\bench\backtrader
```

**验收与归因**：

| 相关系数 | 判定 | 动作 |
| --- | --- | --- |
| > 0.98 | 通过 | 把脚本与结果写入 `reports/bench/backtrader/`，在 `docs/12` §4 记为已验收 |
| 0.90 ~ 0.98 | 有条件通过 | **必须逐项归因**（成本模型 / 整手取整 / 顺延规则 / 复权口径 / 成交价口径），差异可解释方可写入文档 |
| < 0.90 | **不通过** | 视为交叉验证失败；**不得**用其结论修正自研引擎，只记录「两引擎不可比」及其原因 |

**回填位置**：

| 回填到 | 具体位置 | 改成什么 |
| --- | --- | --- |
| `docs/12` | §4 表格「交叉验证（Backtrader）」行的「验收」列 | 实际相关系数 + 归因结论 |
| `docs/12` | §7 表 V5 行「产出」列 | 脚本路径 + 结果文件路径 |
| `CHANGELOG.md` | `[Unreleased]` | 记录交叉验证结论 |

**与假设不符时的降级处置**：

| 场景 | 处置 |
| --- | --- |
| Backtrader 装不上（Python 3.13 兼容性） | 用独立 venv 固定到 Python 3.11/3.12；若仍失败 → 记为「无法交叉验证」，在 `docs/12` §4 明确写「未验证」，**不留空白** |
| 其撮合默认行为（如允许 T+0、不含涨跌停）导致曲线必然不同 | 先把差异项在脚本里对齐（同成本、同整手、同成交价），仍不可对齐的部分列为「引擎语义差异」并计入归因表 |
| 结果为「不通过」 | 按上表处置；**禁止**为了让相关系数好看而修改自研引擎的口径（`docs/12` §8 第 4 条） |

---

## V6 依赖体量与许可传染性

**目的**：产出 `NOTICE` §2 的第三方清单终稿，并确认主包 extras 不含 copyleft 传染项。

**核实命令**（独立 venv 内，避免污染主环境）：

```powershell
python -m venv .venv-lic
.\.venv-lic\Scripts\Activate.ps1
pip install -e ".[all]"
pip install pip-licenses
pip-licenses --format=markdown --with-urls --with-license-file --output-file licenses.md
pip-licenses --format=csv --with-urls | Measure-Object -Line   # 依赖体量（行数）
```

**回填位置**：

| 回填到 | 具体位置 | 改成什么 |
| --- | --- | --- |
| `NOTICE` | §2 各分组表「许可证」列 | 实际 SPDX（逐个替换「（待核实）」表述） |
| `NOTICE` | §4 待核实项 N5 | 标记完成，附 `licenses.md` 的生成命令与日期 |
| `NOTICE` | §2 分组结构 | 若发现某依赖实际只被第二阶段需要，把它从 `core` 移到对应 extras |

**与假设不符时的降级处置**：

| 场景 | 处置 |
| --- | --- |
| 某传递依赖为 GPL/AGPL | **从 extras 中移除该可选依赖**，在 `NOTICE` §3 记录原因；若它是 `akshare` 的必需传递依赖，则把 AKShare 适配层标为「可选且需自行评估许可」 |
| 依赖体量过大（如 `all` 超过 200 个包） | 拆分 extras（例如把 `akshare` 与 `pyarrow` 分开），并把 `all` 改为「按需组合」说明而非一次性安装 |
| PyPI 的 license 字段与实际 LICENSE 文件不一致 | **以仓库 LICENSE 原文为准**，并在 `NOTICE` 注明「PyPI 元数据与仓库不一致，已按仓库原文记录」 |

---

## 7. 结果记录表（执行后填写）

> 每完成一项就填一行，并把原始输出粘贴到「原始输出摘要」列（或另存 `reports/_probe/verify_V<n>.txt` 并在本表引用）。
> **未填写的行一律视为「未核实」**，不得据此修改选型结论。

| # | 执行日期 | 执行人 | 关键命令 | 原始输出摘要 | 结论 | 已回填位置 | 依据 commit |
| --- | --- | --- | --- | --- | --- | --- | --- |
| V1 | | | | | | | |
| V2 | | | | | | | |
| V3 | | | | | | | |
| V4 | | | | | | | |
| V5 | | | | | | | |
| V6 | | | | | | | |

### 7.1 许可证核实结果（V1 明细）

| 框架 | 实际 SPDX | LICENSE 文件路径 | 文件 SHA-1（前 8 位） | 与假设一致？ | 处置是否变化 |
| --- | --- | --- | --- | --- | --- |
| AKQuant | | | | | |
| PyBroker | | | | | |
| Backtrader | | | | | |
| Backtrader fork | | | | | |
| AKShare | | | | | |

### 7.2 版本与活跃度（V2 明细）

| 框架 | 最新版本 | requires-python | pushed_at | archived | stars | 维护活跃度评分 |
| --- | --- | --- | --- | --- | --- | --- |
| AKQuant | | | | | | |
| PyBroker | | | | | | |
| Backtrader | | | | | | |
| AKShare | | | | | | |

### 7.3 交叉验证结果（V5 明细）

| 策略 | 交易日数 | 自研终值 | Backtrader 终值 | 净值相关系数 | 归因项 | 判定 |
| --- | --- | --- | --- | --- | --- | --- |
| ma_cross | | | | | | |
| breakout | | | | | | |
| volume | | | | | | |

---

## 8. 判定规则：什么情况下必须改选型结论

以下规则**先写死**，避免执行完 V1~V6 后凭印象改结论：

1. **主引擎不换**（除非同时满足下列全部）：
   (a) 某框架许可证为 MIT/Apache；
   (b) 它能对我们的全部验收点（事件顺序、T+1、涨跌停、PIT 越界、整手、成本明细）提供**可断言接口**；
   (c) 迁移后回归用例数量不减少；
   (d) 性能提升 ≥ 5×（否则优先做向量化/Rust 插件）。
   当前判断：**(b) 与 (c) 几乎不可能满足**，故结论维持「保持自研」。
2. **禁止三套框架同时当主引擎**（合同禁令，与核实结果无关）。
3. **GPL 系框架**：任何情况下不得进入主包 `dependencies`、不得复制代码、不得随主包分发。
4. **许可证不明确**：视同最严格情形处理（只借鉴设计、零代码引入）。
5. 若 V1~V6 全部完成且结论与 `docs/12` 现有内容一致 → 在 `docs/12` §7 末尾追加
   「V1~V6 已于 YYYY-MM-DD 完成核实，结论未变」，并保留本表作为证据链。

---

## 9. 本清单自身的维护

- 本文件随 V1~V6 的执行而更新；**执行人不确定时不要猜**，把疑问写进 §7 结果表的「结论」列并标 `TODO`。
- 若发现新框架候选，按同样结构新增 V7…；**不得**因为「看起来更好」就绕过 §8 的判定规则。
- 本文件与 `docs/12` 的一致性由人工核对（数字与许可证标识难以机械提取）；
  但 `docs/12` 的「单主引擎」声明已由
  `tests/test_defect_11_docs_consistency.py::test_framework_matrix_states_single_main_engine` 机械守护。
