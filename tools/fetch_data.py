"""AKShare 抓取并落盘（第三轮 M5-2）。**CI 与离线测试全程不联网**。

职责只有四件事：**解析股票池 → 落盘 → 出报告 → 定退出码**。
取数本身**完全复用** `aqs.data.loader.ingest_from_provider`（M4-10 的九步流程：
日历 → 指数成分 → 元信息 → 行情 → 财务 → 行业 → 归一 → 校验 → 建 store）。
理由：所有取数口径（PIT 过滤、降级披露、`skipped` 只表示用户没请求、严格校验）都在那里；
本工具再写一遍就会变成「两个入口口径不一致」，而这类漂移**不报错**。

四份产物（`--out` 下的 `<run_id>/`）
-----------------------------------
1. ``summary.json``：本次运行的完整结论（口径 / 计数 / 步态 / 每步耗时 / 增量 / 指针）；
2. ``quality_report.json``：Q1~Q12 + 取数层降级披露，**内嵌** `ProviderQualityReport.to_dict()` 原样；
3. ``manifest.json``：缓存清单的**投影**（权威清单在 ``data/cache/manifest.json``，本文件只读它）；
4. ``report.md``：人读版（嵌 `quality.to_markdown()` 原文 + 三件套 + 计数 + 增量）。

落盘形态（下游 `ParquetProvider`/`CsvProvider` 直接读，见 `artifacts.provider_for_readback`）
-----------------------------------------------------------------------------------------
```
<data-root>/<symbol>.<fmt>        一标的一文件（canonical，含 close_adj）
<index-dir>/index_members.<fmt>   单文件（index_code/symbol/effective_from/effective_to）
<fund-dir>/fundamentals.<fmt>     单文件（symbol/report_period/announce_date/…）
```
- **不得建子目录**：`ParquetBarLoader` 用**非递归** glob `*.parquet`，嵌套后文件在、加载器读不到；
- **空结果不落盘**：空文件会被下游当成「该标的存在但无数据」；
- **原子写**（temp + `os.replace`）：写一半被中断的数据文件会被当完整数据读进去且不报错；
- **为什么落 canonical 而不是原始表**：`normalize_bars` 已实测**幂等**
  （`normalize_bars(normalize_bars(x)).equals(normalize_bars(x))` 为真，
  因为 `add_adjusted_prices` 只从 `close × adj_factor/首日 adj_factor` 派生、从不读已有 `close_adj`），
  所以读回时二次归一不会叠乘复权因子；端到端回读用例断言 `close_adj` **逐值相等**。

`refresh` 与 `incremental` 的语义（**不可混用**）
----------------------------------------------
- ``--refresh``：**整体替换**。先清空 `<data-root>`/`<index-dir>`/`<fund-dir>` 下的**所有文件**
  （跨格式清理：否则「先 parquet 后 csv」会留下两批数据同目录，下游 glob 到哪批取决于后缀），
  再写本次结果；`.gitkeep` 保留，子目录存在则直接报错（要求人工清理）。
- 默认（``--incremental`` 是它的显式同义写法）：**增量续抓**，缓存优先；本次结果**添加/更新**，
  不动未涉及的标的。

`--dry-run`（离线自检）
----------------------
用 `FakeAKShareClient` + `tests/fixtures/akshare/`。**安全边界**：本次所有写入路径
（run 目录 / data-root / index / fundamental / 缓存）解析后必须落在 ``<out>/dry-run/`` 下，否则抛异常；
``--data-root`` 在 dry-run 下**被直接拒绝**（parser.error）—— 否则会让人以为参数生效，
实际却在真实抓取或污染生产数据。dry-run 退出码恒 0（证明脚本自身跑通），
它**不能**证明数据源可用：真实结论见 ``dry_run_estimated_exit_if_live``（估计值）。

用法::

    # 离线自检（不联网、不装 akshare）
    python tools\\fetch_data.py --dry-run --index 000300.SH

    # 真实抓取（联网环境；先 pip install -e ".[data]"）
    python tools\\fetch_data.py --start 2020-01-01 --end 2025-12-31 --index 000300.SH
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(_ROOT / "src"), str(_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from aqs.config.loader import load_base_config  # noqa: E402
from aqs.config.schema import DataConfig, UniverseConfig  # noqa: E402
from aqs.core.exceptions import ConfigError, DataError, DataQualityError  # noqa: E402
from aqs.core.provenance import build_invocation  # noqa: E402
from aqs.data.cache import atomic_write_frame, atomic_write_text  # noqa: E402
from aqs.data.loader import (  # noqa: E402
    INGEST_FINDING_CODE,
    INGEST_STEPS,
    IngestReport,
    ingest_from_provider,
)
from aqs.data.provider import utc_now  # noqa: E402
from aqs.data.quality import ProviderQualityReport, QualityFinding  # noqa: E402
from aqs.data.registry import build_provider  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量（口径集中在这里；代码里不散落魔法数字）
# --------------------------------------------------------------------------- #
SCHEMA_VERSION = 1
DEFAULT_CONFIG = "configs/base.yaml"
DEFAULT_OUT = "reports/fetch"
DEFAULT_DATA_ROOT = "data/raw"
DEFAULT_INDEX_DIR = "data/index"
DEFAULT_FUNDAMENTAL_DIR = "data/fundamental"
DEFAULT_FMT = "parquet"
SUPPORTED_FMT: tuple[str, ...] = ("parquet", "csv")

DRY_RUN_ROOT_NAME = "dry-run"
SUMMARY_NAME = "summary.json"
QUALITY_NAME = "quality_report.json"
MANIFEST_NAME = "manifest.json"
REPORT_NAME = "report.md"
REPORT_FILES: tuple[str, ...] = (SUMMARY_NAME, QUALITY_NAME, MANIFEST_NAME, REPORT_NAME)

#: dry-run 的默认窗口：`tests/fixtures/akshare/` 覆盖的区间，
#: 且**右端 03-15 刻意包含财务样例的公告日**（否则 fundamentals 会因 PIT 过滤为空，
#: 四份产物里的 `data/fundamental/fundamentals.*` 就落不下来）
DRY_RUN_START = "2022-03-01"
DRY_RUN_END = "2022-03-15"
DRY_RUN_INDEX_CODE = "000300.SH"
#: dry-run 的固定时钟：指数成分/行业是**快照累积**（区间从抓取日开始），
#: 用系统时钟会让快照落在 fixture 窗口之外 → 成分恒为空。固定时钟让 dry-run 可复现，
#: 也让四份产物都能落到（真实运行一律用系统 UTC 时钟）。
DRY_RUN_NOW = "2022-03-01T09:30:00+00:00"

#: 规范化 JSON 的序列化参数（digest 必须与「同一份内容的书写方式」无关）
_COMPACT_SEPARATORS = (",", ":")
DIGEST_HEX = 16
MANIFEST_ABSENT = "absent"

#: 运行目录名：秒级会撞（同一秒内两次运行），故带微秒
RUN_ID_FORMAT = "%Y%m%dT%H%M%S.%fZ"

#: `--max-symbols` 截断时写进质量报告的告警（与取数层降级披露共用 I1 编码）
TRUNCATION_FINDING = INGEST_FINDING_CODE

DATA_FORMAT = "file-per-symbol"
#: 落盘格式 → 回读时该用哪个 provider（`parquet`/`csv` 与 `ParquetProvider`/`CsvProvider` 对应）
_READBACK_PROVIDER = {"parquet": "parquet", "csv": "csv"}

EXIT_OK = 0
EXIT_FAILED = 2

LIMITATIONS: tuple[str, ...] = (
    "落盘的是**归一后的 canonical 表**（含 close_adj）；normalize_bars 幂等，读回再归一不会叠乘复权因子",
    "AKShare 的接口名与列名在 M5-5 固化前仍是**候选值**（见 docs/data/akshare_capability_report.*）",
    "一标的一文件在**全 A 股**（约 5,000 只量级，未联网核实）时会让 `data/raw/` 下出现数千个文件："
    "NTFS 单目录可容纳（数千不是问题），但 `ParquetBarLoader.load_bars` 会 glob 全部文件并逐个 read"
    "（**不做按名跳过**），单次装载会明显变慢 —— 根治方案是给 loader 加**按符号索引文件**，属后续优化；",
    "`audit_log_lines_delta` 是**审计日志行数**（仅当标的数 > 50 时每 10 个标的记一条），"
    "**为 0 不代表没抓**；",
    "`incremental` 的数字由 run 前后两份缓存清单快照做差得到，因此 `requests_avoided_symbols` 的口径是"
    "「本次未写入缓存的标的数」（缓存新鲜期内不发请求；`--refresh` 时该值恒为 0）",
)


@dataclass(slots=True)
class FetchOutcome:
    """一次 `run_fetch` 的完整结果。

    保留 `ingest` 对象是为了让「报告的 quality 段 == 流水线的 to_dict()」这类
    **单一真相**断言可以逐键比对（只读文件无法证明这一点）。
    """

    summary: dict[str, Any]
    ingest: IngestReport | None
    quality: ProviderQualityReport | None
    manifest_projection: dict[str, Any]
    written: dict[str, Path]

    @property
    def exit_code(self) -> int:
        return int(self.summary["exit_code"])


# --------------------------------------------------------------------------- #
# 小结构
# --------------------------------------------------------------------------- #
class Stopwatch:
    """分段计时（秒）：``mark(name)`` 记录**自上次 mark 起**的独占耗时。"""

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self._origin = clock()
        self._last = self._origin
        self.spans: dict[str, float] = {}

    def mark(self, name: str) -> float:
        now = self._clock()
        self.spans[name] = round(now - self._last, 3)
        self._last = now
        return self.spans[name]

    def total(self) -> float:
        return round(self._clock() - self._origin, 3)


@dataclass(frozen=True, slots=True)
class CandidateSet:
    """候选池解析结果（`--symbols` ∪ `--index` 成分 → 排序去重 → 截断）。"""

    total: int
    kept: tuple[str, ...]
    truncated: bool
    source: str

    def as_dict(self, *, max_symbols: int | None) -> dict[str, Any]:
        return {
            "total": self.total,
            "kept": len(self.kept),
            "source": self.source,
            "max_symbols": max_symbols,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """缓存清单的一条（`CacheMeta` 的只读视图，用于做差与投影）。"""

    dataset: str
    key: str
    rows: int
    start: str | None
    end: str | None
    fetched_at: str
    path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "key": self.key,
            "rows": self.rows,
            "start": self.start,
            "end": self.end,
            "fetched_at": self.fetched_at,
            "path": self.path,
        }


@dataclass(slots=True)
class WriteStats:
    """一标的一文件落盘统计。"""

    written: dict[str, int] = field(default_factory=dict)
    skipped_empty: list[str] = field(default_factory=list)
    total_bytes: int = 0
    root: Path | None = None

    @property
    def files(self) -> int:
        return len(self.written)


@dataclass(slots=True)
class MaterializeResult:
    """落盘结果（供 summary 直接消费，不含任何"再算一遍"的数字）。"""

    raw_files: int
    raw_rows: int
    raw_bytes: int
    skipped_empty: tuple[str, ...]
    index_path: Path | None
    index_rows: int
    fundamental_path: Path | None
    fundamental_rows: int
    cleared_files: tuple[str, ...]


# --------------------------------------------------------------------------- #
# 纯函数：参数解析 / 候选池 / 清单 / digest / 退出码
# --------------------------------------------------------------------------- #
def parse_symbol_list(value: str | None) -> tuple[str, ...]:
    """``--symbols "600000,000001"`` → 规范化候选（去空、去重、保持输入顺序）。"""
    if not value:
        return ()
    seen: dict[str, None] = {}
    for item in str(value).split(","):
        text = item.strip()
        if text:
            seen.setdefault(text, None)
    return tuple(seen)


def parse_index_codes(value: str | None) -> tuple[str, ...]:
    """``--index`` 解析：必须带交易所后缀（与 provider 的严格口径一致）。"""
    if not value:
        return ()
    codes: list[str] = []
    for item in str(value).split(","):
        text = item.strip().upper()
        if not text:
            continue
        if "." not in text:
            raise ConfigError(
                f"指数代码必须带交易所后缀（收到 {text!r}）：裸代码无法区分 000300.SH 与 399300.SZ"
            )
        codes.append(text)
    return tuple(dict.fromkeys(codes))


def resolve_candidates(
    *,
    cli_symbols: Sequence[str] = (),
    index_members: Sequence[str] = (),
    max_symbols: int | None = None,
) -> CandidateSet:
    """候选池 = ``--symbols`` ∪ 指数成分 → 规范化 → **排序** → 截断前 N。

    排序是刻意的：``--max-symbols`` 必须**确定性**（同一命令任何机器都截同一批），
    否则「只跑了 50 只」这件事无法复现。
    """
    union = {str(s).strip().upper() for s in (*cli_symbols, *index_members) if str(s).strip()}
    ordered = tuple(sorted(union))
    if max_symbols is None or max_symbols >= len(ordered):
        kept, truncated = ordered, False
    else:
        kept, truncated = ordered[:max_symbols], True
    if cli_symbols and index_members:
        source = "cli+index"
    elif cli_symbols:
        source = "cli"
    elif index_members:
        source = "index"
    else:
        source = "none"
    return CandidateSet(total=len(ordered), kept=kept, truncated=truncated, source=source)


def read_manifest_payload(path: str | Path) -> dict[str, Any] | None:
    """读取权威缓存清单的原始 JSON；缺失或损坏时返回 ``None``（**不猜**）。"""
    target = Path(path)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def canonical_json_digest(payload: Mapping[str, Any] | None) -> str:
    """规范化 JSON（``sort_keys`` + 紧凑分隔符 + UTF-8）的 sha256 前 16 hex。

    规范化是必须的：否则「同一份清单换个键顺序」就会得到不同 digest，
    投影文件就无法证明「当时看到的是这一份」。
    """
    if payload is None:
        return MANIFEST_ABSENT
    blob = json.dumps(payload, sort_keys=True, separators=_COMPACT_SEPARATORS, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:DIGEST_HEX]


def manifest_entries(payload: Mapping[str, Any] | None) -> dict[str, ManifestEntry]:
    """``{dataset::key: ManifestEntry}``（清单缺失时为空字典）。"""
    entries: dict[str, ManifestEntry] = {}
    raw = (payload or {}).get("entries") or {}
    if not isinstance(raw, Mapping):
        return entries
    for entry_key, value in raw.items():
        if not isinstance(value, Mapping):
            continue
        entries[str(entry_key)] = ManifestEntry(
            dataset=str(value.get("dataset", "")),
            key=str(value.get("key", "")),
            rows=int(value.get("rows", 0) or 0),
            start=value.get("start"),
            end=value.get("end"),
            fetched_at=str(value.get("fetched_at", "")),
            path=str(value.get("path", "")),
        )
    return entries


def diff_manifests(
    before: Mapping[str, ManifestEntry],
    after: Mapping[str, ManifestEntry],
    *,
    mode: str,
    audit_log_lines_delta: int,
) -> dict[str, Any]:
    """run 前后两份清单快照做差 → `summary.incremental`（纯函数）。"""
    datasets: dict[str, dict[str, Any]] = {}
    for dataset in sorted({e.dataset for e in (*before.values(), *after.values())}):
        old = {k: v for k, v in before.items() if v.dataset == dataset}
        new = {k: v for k, v in after.items() if v.dataset == dataset}
        refreshed = [
            key
            for key, entry in new.items()
            if key not in old or old[key].fetched_at != entry.fetched_at
        ]
        untouched = [
            key for key, entry in new.items() if key in old and old[key].fetched_at == entry.fetched_at
        ]
        rows_before = sum(v.rows for v in old.values())
        rows_after = sum(v.rows for v in new.values())
        datasets[dataset] = {
            "keys_before": len(old),
            "keys_after": len(new),
            "keys_refreshed": len(refreshed),
            "keys_untouched": len(untouched),
            "rows_before": rows_before,
            "rows_after": rows_after,
            "rows_delta": rows_after - rows_before,
            "max_date_before": _max_date(old.values()),
            "max_date_after": _max_date(new.values()),
        }
    bars = datasets.get("bars", {})
    return {
        "mode": mode,
        "by_dataset": datasets,
        "requests_avoided_symbols": int(bars.get("keys_untouched", 0)),
        "audit_log_lines_delta": int(audit_log_lines_delta),
        "unit": "keys=缓存样本数（bars 下 1 key=1 标的）；rows_delta=缓存记录数变化（新增+补抓）",
        "method": (
            "run 前后各读一次 <cache-root>/manifest.json 做差；不读 provider 私有统计。"
            "requests_avoided_symbols = 本次**未写入缓存**的 bars 样本数"
            "（缓存新鲜期内不发请求；--refresh 时恒为 0）"
        ),
    }


def _max_date(entries: Any) -> str | None:
    values = [e.end for e in entries if e.end]
    return max(values) if values else None


def estimate_exit_if_live(*, status: str, has_errors: bool, fail_on_degraded: bool) -> int:
    """退出码判定（**live 与 dry-run 的估计值共用**，保证两处口径一致）。

    - ``failed`` 或 ``has_errors`` → 2。``has_errors`` 取 `IngestReport.has_errors`
      （质量报告有 error，或校验步骤 failed）：质量报错却返回 0，等于让 CI 把「数据坏了」读成成功；
    - ``degraded`` 且 ``--fail-on-degraded`` → 2；
    - 其余 → 0。
    """
    if status == "failed" or has_errors:
        return EXIT_FAILED
    if status == "degraded" and fail_on_degraded:
        return EXIT_FAILED
    return EXIT_OK


def exit_reason(*, status: str, has_errors: bool, fail_on_degraded: bool, mode: str) -> str:
    """给退出码配一句可追溯的理由（写进 summary）。"""
    if mode == "dry-run":
        return "dry-run 恒 0（脚本自身跑通即通过）；真实估计值见 dry_run_estimated_exit_if_live"
    if has_errors:
        return "IngestReport.has_errors=True（质量报告有 error，或校验步骤 failed）"
    if status == "failed":
        return "取数流程失败（必需步骤失败或未完成九步）"
    if status == "degraded" and fail_on_degraded:
        return "整体 degraded 且指定了 --fail-on-degraded"
    if status == "degraded":
        return "degraded 不算失败（无 error 且必需步骤完成）"
    return "全部必需步骤 ok"


def count_audit_lines(path: str | Path) -> int:
    """审计日志行数（不存在 → 0）。**不是网络请求计数**，口径见 LIMITATIONS。"""
    target = Path(path)
    if not target.exists():
        return 0
    try:
        with target.open("r", encoding="utf-8") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def audit_log_path(cache_root: str | Path, provider_name: str = "akshare") -> Path:
    """provider 的审计日志路径（与 `AKShareProvider._resolve_audit_path` 同构）。"""
    return Path(cache_root) / provider_name / "audit.log"


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #
def plan_run_dir(out_root: str | Path, *, mode: str) -> tuple[Path, str]:
    """建本次运行目录：``<out>/[dry-run/]<run_id>``。

    用 ``os.mkdir(exist_ok=False)`` + 捕获 ``FileExistsError`` 试 ``-2``/``-3``
    （**不是 TOCTOU**：先 exists() 再 mkdir 会在并发下撞车）；
    崩溃残留的目录**绝不复用**（新 run_id），避免两次运行的结果混在一起。
    """
    root = Path(out_root)
    if mode == "dry-run":
        root = root / DRY_RUN_ROOT_NAME
    root.mkdir(parents=True, exist_ok=True)
    base = utc_now().strftime(RUN_ID_FORMAT)
    for suffix in range(0, 100):
        run_id = base if suffix == 0 else f"{base}-{suffix + 1}"
        try:
            (root / run_id).mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return root / run_id, run_id
    raise DataError(f"无法在 {root} 下创建唯一 run 目录（同名冲突超过 100 次）：{base}")


def assert_within_dry_run(paths: Sequence[Path], *, dry_run_root: Path) -> None:
    """dry-run 的硬安全边界：所有写入路径必须落在 ``<out>/dry-run/`` 之内。

    越过边界的后果是**污染生产数据**（fixture 数据进 `data/cache` 后，之后真实抓取会命中
    假缓存当真实数据用，而且不报错）—— 因此这里直接抛异常，不做"尽力而为"。
    """
    resolved_root = Path(dry_run_root).resolve()
    for path in paths:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(resolved_root):
            raise DataError(
                f"dry-run 安全边界被越过：{resolved} 不在 {resolved_root} 之内。"
                "dry-run 只允许写入 <out>/dry-run/ 下的路径（避免污染生产数据与缓存）"
            )


def clear_data_root(root: str | Path) -> list[str]:
    """整体替换前的清理：删除该目录下**所有文件**（跨格式），保留 ``.gitkeep``。

    跨格式是必须的：先 ``--fmt parquet`` 落 100 个文件，再 ``--fmt csv --refresh``
    若只清 csv，就会得到两批数据同目录，下游 glob 到哪批取决于后缀。
    **不递归删目录**：子目录存在即报错（`ParquetBarLoader` 是非递归 glob，
    子目录里的数据加载器读不到 —— 那属于"文件在、数据没了"的静默失真，必须人工处理）。
    """
    target = Path(root)
    if not target.exists():
        return []
    removed: list[str] = []
    for entry in sorted(target.iterdir()):
        if entry.is_dir():
            raise DataError(
                f"{target} 下存在子目录 {entry.name!r}：该目录不允许子目录"
                "（加载器用非递归 glob，子目录里的数据会被静默忽略）。请人工清理后重试 --refresh"
            )
        if entry.name == ".gitkeep":
            continue
        entry.unlink()
        removed.append(entry.name)
    return removed


def _safe_symbol(symbol: str) -> str:
    text = str(symbol).strip()
    if not text or "/" in text or "\\" in text or text in (".", ".."):
        raise DataError(f"非法标的代码，不能作为文件名：{symbol!r}")
    return text


def write_symbol_frames(
    frames: Mapping[str, pd.DataFrame], root: str | Path, *, fmt: str
) -> WriteStats:
    """一标的一文件原子落盘；**空结果不落盘**（空文件会被下游当成"有该标的但无数据"）。"""
    target = Path(root)
    target.mkdir(parents=True, exist_ok=True)
    stats = WriteStats(root=target)
    for symbol in sorted(frames):
        frame = frames[symbol]
        name = _safe_symbol(symbol)
        if frame is None or len(frame) == 0:
            stats.skipped_empty.append(name)
            continue
        path = atomic_write_frame(target / f"{name}.{fmt}", frame, fmt=fmt)
        stats.written[name] = int(len(frame))
        stats.total_bytes += path.stat().st_size
    return stats


def assert_no_subdirectories(root: str | Path) -> None:
    """``data/raw`` 下不得有子目录（见 `clear_data_root` 的理由）。"""
    target = Path(root)
    if not target.exists():
        return
    subdirs = sorted(p.name for p in target.iterdir() if p.is_dir())
    if subdirs:
        raise DataError(f"{target} 下出现子目录 {subdirs}：加载器用非递归 glob，会导致数据被静默忽略")


def materialize(
    ingest: IngestReport,
    *,
    data_root: str | Path,
    index_dir: str | Path,
    fundamental_dir: str | Path,
    fmt: str = DEFAULT_FMT,
    refresh: bool = False,
) -> MaterializeResult:
    """把取到的数据落成下游可直接读的形态（见模块 docstring 的目录约定）。"""
    if fmt not in SUPPORTED_FMT:
        raise DataError(f"--fmt 只能是 {list(SUPPORTED_FMT)}，收到 {fmt!r}")
    dirs = (Path(data_root), Path(index_dir), Path(fundamental_dir))
    cleared: list[str] = []
    if refresh:
        for directory in dirs:
            cleared.extend(clear_data_root(directory))

    bars = ingest.store.bars_frame
    frames = {
        str(symbol): group.sort_values("date", kind="stable")
        for symbol, group in bars.groupby("symbol", observed=True, sort=True)
    }
    stats = write_symbol_frames(frames, dirs[0], fmt=fmt)
    assert_no_subdirectories(dirs[0])

    index_path, index_rows = _write_side_table(
        ingest.store.index_members_frame, dirs[1], "index_members", fmt=fmt,
        sort_by=["index_code", "effective_from", "symbol"],
    )
    fundamental_path, fundamental_rows = _write_side_table(
        ingest.store.fundamentals_frame, dirs[2], "fundamentals", fmt=fmt,
        sort_by=["announce_date", "symbol", "report_period"],
    )
    return MaterializeResult(
        raw_files=stats.files,
        raw_rows=sum(stats.written.values()),
        raw_bytes=stats.total_bytes,
        skipped_empty=tuple(stats.skipped_empty),
        index_path=index_path,
        index_rows=index_rows,
        fundamental_path=fundamental_path,
        fundamental_rows=fundamental_rows,
        cleared_files=tuple(cleared),
    )


def _write_side_table(
    frame: pd.DataFrame | None,
    directory: Path,
    stem: str,
    *,
    fmt: str,
    sort_by: Sequence[str],
) -> tuple[Path | None, int]:
    """侧表（指数成分 / 财务）单文件落盘；为空则不落盘（返回 ``None``）。"""
    if frame is None or len(frame) == 0:
        return None, 0
    columns = [c for c in sort_by if c in frame.columns]
    ordered = frame.sort_values(columns, kind="stable") if columns else frame
    path = atomic_write_frame(directory / f"{stem}.{fmt}", ordered, fmt=fmt)
    return path, int(len(ordered))


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def build_quality_payload(
    quality: ProviderQualityReport,
    *,
    provider_name: str,
    scope: Mapping[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    """质量报告文件：外层包装（口径/来源）+ **内嵌 `to_dict()` 原样**。

    刻意不另起一套 schema：`to_dict()` 已被 `IngestReport.to_dict()` 消费、已有用例覆盖；
    再写一份必然与内存里的那份漂移（表现为"报告说没问题，内存里其实有 error"）。
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "provider": provider_name,
        "scope": dict(scope),
        "quality": quality.to_dict(),
    }


def build_manifest_projection(
    *,
    entries: Mapping[str, ManifestEntry],
    source_path: Path,
    digest_before: str,
    digest_after: str,
    generated_at: str,
) -> dict[str, Any]:
    """缓存清单的**投影**（权威清单只读，不复制真相）。"""
    rows: list[dict[str, Any]] = []
    by_dataset: dict[str, dict[str, int]] = {}
    missing: list[str] = []
    for entry in sorted(entries.values(), key=lambda e: (e.dataset, e.key)):
        exists = bool(entry.path) and Path(entry.path).exists()
        if not exists:
            missing.append(entry.path or f"{entry.dataset}::{entry.key}")
        payload = entry.as_dict()
        payload["path_exists"] = exists
        rows.append(payload)
        stats = by_dataset.setdefault(entry.dataset, {"keys": 0, "rows": 0})
        stats["keys"] += 1
        stats["rows"] += entry.rows
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "source_manifest_path": str(source_path),
        "source_manifest_digest_before": digest_before,
        "source_manifest_digest_after": digest_after,
        "entries_total": len(rows),
        "by_dataset": by_dataset,
        "entries": rows,
        "missing_paths": missing,
    }


def render_report_md(
    summary: Mapping[str, Any],
    quality: ProviderQualityReport | None,
    manifest_projection: Mapping[str, Any],
) -> str:
    """人读版报告：数字全部来自 summary/manifest（**不重算**）。"""
    mode = summary.get("mode")
    lines: list[str] = ["# AKShare 数据抓取报告", ""]
    if mode == "dry-run":
        lines.append(
            "> **模式：dry-run** —— 数据来自 `tests/fixtures/akshare/` 的**手工构造样例**，"
            "**不是真实抓取**；退出码恒 0，真实估计值见 `dry_run_estimated_exit_if_live`。"
        )
    else:
        lines.append(
            f"> **模式：live（真实联网抓取）** —— provider `{(summary.get('provider') or {}).get('name')}`"
            f"，akshare 版本 `{(summary.get('provider') or {}).get('akshare_version')}`。"
        )
    lines.append("")
    lines.append("## 生成信息（口径 / 来源 / 命令 三件套）")
    lines.append("")
    invocation = summary.get("invocation") or {}
    lines.append(f"- **生成命令**：`{summary.get('command')}`")
    lines.append(f"- **cwd**：`{invocation.get('cwd')}`（命令与相对路径都以此为前提）")
    git = invocation.get("git") or {}
    lines.append(f"- **Git**：`{git.get('short') or 'unknown'}`（branch `{git.get('branch')}`）")
    lines.append(f"- **开始/结束（UTC）**：`{summary.get('created_at')}` → `{summary.get('finished_at')}`")
    lines.append(f"- **run_id**：`{summary.get('run_id')}` ｜ **报告目录**：`{summary.get('out')}`")
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    lines.append(f"- **整体状态**：`{summary.get('status')}`（搬运 `IngestReport.overall_status`，未重算）")
    lines.append(f"- **退出码**：`{summary.get('exit_code')}` —— {summary.get('exit_code_reason')}")
    if mode == "dry-run":
        lines.append(
            f"- **若为 live 的估计退出码**：`{summary.get('dry_run_estimated_exit_if_live')}`"
            "（估计值：由 fixture 上的质量报告与步骤状态推出，**不保证**与真实 live 一致）"
        )
    failure = summary.get("failure")
    if failure:
        lines.append(f"- **失败原因**：`{failure.get('type')}` {failure.get('message')}")
    lines.append("")
    request = summary.get("request") or {}
    lines.append("## 请求与落盘口径")
    lines.append("")
    lines.append(f"- **区间**：`{request.get('start')}` ~ `{request.get('end')}`（{request.get('days')} 天）")
    lines.append(f"- **指数**：{request.get('index_codes')} ｜ **标的**：{request.get('symbols')}")
    lines.append(f"- **复权**：`{request.get('adjust')}` ｜ **格式**：`{request.get('fmt')}`"
                 f" ｜ **refresh**：{request.get('refresh')}")
    artifacts = summary.get("artifacts") or {}
    lines.append(f"- **数据落盘**：`{artifacts.get('data_root')}`（`{artifacts.get('data_format')}`）")
    lines.append(f"- **回读方式**：`{artifacts.get('provider_for_readback')}`")
    lines.append("")
    lines.append("## 计数")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    for key, value in (summary.get("counts") or {}).items():
        lines.append(f"| `{key}` | {value} |")
    lines.append("")
    lines.append("## 步态与每步耗时")
    lines.append("")
    lines.append("| 步骤 | 状态 | 耗时(s) |")
    lines.append("| --- | --- | --- |")
    timing = summary.get("timing") or {}
    step_seconds = timing.get("steps") or {}
    for step in INGEST_STEPS:
        status = (summary.get("steps") or {}).get(step, "—")
        seconds = step_seconds.get(step, "—")
        lines.append(f"| `{step}` | `{status}` | {seconds} |")
    lines.append("")
    lines.append(
        f"- 总耗时 {timing.get('total_seconds')}s（ingest {timing.get('ingest_seconds')}s / "
        f"落盘 {timing.get('materialize_seconds')}s / 报告 {timing.get('reports_seconds')}s）"
    )
    lines.append(f"- 每步耗时口径：{timing.get('steps_note')}")
    lines.append("")
    lines.append("## 本次增量（断点续抓的证据）")
    lines.append("")
    incremental = summary.get("incremental") or {}
    lines.append(f"- **模式**：`{incremental.get('mode')}` ｜ "
                 f"**未写入缓存的标的数**：`{incremental.get('requests_avoided_symbols')}`")
    for dataset, stats in (incremental.get("by_dataset") or {}).items():
        lines.append(f"- `{dataset}`：{stats}")
    lines.append(f"- 口径：{incremental.get('method')}")
    lines.append(f"- 审计日志（{incremental.get('audit_log_lines_delta')} 行，行数≠请求数）：{summary.get('audit')}")
    lines.append("")
    lines.append("## 降级披露")
    lines.append("")
    notes = summary.get("degradation_notes") or []
    if notes:
        for note in notes:
            lines.append(f"- {note}")
    else:
        lines.append("- 本次无降级/近似披露")
    lines.append("")
    lines.append("## 缓存清单投影")
    lines.append("")
    lines.append(f"- 权威清单：`{manifest_projection.get('source_manifest_path')}`"
                 f"（digest `{manifest_projection.get('source_manifest_digest_after')}`）")
    lines.append(f"- 本次 entries：{manifest_projection.get('entries_total')} 条"
                 f"；缺失文件：{manifest_projection.get('missing_paths')}")
    lines.append("")
    if quality is not None:
        lines.append("## 数据质量（Q1~Q12 + 取数层披露）")
        lines.append("")
        lines.append(quality.to_markdown())
    lines.append("## 本报告的局限")
    lines.append("")
    for item in summary.get("limitations") or []:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def write_run_reports(
    run_dir: str | Path,
    *,
    summary: Mapping[str, Any],
    quality: ProviderQualityReport | None,
    manifest_projection: Mapping[str, Any],
) -> dict[str, Path]:
    """原子写出四份产物（JSON 用 ``ensure_ascii=False`` 保留中文）。"""
    target = Path(run_dir)
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    written[SUMMARY_NAME] = atomic_write_text(
        target / SUMMARY_NAME, json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )
    written[MANIFEST_NAME] = atomic_write_text(
        target / MANIFEST_NAME, json.dumps(manifest_projection, ensure_ascii=False, indent=2) + "\n"
    )
    quality_payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": summary.get("finished_at"),
        "provider": (summary.get("provider") or {}).get("name"),
        "scope": {
            "start": (summary.get("request") or {}).get("start"),
            "end": (summary.get("request") or {}).get("end"),
            "symbols": (summary.get("counts") or {}).get("symbols_requested"),
            "mode": summary.get("mode"),
        },
        "quality": (quality.to_dict() if quality is not None else None),
    }
    written[QUALITY_NAME] = atomic_write_text(
        target / QUALITY_NAME, json.dumps(quality_payload, ensure_ascii=False, indent=2) + "\n"
    )
    written[REPORT_NAME] = atomic_write_text(
        target / REPORT_NAME, render_report_md(summary, quality, manifest_projection)
    )
    return written


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
def _dry_run_paths(out_root: Path, run_dir: Path) -> dict[str, Path]:
    """dry-run 的全部写入路径（都在 ``<out>/dry-run/`` 之内）。

    缓存刻意放在 ``<out>/dry-run/cache`` 而**不是**每个 run 目录下：两次 dry-run 必须共用
    同一份缓存，才能验证「断点续抓」（第二次对可缓存端点 0 次调用）。它仍然完全落在
    dry-run 边界内，永远不会碰生产缓存。
    """
    return {
        "run_dir": run_dir,
        "dry_run_root": out_root / DRY_RUN_ROOT_NAME,
        "data_root": run_dir / "data" / "raw",
        "index_dir": run_dir / "data" / "index",
        "fundamental_dir": run_dir / "data" / "fundamental",
        "cache_root": out_root / DRY_RUN_ROOT_NAME / "cache",
    }


def _build_provider(
    args: argparse.Namespace,
    *,
    config: Any,
    mode: str,
    cache_root: Path,
    client: Any | None,
    provider: Any | None,
) -> tuple[Any, Any | None]:
    """构造 provider：真实路径走注册表；dry-run 用假客户端 + 隔离缓存。

    Returns:
        ``(provider, client)``：client 供报告标注（dry-run 的版本号写着假客户端名）。
    """
    if provider is not None:
        return provider, client
    if mode == "live":
        # 注意参数顺序：build_provider(config, *, provider=...)，名字是关键字
        return build_provider(config.data, provider="akshare"), None
    from aqs.data.akshare_provider import AKShareProvider  # noqa: PLC0415 - 仅 dry-run 需要
    from aqs.data.cache import DataCache  # noqa: PLC0415
    from aqs.data.ratelimit import RateLimiter  # noqa: PLC0415
    from tests.fake_akshare import FakeAKShareClient  # noqa: PLC0415 - dry-run 依赖 tests/

    if client is None:
        client = FakeAKShareClient(args.fixture_root)
    data_config: DataConfig = config.data
    # 固定时钟：让快照类端点（成分/行业）落在 fixture 窗口内，且两次 dry-run 完全可复现
    dry_run_now = pd.Timestamp(DRY_RUN_NOW).to_pydatetime()
    provider_obj = AKShareProvider(
        data_config,
        client=client,
        cache=DataCache(
            cache_root,
            version=data_config.cache.version,
            fmt=data_config.cache.fmt,
            ttl_hours=data_config.cache.ttl_hours,
            provider="akshare",
        ),
        # dry-run 读本地 fixture，没有"对端配额"要保护 → 不限流（否则白等）
        limiter=RateLimiter(requests_per_minute=0),
        sleeper=lambda _seconds: None,
        now=lambda: dry_run_now,
    )
    return provider_obj, client


def akshare_version(*, mode: str, client: Any | None) -> str | None:
    """akshare 版本：dry-run 标注假客户端；未安装/取不到 → ``None``（**不猜**）。"""
    if mode == "dry-run":
        label = getattr(client, "__name__", None) or type(client).__name__
        return f"dry-run({label})"
    module = sys.modules.get("akshare")
    if module is None:
        try:
            import akshare as module  # noqa: PLC0415 - 惰性导入，只为取版本号
        except Exception:  # noqa: BLE001 - 未安装/导入失败都退化为 None
            return None
    version = getattr(module, "__version__", None)
    return None if version is None else str(version)


def data_config_snapshot(config: Any, *, mode: str, cache_root: Path, config_file: str) -> dict[str, Any]:
    """写进报告的**口径快照**：所有会影响结果的取数参数（便于日后复查"当时用的什么"）。"""
    data = config.data
    rate_limit = data.rate_limit
    return {
        "config_file": config_file,
        "provider": data.provider,
        "root": data.root,
        "adjustment": data.adjustment,
        "failure_policy": data.failure_policy,
        "max_missing_ratio": data.max_missing_ratio,
        "listing_date_policy": data.listing_date.policy,
        "quality_strict": data.quality.strict,
        "unit_conversion": {
            "volume_to_shares": data.unit_conversion.volume_to_shares,
            "amount_to_yuan": data.unit_conversion.amount_to_yuan,
        },
        "cache": {
            "root": str(cache_root),
            "version": data.cache.version,
            "fmt": data.cache.fmt,
            "ttl_hours": data.cache.ttl_hours,
            "refresh": data.cache.refresh,
        },
        "rate_limit": (
            {"enabled": False, "source": "dry-run 读本地 fixture，未限流"}
            if mode == "dry-run"
            else {
                "enabled": bool(rate_limit.enabled),
                "requests_per_minute": rate_limit.requests_per_minute,
                "burst": rate_limit.burst,
                "min_interval_ms": rate_limit.min_interval_ms,
                "source": f"{config_file} → data.rate_limit",
            }
        ),
        "clock": (
            f"dry-run 固定时钟 {DRY_RUN_NOW}（真实运行用系统 UTC 时钟）"
            if mode == "dry-run"
            else "系统 UTC 时钟"
        ),
    }


def run_fetch(
    args: argparse.Namespace,
    *,
    mode: str,
    run_dir: Path,
    dry_run_root: Path | None,
    argv: Sequence[str] | None = None,
    provider: Any | None = None,
    client: Any | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> FetchOutcome:
    """执行一次抓取并写出四份报告，返回 :class:`FetchOutcome`。"""
    watch = Stopwatch(clock)
    started = utc_now()
    out_root = Path(args.out)
    paths = _dry_run_paths(out_root, run_dir) if mode == "dry-run" else {
        "run_dir": run_dir,
        "data_root": Path(args.data_root),
        # 侧表目录与 data-root **同源**（`data/raw` → `data/index` / `data/fundamental`），
        # 与 `load_market_data` 的既有约定一致，避免出现第二套目录规则
        "index_dir": Path(args.data_root).parent / "index",
        "fundamental_dir": Path(args.data_root).parent / "fundamental",
        "cache_root": Path(load_base_config(args.config).data.cache.root),
    }
    data_root = paths["data_root"]
    index_dir = paths["index_dir"]
    fundamental_dir = paths["fundamental_dir"]
    cache_root = paths["cache_root"]
    if mode == "dry-run":
        assert dry_run_root is not None
        assert_within_dry_run(
            [run_dir, data_root, index_dir, fundamental_dir, cache_root], dry_run_root=dry_run_root
        )

    index_codes = parse_index_codes(args.index)
    symbols = parse_symbol_list(args.symbols)
    overrides: dict[str, Any] = {
        "data": {"provider": "akshare", "root": str(data_root)},
        "universe": {"mode": "index" if index_codes else "all", "fallback_to_all": True},
    }
    if mode == "dry-run":
        overrides["data"]["cache"] = {"root": str(cache_root)}
    config = load_base_config(args.config, overrides=overrides)
    universe = config.universe

    manifest_path = cache_root / "manifest.json"
    audit_path = audit_log_path(cache_root)
    payload_before = read_manifest_payload(manifest_path)
    digest_before = canonical_json_digest(payload_before)
    entries_before = manifest_entries(payload_before)
    audit_before = count_audit_lines(audit_path)

    provider_obj, actual_client = _build_provider(
        args, config=config, mode=mode, cache_root=cache_root, client=client, provider=provider
    )
    capabilities = provider_obj.capabilities()
    provider_info = {
        "name": provider_obj.name,
        "akshare_version": akshare_version(mode=mode, client=actual_client),
        "capabilities": _capabilities_dict(capabilities),
        "data_config": data_config_snapshot(
            config, mode=mode, cache_root=cache_root, config_file=args.config
        ),
    }

    failure: dict[str, Any] | None = None
    ingest: IngestReport | None = None
    materialized: MaterializeResult | None = None
    candidates: CandidateSet | None = None
    truncation_note: str | None = None
    ingest_seconds = 0.0
    try:
        members = _fetch_index_symbols(provider_obj, index_codes, args)
        candidates = resolve_candidates(
            cli_symbols=symbols, index_members=members, max_symbols=args.max_symbols
        )
        if candidates.truncated:
            truncation_note = (
                f"--max-symbols={args.max_symbols} 截断了候选池："
                f"{candidates.total} → {len(candidates.kept)}（取排序后的前 N 个，确定性）"
            )
        watch.mark("resolve")
        ingest = ingest_from_provider(
            provider_obj,
            config=config.data,
            universe_config=universe,
            index_codes=index_codes,
            start=args.start,
            end=args.end,
            symbols=list(candidates.kept) or None,
            fundamentals=not args.no_fundamentals,
            industry=not args.no_industry,
        )
        ingest_seconds = watch.mark("ingest")
        if truncation_note:
            ingest.quality.add(
                QualityFinding(
                    code=TRUNCATION_FINDING, level="warning", message=truncation_note,
                    count=candidates.total - len(candidates.kept),
                )
            )
        materialized = materialize(
            ingest,
            data_root=data_root,
            index_dir=index_dir,
            fundamental_dir=fundamental_dir,
            fmt=args.fmt,
            refresh=args.refresh,
        )
        watch.mark("materialize")
        status, has_errors = ingest.overall_status, ingest.has_errors
    except (DataError, DataQualityError, ConfigError) as exc:
        failure = {"type": type(exc).__name__, "message": str(exc)}
        status, has_errors = "failed", True
        ingest_seconds = watch.spans.get("ingest", 0.0)

    payload_after = read_manifest_payload(manifest_path)
    digest_after = canonical_json_digest(payload_after)
    entries_after = manifest_entries(payload_after)
    audit_after = count_audit_lines(audit_path)
    incremental = diff_manifests(
        entries_before,
        entries_after,
        mode="refresh" if args.refresh else ("full" if not entries_before else "resume"),
        audit_log_lines_delta=audit_after - audit_before,
    )
    if truncation_note:
        incremental["truncation"] = truncation_note

    request = {
        "start": str(args.start),
        "end": str(args.end),
        "days": (pd.Timestamp(args.end) - pd.Timestamp(args.start)).days,
        "index_codes": list(index_codes),
        "symbols": (
            candidates.as_dict(max_symbols=args.max_symbols)
            if candidates is not None
            else {"total": 0, "kept": 0, "source": "unresolved", "max_symbols": args.max_symbols,
                  "truncated": False}
        ),
        "adjust": config.data.adjustment,
        "fmt": args.fmt,
        "fundamentals": not args.no_fundamentals,
        "industry": not args.no_industry,
        "refresh": bool(args.refresh),
    }

    quality = ingest.quality if ingest is not None else None
    counts = _counts(ingest, materialized, entries_after, candidates)
    artifacts = {
        "summary": SUMMARY_NAME,
        "quality_report": QUALITY_NAME,
        "manifest": MANIFEST_NAME,
        "report_md": REPORT_NAME,
        "data_root": str(data_root),
        "data_format": DATA_FORMAT,
        "provider_for_readback": {
            "name": _READBACK_PROVIDER[args.fmt],
            "root": str(data_root),
            "index_members_path": str(materialized.index_path) if materialized and materialized.index_path else None,
            "fundamentals_path": str(materialized.fundamental_path) if materialized and materialized.fundamental_path else None,
        },
        "index_members": str(materialized.index_path) if materialized and materialized.index_path else None,
        "fundamentals": str(materialized.fundamental_path) if materialized and materialized.fundamental_path else None,
    }
    estimate = estimate_exit_if_live(
        status=status, has_errors=has_errors, fail_on_degraded=args.fail_on_degraded
    )
    exit_code = EXIT_OK if mode == "dry-run" else estimate
    command = [f"python tools/{Path(__file__).name}", *(list(argv) if argv is not None else sys.argv[1:])]
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_dir.name,
        "mode": mode,
        "status": status,
        "exit_code": exit_code,
        "exit_code_reason": exit_reason(
            status=status, has_errors=has_errors, fail_on_degraded=args.fail_on_degraded, mode=mode
        ),
        "dry_run_estimated_exit_if_live": estimate if mode == "dry-run" else None,
        "dry_run_estimate_note": (
            "估计值：由 fixture 上的质量报告与步骤状态推出，**不保证**与真实 live 一致"
            if mode == "dry-run" else None
        ),
        "failure": failure,
        "command": " ".join(command),
        "invocation": build_invocation(command, project_root=_ROOT, extras={"mode": mode}),
        "out": str(run_dir),
        "created_at": started.isoformat(timespec="seconds"),
        "finished_at": utc_now().isoformat(timespec="seconds"),
        "provider": provider_info,
        "request": request,
        "counts": counts,
        "steps": dict(ingest.step_status) if ingest is not None else {},
        "degradation_notes": list(ingest.degradation_notes) if ingest is not None else [],
        "quality": {
            "ok": quality.ok if quality is not None else False,
            "errors": len(quality.errors()) if quality is not None else 0,
            "warnings": len(quality.warnings()) if quality is not None else 0,
            "report": QUALITY_NAME,
        },
        "manifest": {
            "entries_total": len(entries_after),
            "entries_before": len(entries_before),
            "by_dataset": {k: v["keys"] for k, v in _manifest_by_dataset(entries_after).items()},
            "source": str(manifest_path),
            "digest": digest_after,
            "report": MANIFEST_NAME,
        },
        "incremental": incremental,
        "timing": {
            "total_seconds": watch.total(),
            "ingest_seconds": ingest_seconds,
            "materialize_seconds": watch.spans.get("materialize", 0.0),
            "resolve_seconds": watch.spans.get("resolve", 0.0),
            "steps": dict(ingest.step_seconds) if ingest is not None else {},
            "steps_note": "每步为**该步独占**耗时；取数中途失败时 `steps` 可能不完整（不补零冒充完整）",
        },
        "artifacts": artifacts,
        "audit": {
            "path": str(audit_path),
            "lines_before": audit_before,
            "lines_after": audit_after,
            "lines_delta": audit_after - audit_before,
            "note": "审计日志行数（标的数 > 50 时每 10 个标的记一条）；**为 0 不代表没抓**",
        },
        "warnings_summary": _warnings_summary(quality),
        "limitations": list(LIMITATIONS),
    }
    manifest_projection = build_manifest_projection(
        entries=entries_after,
        source_path=manifest_path,
        digest_before=digest_before,
        digest_after=digest_after,
        generated_at=summary["finished_at"],
    )
    write_run_reports(
        run_dir, summary=summary, quality=quality, manifest_projection=manifest_projection
    )
    watch.mark("reports")
    summary["timing"]["reports_seconds"] = watch.spans.get("reports", 0.0)
    summary["timing"]["total_seconds"] = watch.total()
    # 只重写 summary：timing 的终值要落盘（原子写保证不会读到半截）
    atomic_write_text(run_dir / SUMMARY_NAME, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return FetchOutcome(
        summary=summary,
        ingest=ingest,
        quality=quality,
        manifest_projection=manifest_projection,
        written={name: run_dir / name for name in REPORT_FILES},
    )


def _capabilities_dict(capabilities: Any) -> dict[str, Any]:
    payload = {
        field: bool(getattr(capabilities, field))
        for field in (
            "daily_bars", "adjustment_factors", "suspensions", "price_limits", "st_flags",
            "listing_dates", "delistings", "index_members", "fundamentals", "industry",
            "market_cap", "intraday",
        )
    }
    payload["default_adjust"] = getattr(capabilities, "default_adjust", None)
    payload["notes"] = list(getattr(capabilities, "notes", ()))
    return payload


def _manifest_by_dataset(entries: Mapping[str, ManifestEntry]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for entry in entries.values():
        stats = out.setdefault(entry.dataset, {"keys": 0, "rows": 0})
        stats["keys"] += 1
        stats["rows"] += entry.rows
    return out


def _warnings_summary(quality: ProviderQualityReport | None, *, limit: int = 20) -> list[str]:
    if quality is None:
        return []
    out: list[str] = []
    for finding in quality.warnings()[:limit]:
        # `title` 对没有登记标题的编码（如取数层披露 I1）会回退成编码本身，别写成「I1 I1：」
        label = finding.code if finding.title == finding.code else f"{finding.code} {finding.title}"
        out.append(f"{label}：{finding.message}")
    return out


def _counts(
    ingest: IngestReport | None,
    materialized: MaterializeResult | None,
    entries: Mapping[str, ManifestEntry],
    candidates: CandidateSet | None,
) -> dict[str, Any]:
    """计数字段。

    **硬不变量**（有用例锁定）：``symbols_with_bars == raw_files_written == len(<data-root>/*.<fmt>)``。

    与 ``manifest_bars_keys`` 的关系刻意**不是**相等：provider 对「抓到空表的标的」也会写缓存样本
    （rows=0），但它不会出现在落盘文件里，故 ``manifest_bars_keys >= symbols_with_bars``，
    差值 = 空样本数。把这两者写成相等会是一句**假的不变量**。
    """
    files = materialized.raw_files if materialized is not None else 0
    requested = len(candidates.kept) if candidates is not None else 0
    manifest_bars_keys = sum(1 for entry in entries.values() if entry.dataset == "bars")
    counts: dict[str, Any] = {
        "symbols_requested": requested,
        "symbols_with_bars": files,
        "symbols_without_bars": max(requested - files, 0),
        "symbols_skipped_empty": len(materialized.skipped_empty) if materialized is not None else 0,
        "raw_files_written": files,
        "raw_files_cleared": len(materialized.cleared_files) if materialized is not None else 0,
        "raw_bytes": materialized.raw_bytes if materialized is not None else 0,
        "bars_rows": materialized.raw_rows if materialized is not None else 0,
        "bars_start": None,
        "bars_end": None,
        "index_member_rows": materialized.index_rows if materialized is not None else 0,
        "fundamental_rows": materialized.fundamental_rows if materialized is not None else 0,
        "manifest_bars_keys": manifest_bars_keys,
        "calendar_days": 0,
        "industry_rows": 0,
    }
    if ingest is not None:
        frame = ingest.store.bars_frame
        if len(frame):
            dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
            if len(dates):
                counts["bars_start"] = str(dates.min().date())
                counts["bars_end"] = str(dates.max().date())
            if "industry" in frame.columns:
                counts["industry_rows"] = int(frame["industry"].notna().sum())
        counts["calendar_days"] = len(ingest.store.trading_days())
    return counts


def _fetch_index_symbols(provider: Any, index_codes: Sequence[str], args: argparse.Namespace) -> list[str]:
    """先取指数成分用于**解析候选池**（供 `--max-symbols` 截断）；失败不中止。"""
    if not index_codes:
        return []
    members: list[str] = []
    for code in index_codes:
        try:
            frame, _provenance = provider.fetch_index_members(code, args.start, args.end)
        except DataError:
            continue
        if frame is not None and len(frame):
            members.extend(str(s) for s in frame["symbol"].tolist())
    return members


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _day_arg(value: str) -> str:
    try:
        parsed = pd.Timestamp(str(value).strip()).normalize()
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError(f"无法解析日期 {value!r}（示例：2020-01-01）") from exc
    if pd.isna(parsed):
        raise argparse.ArgumentTypeError(f"无法解析日期 {value!r}（示例：2020-01-01）")
    return str(parsed.date())


def build_parser() -> argparse.ArgumentParser:
    """CLI 定义（README 的命令必须与本解析器一致，由守护用例保证）。"""
    parser = argparse.ArgumentParser(
        prog="fetch_data.py",
        description="从 AKShare 抓取行情/成分/财务并落盘到 data/，产出四份报告",
        epilog=(
            "示例：python tools/fetch_data.py --start 2020-01-01 --end 2025-12-31 --index 000300.SH "
            "| python tools/fetch_data.py --dry-run --index 000300.SH"
        ),
    )
    parser.add_argument("--start", type=_day_arg, default=None, help="起始日（live 必填；dry-run 默认 fixture 区间）")
    parser.add_argument("--end", type=_day_arg, default=None, help="结束日（live 必填；dry-run 默认 fixture 区间）")
    parser.add_argument("--index", default=None, help="指数代码，逗号分隔（如 000300.SH,000905.SH）")
    parser.add_argument("--symbols", default=None, help="显式标的，逗号分隔；与指数成分取并集")
    parser.add_argument("--max-symbols", type=int, default=None, dest="max_symbols",
                        help="候选池上限（取排序后前 N 个，确定性）；用于冒烟/分片抓取")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"报告输出目录（默认 {DEFAULT_OUT}）")
    parser.add_argument("--data-root", default=DEFAULT_DATA_ROOT, dest="data_root",
                        help=f"行情落盘目录（默认 {DEFAULT_DATA_ROOT}）；dry-run 下不允许指定")
    parser.add_argument("--fmt", default=DEFAULT_FMT, choices=list(SUPPORTED_FMT),
                        help=f"落盘格式（默认 {DEFAULT_FMT}）")
    parser.add_argument("--incremental", action="store_true", help="增量续抓（默认行为的显式写法）")
    parser.add_argument("--refresh", action="store_true", help="整体替换：先清空数据目录再全量写")
    parser.add_argument("--no-fundamentals", action="store_true", dest="no_fundamentals", help="不抓财务")
    parser.add_argument("--no-industry", action="store_true", dest="no_industry", help="不抓行业")
    parser.add_argument("--fail-on-degraded", action="store_true", dest="fail_on_degraded",
                        help="整体 degraded 时也返回 2（默认关：降级不算失败）")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"主配置文件（默认 {DEFAULT_CONFIG}）")
    parser.add_argument("--dry-run", action="store_true", help="用 fixture 离线走全流程（不联网）")
    parser.add_argument("--fixture-root", default=None, dest="fixture_root",
                        help="dry-run 的 fixture 目录（默认 tests/fixtures/akshare）；只能与 --dry-run 同用")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    provider: Any | None = None,
    client: Any | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> int:
    """CLI 入口。

    Returns:
        退出码：live ``ok``/``degraded`` → 0；``failed`` 或质量有 error → 2；``--fail-on-degraded`` 时 degraded → 2；
        ``--dry-run`` 恒 0（真实估计值写进 ``dry_run_estimated_exit_if_live``）；用法错误由 argparse 返回 2。
    """
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.incremental and args.refresh:
        parser.error("--incremental 与 --refresh 互斥（前者=增量续抓，后者=整体替换）")
    if args.fixture_root and not args.dry_run:
        parser.error("--fixture-root 只能与 --dry-run 一起使用")
    if args.dry_run and args.data_root != DEFAULT_DATA_ROOT:
        parser.error(
            "--data-root 不能与 --dry-run 同用：dry-run 一律写入 <out>/dry-run/<run_id>/data/raw"
            "（避免以为在用 fixture 试跑、实际写了生产目录）"
        )
    if args.max_symbols is not None and args.max_symbols < 1:
        parser.error("--max-symbols 至少为 1")

    mode = "dry-run" if args.dry_run else "live"
    if args.start is None:
        if mode == "live":
            parser.error("--start 必填（live 模式）")
        args.start = DRY_RUN_START
    if args.end is None:
        if mode == "live":
            parser.error("--end 必填（live 模式）")
        args.end = DRY_RUN_END
    if pd.Timestamp(args.end) < pd.Timestamp(args.start):
        parser.error("--end 不能早于 --start")
    # dry-run 的默认指数要在「必须给出标的」之前补上（否则离线自检得先手写标的）
    if mode == "dry-run" and not args.index and not args.symbols:
        args.index = DRY_RUN_INDEX_CODE
    if not args.index and not args.symbols:
        parser.error("必须给出 --index 或 --symbols（akshare 逐标的抓取，无法「不过滤」）")

    out_root = Path(args.out)
    run_dir, run_id = plan_run_dir(out_root, mode=mode)
    dry_run_root = out_root / DRY_RUN_ROOT_NAME if mode == "dry-run" else None
    print(f"模式={mode} run_id={run_id} 报告目录={run_dir}")
    summary = run_fetch(
        args,
        mode=mode,
        run_dir=run_dir,
        dry_run_root=dry_run_root,
        argv=argv,
        provider=provider,
        client=client,
        clock=clock,
    ).summary
    counts = summary["counts"]
    print(
        f"状态={summary['status']} 退出码={summary['exit_code']} "
        f"标的={counts['symbols_with_bars']}/{counts['symbols_requested']} 行数={counts['bars_rows']}"
    )
    print("产物：" + " | ".join(str(run_dir / name) for name in REPORT_FILES))
    return int(summary["exit_code"])


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
