# M5-2 设计：AKShare 抓取落盘工具（接口 / 数据结构 / 伪代码 / 测试计划）

> 配套代码：`tools/fetch_data.py`（主工具）、`tests/test_fetch_data.py`（24 条离线用例）、
> `tests/test_schema_contracts.py`（报告契约执法）、`docs/data/fetch_summary.schema.json`。
> 与本文件同构的先例：`docs/18_m4_dataprovider.md`（M4）。

---

## 1. 目标与边界

### 1.1 目标

把 AKShare 取到的数据**落成下游能直接读的形态**，并把「这次拿到了什么、可信到什么程度、
哪些是降级、增量了多少、慢在哪一步」写成四份可追溯的产物。

**唯一入口是 `ingest_from_provider`（M4-10）**：fetch_data 只做四件事 ——
解析股票池 / 落盘 / 出报告 / 定退出码。理由：所有取数口径（PIT 过滤、降级披露、
`skipped` 只表示"用户没请求"、严格校验）都在那儿；这里再写一遍就会变成
「两个入口口径不一致」，而这类漂移**不报错**。

### 1.2 硬约束（逐条可验收）

| # | 约束 | 验收点（用例） |
|---|---|---|
| 1 | **不重新实现取数**：内部必须调用 `ingest_from_provider` | `tools/fetch_data.py` 只 import `ingest_from_provider`，无自己的 fetch 循环 |
| 2 | 四份产物的 schema 与语义固定 | `test_summary_satisfies_declared_schema`、`test_schema_contracts::*` |
| 3 | `counts.symbols_with_bars == len(<data-root>/*.<fmt>) == counts.raw_files_written` | `test_counts_invariant_matches_files_on_disk_and_manifest` |
| 4 | 断点续抓**机械证明**（可缓存端点零调用 + 落盘逐行相等） | `test_resume_run_avoids_all_cacheable_calls_and_writes_identical_data` |
| 5 | dry-run 写入必须落在 `<out>/dry-run/` 内，越界即抛 | `test_dry_run_rejects_data_root_and_enforces_write_boundary` |
| 6 | 退出码矩阵 | `test_exit_code_matrix_matches_status_and_quality`、`test_status_and_exit_code_are_recomputable_from_summary` |
| 7 | 原子写（temp + `os.replace`），中断不留半截 | `test_interrupted_write_leaves_no_partial_file`、`test_data_cache::test_atomic_write_*` |
| 8 | `data/raw` 下不得有子目录（加载器非递归 glob） | `test_clear_data_root_refuses_subdirectories` |
| 9 | 空结果不落盘 | `test_empty_frames_are_not_persisted` |
| 10 | `--fmt csv` 必须能被 `read_table(utf-8)` 原样读回 | `test_csv_round_trip_has_no_bom_in_column_names` |
| 11 | CI 与离线测试全程不联网 | 全部用例走 `--dry-run` + `FakeAKShareClient` |
| 12 | 报告数字可追溯（命令 / 口径 / 来源三件套） | `test_markdown_and_json_agree_and_carry_three_piece` |

### 1.3 明确不做（本模块）

- **不做真实抓取**：阶段 B（联网）由使用者执行，本模块只保证"拿到数据后怎么落、怎么报"；
- **不改 `ENDPOINTS` / `MAPPERS`**：接口名与列名的候选值由 M5-5 依据探测报告单点修正；
- **不做特征/因子计算**：`data/processed/` 留给 M6/M7；
- **不做定时任务/增量调度**：只提供可被调度器调用的确定性命令。

---

## 2. 模块清单与依赖方向

```
tools/fetch_data.py                 主工具（解析 / 落盘 / 报告 / 退出码）
   ├── aqs.data.loader.ingest_from_provider   ← 取数九步（M4-10，复用）
   ├── aqs.data.cache.atomic_write_frame/text ← 原子写（M5-2 提升为公开）
   ├── aqs.data.akshare_provider              ← fetch_adjust_for / AKShareProvider
   └── aqs.data.store（只读属性：bars_frame / index_members_frame / fundamentals_frame）

docs/data/fetch_summary.schema.json   summary.json 的格式契约
tests/schema_validator.py             轻量 JSON Schema 校验器（**两个报告契约共用**）
tests/test_fetch_data.py              24 条离线用例
```

依赖方向不变：`tools → aqs.*`，工具不被库反向依赖。

**为 M5-2 对既有代码做的小改**（都只加公开入口 / 不改语义）：

| 改动 | 原因 |
|---|---|
| `akshare_provider.fetch_adjust_for(ep)`（新增公开函数） | 复权口径**单一出处**：请求参数 / 缓存 key / 缓存参数三处共用；修掉 M5-1 遗留的「缓存参数记的是请求口径」 |
| `akshare_provider._call(adjust=…)` 删除该形参 | 派生之后它就是死参数（死参数会让两侧"看起来在比对同一个量"） |
| `loader.IngestReport.step_seconds` + `to_dict()` 同名键 | 真实抓取可能几十分钟，需要看到瓶颈在**哪一步**；只加计时，不改控制流 |
| `cache.atomic_write_frame / atomic_write_text`（公开） | 缓存与 `data/raw` 落盘共用同一实现；写一半的文件会被下游当完整数据读进去且不报错 |
| `store.index_members_frame / fundamentals_frame`（只读属性） | 落盘工具需要侧表**本体**，避免去碰私有 `_members`（"调用方被迫改用内部路径"是老问题） |

---

## 3. 数据结构（四份产物）

### 3.1 `summary.json`（顶层，schema 见 `docs/data/fetch_summary.schema.json`）

| 字段 | 类型 | 语义 |
|---|---|---|
| `schema_version` | int（=1） | 格式版本 |
| `run_id` | str | `%Y%m%dT%H%M%S.%fZ`（**带微秒**：秒级在同一秒内会撞；冲突再试 `-2`/`-3`） |
| `mode` | `dry-run` / `live` | 决定退出码规则与产能边界 |
| `status` | `ok` / `degraded` / `failed` | **搬运** `IngestReport.overall_status`，不重算 |
| `exit_code` / `exit_code_reason` | int / str | 本进程退出码 + 可追溯理由 |
| `dry_run_estimated_exit_if_live` | int / null | **估计值**（由 fixture 上的质量报告与步态推出，不保证与真实 live 一致） |
| `failure` | object / null | `{type, message}`；失败时 `steps` 为空对象（不假装有步态） |
| `invocation` | object | `core/provenance.build_invocation` 原样（argv/command/cwd/python/platform/git） |
| `provider` | object | `name` / `akshare_version` / `capabilities`（12 项 + notes）/ `data_config`（**口径快照**：adjustment、failure_policy、max_missing_ratio、listing_date_policy、quality_strict、unit_conversion、cache、rate_limit、clock） |
| `request` | object | 区间、指数、`symbols{total,kept,source,max_symbols,truncated}`、adjust、fmt、refresh |
| `counts` | object | 见 3.2（含硬不变量） |
| `steps` | object | 九步步态（取值域 `ok|degraded|failed|skipped`） |
| `degradation_notes` | array | 逐条写明**被触发的既有口径**（来自 M4-10） |
| `quality` | object | `ok/errors/warnings/report` —— **摘要 + 指针**，明细在 `quality_report.json` |
| `manifest` | object | `entries_total/entries_before/by_dataset/source/digest/report` —— 摘要 + 指针 |
| `incremental` | object | 见 3.3 |
| `timing` | object | `total/ingest/materialize/resolve/reports_seconds` + `steps`（每步**独占**耗时）+ `steps_note` |
| `artifacts` | object | 四份文件名 + `data_root` + `data_format` + **`provider_for_readback`**（name/root/index_members_path/fundamentals_path） |
| `audit` | object | 审计日志路径与行数变化（口径见 3.3 的注意事项） |
| `warnings_summary` | array | 最多 20 条（全文在 `quality_report.json`） |
| `limitations` | array | 诚实声明（候选字段名未核实、文件数/装载成本、审计行数口径、增量口径…） |

### 3.2 `counts` 与它的**硬不变量**

```
symbols_with_bars == raw_files_written == len(<data-root>/*.<fmt>)      ← 硬不变量（用例锁定）
manifest_bars_keys >= symbols_with_bars                                 ← 刻意是 >=，不是 ==
```

为什么不是 `==`：provider 对「抓到**空表**的标的」也会写缓存样本（`rows=0`），
但它不会落盘（空结果不落盘）→ 缓存键数可以大于落盘文件数。
把两者写成相等就是一句**假的不变量**，而假的不变量比没有不变量更危险。

其余计数：`bars_rows/bars_start/bars_end`、`index_member_rows`、`fundamental_rows`、
`calendar_days`、`industry_rows`、`raw_bytes`、`raw_files_cleared`、`symbols_without_bars`。

### 3.3 `incremental`（断点续抓的证据）

由 run 前后两份 `data/cache/manifest.json` 快照做差得到（**不读 provider 私有统计** ——
`AKShareProvider._stats` 每次公开调用都会重置，拿它汇总整个 run 会静默得出错数字）：

```
mode: full | resume | refresh
by_dataset.<dataset>: {keys_before, keys_after, keys_refreshed, keys_untouched,
                       rows_before, rows_after, rows_delta, max_date_before, max_date_after}
requests_avoided_symbols = bars.keys_untouched      ← 本次**未写入缓存**的标的数
audit_log_lines_delta                                ← 见下方注意事项
unit / method                                        ← 口径与算法，写进报告
```

- `keys_refreshed` = 新增的 key 或 `fetched_at` 变化的 key（即"本次真的写了缓存"）；
- **注意事项（写进报告，不靠读者猜）**：`audit_log_lines_delta` 是**审计日志行数**，
  而审计日志只在**标的数 > 50** 时每 10 个标的记一条 → **为 0 不代表没抓**；
  它**不是**网络请求计数。

### 3.4 `quality_report.json` / `manifest.json` / `report.md`

- `quality_report.json` = 外层包装（`schema_version/generated_at/provider/scope`）
  + **`quality`: `ProviderQualityReport.to_dict()` 原样**。刻意不另起一套 schema：
  另写一份必然与内存里的那份漂移（表现为"报告说没问题、内存里其实有 error"）。
- `manifest.json` = 缓存清单的**投影**（权威在 `data/cache/manifest.json`，本文件只读它）：
  本次 entries（`CacheMeta.as_dict()` 原样 + 每条 `path_exists`）、`by_dataset`、
  `missing_paths`、`source_manifest_path`、**`source_manifest_digest_before/after`**。
  digest = 规范化 JSON（`sort_keys=True` + 紧凑分隔符 + UTF-8）的 sha256 前 16 hex
  → 「同一份清单换个键顺序」不会得到不同 digest，投影因此能自证"当时看到的是这一份"。
- `report.md` = 人读版：三件套 + 结论 + 请求/落盘口径 + 计数表 + 步态与每步耗时 +
  本次增量 + 降级披露 + 清单投影 + **`quality.to_markdown()` 原文** + 局限。

---

## 4. 目录与落盘约定

```
<data-root>/<symbol>.<fmt>            一标的一文件（canonical，含 close_adj）
<index-dir>/index_members.<fmt>       单文件（index_code/symbol/effective_from/effective_to）
<fund-dir>/fundamentals.<fmt>         单文件（symbol/report_period/announce_date/…）
```

- `<index-dir>` / `<fund-dir>` 由 `<data-root>.parent` 派生（`data/raw` → `data/index`、`data/fundamental`），
  与 `load_market_data` 的既有约定一致，**不引入第二套目录规则**；
- **文件内仍写 `symbol` 列**，文件名同时用 canonical symbol：不依赖"文件名推断"这一隐性契约；
- **不得建子目录**：`ParquetBarLoader` 是非递归 glob，嵌套后"文件在、加载器读不到"；
- **为什么落 canonical 而不是原始表**：`normalize_bars` 已实测**幂等**
  （`add_adjusted_prices` 只从 `close × adj_factor/首日 adj_factor` 派生，从不读已有 `close_adj`），
  读回时二次归一不会叠乘复权因子 —— 端到端用例断言 `close_adj` **逐值相等**；
- **全 A 股规模**：一标的一文件约 5,000 个（**量级估算，未联网核实**）。
  NTFS 单目录可容纳（数千不是问题），真正的成本是 `ParquetBarLoader.load_bars`
  会 glob 全部文件并逐个 read（**不做按名跳过**）→ 单次装载明显变慢。
  **根治方案：给 loader 加按符号索引文件**（后续优化，不在 M5-2）。

---

## 5. 关键伪代码

### 5.1 主流程

```
main(argv):
    args = parse(argv)                       # 用法错误 → 退出 2（argparse）
    互斥/前提检查：--incremental × --refresh、--fixture-root 需 --dry-run、
                  --data-root 不能用于 --dry-run、--max-symbols ≥ 1、--end ≥ --start
    mode = "dry-run" if args.dry_run else "live"
    run_dir, run_id = plan_run_dir(out, mode)   # os.mkdir(exist_ok=False) + FileExistsError 试 -2/-3
    summary = run_fetch(...)                    # 见 5.2
    print(口径行)                                # 模式/状态/退出码/计数/产物路径
    return summary.exit_code

run_fetch(args, mode, run_dir, ...):
    watch = Stopwatch();  started = utc_now()
    paths = dry-run ? 全部指向 <out>/dry-run/ 内 : {data_root, data_root.parent/index|fundamental, cache=config}
    if mode == dry-run: assert_within_dry_run([所有写入路径], root=<out>/dry-run)   # 硬边界
    config = load_base_config(args.config, overrides={data.{provider,root}, universe.{mode,fallback}})
    before = manifest 快照 + audit 行数          # 为 incremental 做差
    provider, client = _build_provider(...)      # live: build_provider(config.data, provider="akshare")
                                                 # dry-run: AKShareProvider(FakeClient, 隔离 cache, 固定时钟, 不限流)
    try:
        members = provider.fetch_index_members(...)      # 仅为解析候选池（ingest 会再取一次，走缓存）
        candidates = resolve_candidates(cli ∪ members → sorted → 截断)
        ingest = ingest_from_provider(provider, symbols=candidates.kept, ...)   # ★ 取数九步
        if 截断: ingest.quality.add(I1 warning)          # 截断必须进报告，不许静默
        materialized = materialize(ingest, ...)          # 见 5.3
        status, has_errors = ingest.overall_status, ingest.has_errors
    except (DataError, DataQualityError, ConfigError) as exc:
        failure = {type, message}; status, has_errors = "failed", True     # 失败**也出报告**
    after = manifest 快照 + audit 行数
    summary = {...}                                      # 见 §3.1（status 直取 overall_status）
    write_run_reports(run_dir, summary, quality, manifest_projection)      # 四份，全原子
    重写 summary（timing 的终值）                          # 只重写 summary 一份
    return FetchOutcome(summary, ingest, quality, manifest_projection, written)
```

### 5.2 dry-run 的语义（离线自检，**不能**当成"接口可用"）

```
窗口 = [2022-03-01, 2022-03-15]     # 右端刻意包含财务样例的公告日 2022-03-15，
                                    # 否则 fundamentals 会被 PIT 过滤成空表 → data/fundamental 落不下来
时钟 = 固定 2022-03-01              # 成分/行业是**快照累积**（区间从抓取日开始），
                                    # 用系统时钟会让快照落在窗口之外 → 成分恒为空
缓存 = <out>/dry-run/cache          # 跨 run 复用，才能验证断点续抓；仍在 dry-run 边界内，
                                    # 永远不碰生产 data/cache
退出码 = 恒 0                        # 证明"脚本自身跑通"；真实结论写进 dry_run_estimated_exit_if_live
```

`--data-root` 在 dry-run 下**直接拒绝**（`parser.error`）：与 `--fixture-root` 的处置一致 ——
否则用户会以为参数生效，实际却写进生产目录（footgun）。

### 5.3 落盘 `materialize`

```
def materialize(ingest, *, data_root, index_dir, fundamental_dir, fmt, refresh):
    if refresh: 对三个目录都执行 clear_data_root()      # 整体替换：删**所有文件**（跨格式），留 .gitkeep
    bars = ingest.store.bars_frame                     # canonical（含 close_adj）
    for symbol, group in bars.groupby("symbol"):       # 排序保证输出确定
        if len(group) == 0: 记 skipped_empty; continue # 空结果不落盘
        atomic_write_frame(f"{data_root}/{symbol}.{fmt}", group)
    assert_no_subdirectories(data_root)                # 加载器非递归 glob
    _write_side_table(ingest.store.index_members_frame,  index_dir,      "index_members",  ...)
    _write_side_table(ingest.store.fundamentals_frame,   fundamental_dir, "fundamentals",   ...)
```

`clear_data_root` 的两条刻意设计：**跨格式**清理（否则"先 parquet 后 csv"会留下两批数据同目录，
下游 glob 到哪批取决于后缀）；**不递归删目录**（子目录存在即报错要求人工清理 ——
静默忽略会变成"文件在、数据没了"）。

### 5.4 退出码

| 情形 | live | `--dry-run` |
|---|---|---|
| 必需步骤全 ok | 0 | 0（估计值 0） |
| `degraded`（可选数据缺失/降级/空区间） | 0；`--fail-on-degraded` → 2 | 0 |
| `has_errors`（质量报告有 error，或校验步骤 failed）或 `status=failed` | 2，**报告仍写出** | 0（估计值 2） |
| 用法错误 | 2（argparse，不产报告） | 2 |
| 脚本自身异常 | 1（冒泡，不吞） | 1 |

> 注：`has_errors` 行是对确认稿矩阵的**收紧**——「降级但质量有 error」也返回 2。
> 理由：质量报错却返回 0，等于让 CI 把"数据坏了"读成成功（本项目最忌讳的一类）。
> 若需要"只有 failed 才 2"，改动只有一行（`estimate_exit_if_live`）。

---

## 6. 决策记录与遗留问题

已确认：Q-A 四份文件 / Q-B run 目录 `mkdir(exist_ok=False)` + `-2/-3` / Q-C 一标的一文件 /
Q-D1 原子写公开 / Q-D2 不做 `provider._stats` 汇总（改用清单快照 + audit 行数）/
Q-E `--fail-on-degraded` 默认关 / Q-F dry-run 写入边界硬断言 / Q-G README 与 argparse 对齐 /
Q-H 口径派生收成 `fetch_adjust_for` 并删掉 `_call(adjust=)` / Q-I `audit_log_lines_delta` 的口径措辞。

与确认稿的**偏差**（都已实现并在报告/文档中写明，供复核）：

1. `manifest_bars_keys >= symbols_with_bars`（不是 `==`）—— 空表标的也会写缓存样本（见 3.2）；
2. 续抓证明**排除快照类端点**：成分/行业每跑必采（M4-8 的快照累积是状态，不是缓存），
   用例断言的是「可缓存端点零调用」；
3. dry-run 缓存放在 `<out>/dry-run/cache`（跨 run 复用）而非每个 run 目录下 —— 否则无法证明续抓；
4. dry-run 的默认窗口/时钟（见 5.2）—— 为了让四份产物都能落到；
5. `exit_code` 对 `has_errors` 收紧（见 5.4 注）；
6. `<index-dir>` / `<fund-dir>` 由 `--data-root.parent` 派生，未新增 CLI 参数。

遗留（下一步）：

- **M5-3**：`docs/15_akshare_runbook.md`（操作手册）+ README 复核；
- **M5-5**：真实探测 → 按报告固化 `ENDPOINTS`/`MAPPERS` → 真实抓取（联网，由使用者执行）；
- **后续优化**：给 loader 加按符号索引，解决数千文件的装载成本（见 §4）。

---

## 7. 交付顺序（每步单独提交 + 单独跑测试）

| 步 | 内容 | 状态 |
|---|---|---|
| A | M4 侧三处小改（`fetch_adjust_for` 统一 + `_call` 删 adjust + `step_seconds` + 原子写公开）+ 各自用例 | ✅ `ef84bcd` |
| B | `tools/fetch_data.py` + `tests/test_fetch_data.py` + `fetch_summary.schema.json` + 共用校验器 + 文档数字同步 | ✅ 本次 |
| C | 本文件 + README §2~§4 与 argparse 对齐 | ✅ 本次 |

---

## 8. 验收标准（Definition of Done）

- [x] 四份产物 schema 与下游契约一致，且 **schema 真的被执行**（`test_schema_contracts`）；
- [x] `counts.symbols_with_bars` 硬不变量成立；
- [x] 断点续抓有机械证明（零调用 + 逐行相等）；
- [x] dry-run 安全边界硬断言生效（越界抛错、`--data-root` 被拒）；
- [x] refresh 与 incremental 语义写清并有覆盖；
- [x] M5-1 遗留的缓存口径问题修复（`test_cache_params_use_fetch_adjust_not_requested_adjust`）；
- [x] 端到端回读成功且 `close_adj` 逐值相等；
- [x] 不破坏既有用例（全量两套运行器一致）。
