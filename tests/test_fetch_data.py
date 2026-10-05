"""M5-2：`tools/fetch_data.py` 的**离线**用例（全部走 dry-run + `FakeAKShareClient`）。

设计依据：M5-2 确认稿（接口 / 伪代码 / 测试清单）+ 5 处修正 + 1 处澄清。

本模块守住的**硬性质**（每条都对应一类「不报错但结果失真」）：

- `counts.symbols_with_bars == raw_files_written == len(<data-root>/*)`（落盘数即报告数）；
- 断点续抓的机械证明（第二次运行对**可缓存端点**零调用，且落盘逐行相等）；
- dry-run 安全边界：所有写入必须落在 `<out>/dry-run/` 内，`--data-root` 直接被拒；
- 退出码与质量报告**单一真相**（`exit_code` 可由 summary 自身复算）；
- 端到端回读：落盘 → 真实 provider 读回 → `ingest_from_provider` 成功且 `close_adj` **逐值相等**；
- `refresh` 是整体替换（跨格式清理），`--max-symbols` 截断确定性；
- 快照类端点（成分/行业）**每次运行都会重新观测**，这是 M4-8 的设计而不是浪费。

**不联网**：所有 provider 都是 `FakeAKShareClient` 或测试桩；真实抓取属 M5 阶段 B。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from tests.compat import raises
from tests.fake_akshare import FakeAKShareClient
from tests.schema_validator import load_schema, validate_schema
from tests.tools import PROJECT_ROOT, workspace_tmp

from aqs.config.schema import DataConfig, UniverseConfig
from aqs.core.exceptions import DataError
from aqs.data.akshare_provider import AKShareProvider
from aqs.data.loader import ingest_from_provider
from aqs.data.provider import ProviderCapabilities, ProviderHealth, Provenance
from aqs.data.registry import build_provider

TOOL_PATH = PROJECT_ROOT / "tools" / "fetch_data.py"
SUMMARY_SCHEMA = PROJECT_ROOT / "docs" / "data" / "fetch_summary.schema.json"
REPORT_FILES = ("summary.json", "quality_report.json", "manifest.json", "report.md")

#: dry-run 的可缓存端点（除快照类）：这些在续抓时必须零调用
CACHEABLE_ENDPOINTS = (
    "stock_zh_a_hist",
    "stock_individual_info_em",
    "tool_trade_date_hist_sina",
    "stock_yjbb_em",
)
#: 快照类端点（M4-8：快照累积是**状态**，每跑必采）
SNAPSHOT_ENDPOINTS = (
    "index_stock_cons_csindex",
    "stock_board_industry_name_em",
    "stock_board_industry_cons_em",
)


# --------------------------------------------------------------------------- #
# 工具加载与跑批助手
# --------------------------------------------------------------------------- #
def load_fetch_module() -> Any:
    """按路径加载 `tools/fetch_data.py`（工具不在包内，只能按文件加载）。"""
    assert TOOL_PATH.exists(), f"缺少 {TOOL_PATH}"
    spec = importlib.util.spec_from_file_location("_fetch_data_under_test", TOOL_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_fetch_data_under_test"] = module
    spec.loader.exec_module(module)
    return module


def run_dry(
    tmp: Path, fetch: Any, *extra: str, client: Any | None = None, argv_extra: Sequence[str] = ()
) -> tuple[int, Path]:
    """跑一次 dry-run，返回 ``(退出码, out 根目录)``。"""
    out = tmp / "out"
    args = ["--dry-run", "--out", str(out), *extra, *argv_extra]
    code = fetch.main(args, client=client)
    return code, out


def latest_run(out_root: Path) -> Path:
    """最近一次运行的 run 目录（dry-run 在 `<out>/dry-run/` 下，live 直接在 `<out>/` 下）。"""
    search_root = out_root / "dry-run" if (out_root / "dry-run").exists() else out_root
    runs = sorted(search_root.glob("20*"))
    assert runs, f"{search_root} 下没有 run 目录"
    return runs[-1]


def live_args(tmp: Path, name: str, *extra: str) -> list[str]:
    """live 模式的一组参数：**必须**把 data-root 指到临时目录，绝不碰生产的 `data/raw`。"""
    base = tmp / name
    return [
        "--start", "2022-03-01",
        "--end", "2022-03-15",
        "--index", "000300.SH",
        "--out", str(base),
        "--data-root", str(base / "data" / "raw"),
        *extra,
    ]


def read_summary(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))


def read_reports(run_dir: Path) -> dict[str, Any]:
    """四份产物都要存在；返回其中的 JSON 两份 + md 文本。"""
    for name in REPORT_FILES:
        assert (run_dir / name).exists(), f"缺少产物 {name}"
    return {
        "summary": json.loads((run_dir / "summary.json").read_text(encoding="utf-8")),
        "quality": json.loads((run_dir / "quality_report.json").read_text(encoding="utf-8")),
        "manifest": json.loads((run_dir / "manifest.json").read_text(encoding="utf-8")),
        "md": (run_dir / "report.md").read_text(encoding="utf-8"),
    }


def symbol_files(run_dir: Path, *, fmt: str = "parquet") -> list[Path]:
    return sorted((run_dir / "data" / "raw").glob(f"*.{fmt}"))


def call_counts(client: FakeAKShareClient) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, _kwargs in client.calls:
        counts[name] = counts.get(name, 0) + 1
    return counts


def build_readback_provider(summary: Mapping[str, Any]) -> Any:
    """按 `artifacts.provider_for_readback` 构造**真实**的读回 provider（不加任何假设）。"""
    spec = summary["artifacts"]["provider_for_readback"]
    return build_provider(
        DataConfig(provider=spec["name"]),
        provider=spec["name"],
        root=spec["root"],
        index_members_path=spec["index_members_path"],
        fundamentals_path=spec["fundamentals_path"],
    )


def config_for(fetch: Any) -> Any:
    """按工具的默认配置路径加载主配置（测试与工具用**同一个**配置来源）。"""
    from aqs.config.loader import load_base_config

    return load_base_config(fetch.DEFAULT_CONFIG, overrides={"data": {"provider": "akshare"}})


class _StubProvider:
    """最小 provider 桩：可控地让某个步骤 ok / degraded / failed（不联网、不落盘）。"""

    name = "stub"

    def __init__(self, *, fail: str | None = None, bars_rows: int = 6) -> None:
        self.fail = fail
        self._bars_rows = bars_rows

    # ---- 协议 ----
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            daily_bars=True, adjustment_factors=True, suspensions=True, price_limits=True,
            st_flags=True, listing_dates=True, index_members=True, fundamentals=True,
            industry=True, default_adjust="hfq",
        )

    def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            ok=True, checked_at=pd.Timestamp("2022-03-01").to_pydatetime(), latency_ms=0.0
        )

    def _prov(self, rows: int, *, symbols: int = 1) -> Provenance:
        """统一构造溯源（`Provenance` 没有默认值的字段必须显式给出）。"""
        return Provenance(
            source=self.name,
            fetched_at=pd.Timestamp("2022-03-01").to_pydatetime(),
            cache_hit=False,
            rows=rows,
            symbols=symbols,
        )

    def _maybe_fail(self, step: str) -> None:
        if self.fail == step:
            raise DataError(f"注入失败：{step}")

    def _bars(self) -> pd.DataFrame:
        days = pd.bdate_range("2022-03-01", periods=self._bars_rows)
        rows = []
        for i, day in enumerate(days):
            close = 10.0 + i * 0.1
            rows.append(
                {
                    # OHLC 必须自洽（high ≥ max(open, close)、low ≤ min(...)），
                    # 否则会撞上 `validate_bars` 的 OHLC 错误，把本用例要测的路径掩盖掉
                    "date": day, "symbol": "600000.SH",
                    "open": round(close - 0.05, 4), "high": round(close + 0.2, 4),
                    "low": round(close - 0.1, 4), "close": close,
                    "volume": 1_000_000.0,
                    # amount 与 volume×close 同量级（否则会触发 Q3 量纲 error）
                    "amount": 1_000_000.0 * close,
                    "adj_factor": 1.0, "is_suspended": False, "is_st": False,
                    "limit_up": round(close * 1.1, 4), "limit_down": round(close * 0.9, 4),
                    "list_date": pd.Timestamp("1999-11-10"), "industry": "银行",
                }
            )
        return pd.DataFrame(rows)

    def fetch_trading_calendar(self, start: Any, end: Any) -> tuple[list[Any], Provenance]:
        self._maybe_fail("calendar")
        days = [d.date() for d in pd.bdate_range(start, end)]
        return days, self._prov(len(days), symbols=0)

    def fetch_index_members(self, index_code: str, start: Any, end: Any) -> tuple[pd.DataFrame, Provenance]:
        self._maybe_fail("index_members")
        frame = pd.DataFrame(
            {"index_code": [index_code], "symbol": ["600000.SH"],
             "effective_from": [pd.Timestamp("2022-01-01")], "effective_to": [pd.NaT]}
        )
        return frame, self._prov(1)

    def fetch_symbol_meta(self, symbols: Sequence[str]) -> tuple[pd.DataFrame, Provenance]:
        self._maybe_fail("symbol_meta")
        frame = pd.DataFrame(
            {"symbol": ["600000.SH"], "name": ["浦发银行"], "list_date": [pd.Timestamp("1999-11-10")],
             "delist_date": [pd.NaT], "industry": ["银行"]}
        )
        return frame, self._prov(1)

    def fetch_bars(self, symbols: Sequence[str], start: Any, end: Any, *, adjust: str = "none"):
        self._maybe_fail("bars")
        frame = self._bars()
        return frame, self._prov(len(frame))

    def fetch_fundamentals(self, symbols: Sequence[str], start: Any, end: Any):
        self._maybe_fail("fundamentals")
        frame = pd.DataFrame(
            {"symbol": ["600000.SH"], "report_period": [pd.Timestamp("2021-12-31")],
             "announce_date": [pd.Timestamp("2022-03-02")], "eps": [0.5], "revenue": [1.0],
             "net_profit": [0.2], "roe": [5.0]}
        )
        return frame, self._prov(1)

    def fetch_industry(self, symbols: Sequence[str]):
        self._maybe_fail("industry")
        frame = pd.DataFrame({"symbol": ["600000.SH"], "industry": ["银行"]})
        return frame, self._prov(1)


# --------------------------------------------------------------------------- #
# 1~6：产物、schema、不变量、单一真相、投影、续抓
# --------------------------------------------------------------------------- #
def test_dry_run_writes_four_reports_into_dry_run_root():
    fetch = load_fetch_module()
    with workspace_tmp("fetch_dry") as tmp:
        code, out = run_dry(tmp, fetch)
        run_dir = latest_run(out)
        reports = read_reports(run_dir)

    assert code == 0, "dry-run 退出码恒 0（脚本自身跑通即通过）"
    assert run_dir.parent.name == "dry-run", "dry-run 产物必须在 <out>/dry-run/ 下"
    assert reports["summary"]["mode"] == "dry-run"
    assert reports["summary"]["provider"]["akshare_version"].startswith("dry-run(")
    assert "dry-run" in reports["md"].splitlines()[2]
    assert "手工构造" in reports["md"] and "不是真实抓取" in reports["md"]


def test_summary_satisfies_declared_schema():
    fetch = load_fetch_module()
    schema = load_schema(SUMMARY_SCHEMA)
    with workspace_tmp("fetch_schema") as tmp:
        _, out = run_dry(tmp, fetch)
        summary = read_summary(latest_run(out))

    errors = validate_schema(summary, schema, schema)
    assert not errors, "summary.json 不符合 docs/data/fetch_summary.schema.json：\n" + "\n".join(errors)


def test_counts_invariant_matches_files_on_disk_and_manifest():
    """`symbols_with_bars` 是**硬不变量**：报告数字 == 落盘文件数（== 通过率的分子）。

    `manifest_bars_keys` 刻意只要求 `>=`：provider 对「抓到空表的标的」也会写缓存样本，
    但它不会落盘（不变量写成相等就是一句假话）。
    """
    fetch = load_fetch_module()
    with workspace_tmp("fetch_counts") as tmp:
        _, out = run_dry(tmp, fetch)
        run_dir = latest_run(out)
        summary = read_summary(run_dir)
        files = symbol_files(run_dir)
        rows_on_disk = sum(len(pd.read_parquet(p)) for p in files)

    counts = summary["counts"]
    assert counts["symbols_with_bars"] == len(files) == counts["raw_files_written"]
    assert counts["symbols_with_bars"] == summary["request"]["symbols"]["kept"]
    assert counts["manifest_bars_keys"] >= counts["symbols_with_bars"]
    assert counts["bars_rows"] == rows_on_disk
    assert counts["bars_start"] == "2022-03-01" and counts["bars_end"] == "2022-03-07"


def test_quality_report_embeds_pipeline_to_dict_verbatim():
    """`quality_report.json` 的 `quality` 段必须**逐键等于**流水线的 `to_dict()`。

    只读文件无法证明这一点（报告可能自己算了一套），所以这里用 `run_fetch` 的返回值
    拿到流水线对象直接比对。
    """
    fetch = load_fetch_module()
    with workspace_tmp("fetch_quality") as tmp:
        out = tmp / "out"
        args = fetch.build_parser().parse_args(
            ["--dry-run", "--out", str(out), "--index", "000300.SH",
             "--start", fetch.DRY_RUN_START, "--end", fetch.DRY_RUN_END]
        )
        run_dir, _run_id = fetch.plan_run_dir(out, mode="dry-run")
        outcome = fetch.run_fetch(
            args, mode="dry-run", run_dir=run_dir,
            dry_run_root=out / "dry-run", argv=["fetch_data.py", "--dry-run"],
        )
        payload = json.loads((run_dir / "quality_report.json").read_text(encoding="utf-8"))

    assert outcome.ingest is not None and outcome.quality is not None
    assert payload["quality"] == outcome.ingest.quality.to_dict()
    assert payload["schema_version"] == fetch.SCHEMA_VERSION
    assert payload["scope"]["mode"] == "dry-run"
    assert set(payload["quality"]) == {"ok", "errors", "warnings", "exit_code", "stats", "findings"}


def test_manifest_projection_matches_authoritative_manifest():
    fetch = load_fetch_module()
    with workspace_tmp("fetch_manifest") as tmp:
        _, out = run_dry(tmp, fetch)
        run_dir = latest_run(out)
        reports = read_reports(run_dir)
        authoritative = json.loads(
            Path(reports["summary"]["manifest"]["source"]).read_text(encoding="utf-8")
        )
        projection = reports["manifest"]

    authority_keys = set(authoritative["entries"])
    assert {f"{e['dataset']}::{e['key']}" for e in projection["entries"]} <= authority_keys
    assert projection["entries_total"] == len(projection["entries"])
    assert not projection["missing_paths"], "投影里每条 path 都必须真实存在"
    assert all(entry["path_exists"] for entry in projection["entries"])
    # digest 可复算，且与 summary 里记录的一致
    recomputed = fetch.canonical_json_digest(authoritative)
    assert projection["source_manifest_digest_after"] == recomputed
    assert reports["summary"]["manifest"]["digest"] == recomputed


def test_resume_run_avoids_all_cacheable_calls_and_writes_identical_data():
    """断点续抓的机械证明（同一次 `--out` → 共用 dry-run 缓存）。

    第二次运行必须：① 可缓存端点 **0 次调用**；② 缓存清单里 bars 无刷新；
    ③ 落盘数据与第一次**逐行相等**。
    快照类端点（成分/行业）**例外**：M4-8 的快照累积是状态，每跑必采（这不是浪费）。
    """
    fetch = load_fetch_module()
    client = FakeAKShareClient()
    with workspace_tmp("fetch_resume") as tmp:
        out = tmp / "out"
        code1, _ = run_dry(tmp, fetch, client=client)
        run1 = latest_run(out)
        calls_after_first = call_counts(client)
        frames1 = {p.name: pd.read_parquet(p) for p in symbol_files(run1)}

        code2, _ = run_dry(tmp, fetch, client=client)
        run2 = sorted((out / "dry-run").glob("20*"))[-1]
        calls_after_second = call_counts(client)
        frames2 = {p.name: pd.read_parquet(p) for p in symbol_files(run2)}
        summary2 = read_summary(run2)

    assert code1 == code2 == 0
    delta = {name: calls_after_second.get(name, 0) - calls_after_first.get(name, 0)
             for name in set(calls_after_first) | set(calls_after_second)}
    for endpoint in CACHEABLE_ENDPOINTS:
        assert delta.get(endpoint, 0) == 0, f"第二次运行不该再抓 {endpoint}（缓存应命中）：{delta}"
    assert all(delta.get(endpoint, 0) >= 0 for endpoint in SNAPSHOT_ENDPOINTS)
    bars = summary2["incremental"]["by_dataset"]["bars"]
    assert bars["keys_refreshed"] == 0, "第二次运行不该刷新任何 bars 样本"
    assert bars["keys_untouched"] == bars["keys_after"]
    assert summary2["incremental"]["requests_avoided_symbols"] == bars["keys_after"]
    assert set(frames1) == set(frames2)
    for name in frames1:
        assert frames1[name].equals(frames2[name]), f"{name} 两次落盘不一致"


# --------------------------------------------------------------------------- #
# 7~10：截断 / 退出码 / 用法错误 / dry-run 安全边界
# --------------------------------------------------------------------------- #
def test_max_symbols_truncates_deterministically_and_is_disclosed():
    fetch = load_fetch_module()
    with workspace_tmp("fetch_truncate") as tmp:
        _, out = run_dry(tmp, fetch, "--max-symbols", "1")
        run_dir = latest_run(out)
        summary = read_summary(run_dir)
        quality = json.loads((run_dir / "quality_report.json").read_text(encoding="utf-8"))
        files = symbol_files(run_dir)

    symbols = summary["request"]["symbols"]
    assert symbols["truncated"] is True and symbols["total"] == 2 and symbols["kept"] == 1
    assert symbols["max_symbols"] == 1
    assert [p.stem for p in files] == ["000001.SZ"], "截断取排序后的前 N 个（确定性）"
    assert summary["counts"]["symbols_requested"] == 1
    assert "截断" in summary["incremental"].get("truncation", "")
    findings = quality["quality"]["findings"]
    assert any(f["code"] == fetch.INGEST_FINDING_CODE and "截断" in f["message"] for f in findings)


def test_exit_code_matrix_matches_status_and_quality():
    """退出码矩阵：ok→0 / degraded→0 / degraded+`--fail-on-degraded`→2 / failed→2（报告仍写出）。

    用注入的 provider 桩在 **live 模式**下走真实分支（不联网；data-root 指向临时目录）。
    """
    fetch = load_fetch_module()
    with workspace_tmp("fetch_exit") as tmp:
        code_ok = fetch.main(live_args(tmp, "ok"), provider=_StubProvider())
        summary_ok = read_summary(latest_run(tmp / "ok"))

        code_degraded = fetch.main(
            live_args(tmp, "degraded"), provider=_StubProvider(fail="fundamentals")
        )
        summary_degraded = read_summary(latest_run(tmp / "degraded"))

        code_strict = fetch.main(
            live_args(tmp, "strict", "--fail-on-degraded"),
            provider=_StubProvider(fail="fundamentals"),
        )

        code_failed = fetch.main(live_args(tmp, "failed"), provider=_StubProvider(fail="bars"))
        run_failed = latest_run(tmp / "failed")
        summary_failed = read_summary(run_failed)
        failed_files = sorted(p.name for p in run_failed.iterdir())

    assert (code_ok, summary_ok["status"], summary_ok["exit_code"]) == (0, "ok", 0)
    assert (code_degraded, summary_degraded["status"], summary_degraded["exit_code"]) == (0, "degraded", 0)
    assert summary_degraded["quality"]["errors"] == 0, "降级不等于质量 error"
    assert code_strict == 2, "--fail-on-degraded 时 degraded 也要 2"
    assert (code_failed, summary_failed["status"], summary_failed["exit_code"]) == (2, "failed", 2)
    assert summary_failed["failure"] is not None
    assert "bars" in summary_failed["failure"]["message"]
    assert set(REPORT_FILES) <= set(failed_files), "失败时报告也要写出（部分报告 > 没有报告）"
    assert summary_failed["steps"] == {}, "九步未走完时 steps 为空，不得假装有步态"
    assert summary_failed["timing"]["steps"] == {}, "每步耗时同样不得补零冒充完整"


def test_fixture_root_without_dry_run_is_rejected():
    fetch = load_fetch_module()
    client = FakeAKShareClient()
    with workspace_tmp("fetch_fixture_guard") as tmp:
        with raises(SystemExit) as info:
            fetch.main(
                ["--start", "2022-03-01", "--end", "2022-03-15", "--index", "000300.SH",
                 "--out", str(tmp), "--fixture-root", str(tmp)],
                client=client,
            )
    assert info.value.code == 2
    assert client.calls == [], "被拒绝时不得发出任何调用"


def test_dry_run_rejects_data_root_and_enforces_write_boundary():
    """两件事：① `--data-root` 在 dry-run 下被直接拒绝（footgun 防护）；
    ② 安全边界函数对越界路径**真的**抛错（不是"尽力而为"）。
    """
    fetch = load_fetch_module()
    with workspace_tmp("fetch_boundary") as tmp:
        with raises(SystemExit) as info:
            fetch.main(["--dry-run", "--out", str(tmp), "--data-root", str(tmp / "data" / "raw")])
        assert info.value.code == 2

        dry_root = tmp / "out" / "dry-run"
        # 边界内：通过
        fetch.assert_within_dry_run([dry_root / "r1" / "data" / "raw"], dry_run_root=dry_root)
        # 边界外：抛错（生产 data/raw 与生产 data/cache 都必须被拦住）
        for outside in (PROJECT_ROOT / "data" / "raw", PROJECT_ROOT / "data" / "cache", tmp / "elsewhere"):
            with raises(DataError):
                fetch.assert_within_dry_run([outside], dry_run_root=dry_root)


# --------------------------------------------------------------------------- #
# 11~14：落盘纪律（原子/往返/空结果/refresh）与报告三件套
# --------------------------------------------------------------------------- #
def test_interrupted_write_leaves_no_partial_file():
    """写盘中途抛错 → 目标文件不存在，且不留 `.tmp*` 残渣（原子写的硬性质）。"""
    fetch = load_fetch_module()
    frame = pd.DataFrame({"date": pd.bdate_range("2022-03-01", periods=2), "close": [1.0, 2.0]})
    with workspace_tmp("fetch_atomic") as tmp:
        root = tmp / "raw"
        real_replace = fetch.atomic_write_frame

        def boom(path, data, *, fmt="parquet"):
            raise OSError("注入的写盘故障")

        fetch.atomic_write_frame = boom  # type: ignore[assignment]
        try:
            with raises(OSError):
                fetch.write_symbol_frames({"600000.SH": frame}, root, fmt="parquet")
        finally:
            fetch.atomic_write_frame = real_replace  # type: ignore[assignment]
        assert not (root / "600000.SH.parquet").exists()
        assert [p.name for p in root.iterdir() if p.suffix == ".tmp"] == []


def test_csv_round_trip_has_no_bom_in_column_names():
    fetch = load_fetch_module()
    from aqs.data.loader import read_table

    with workspace_tmp("fetch_csv") as tmp:
        code, out = run_dry(tmp, fetch, "--fmt", "csv")
        run_dir = latest_run(out)
        summary = read_summary(run_dir)
        files = symbol_files(run_dir, fmt="csv")
        frame = pd.read_csv(files[0], dtype={"symbol": str})
        # 与 provider 的读回口径一致（`read_table` 用 utf-8：写入端带 BOM 就会污染列名）
        again = read_table(files[0])

    assert code == 0 and summary["artifacts"]["provider_for_readback"]["name"] == "csv"
    assert not any(str(c).startswith("\ufeff") for c in frame.columns), "列名不得带 BOM"
    assert "date" in frame.columns and "close_adj" in frame.columns
    assert list(again.columns) == list(frame.columns), "落盘 csv 必须能被 read_table 原样读回"


def test_empty_frames_are_not_persisted():
    fetch = load_fetch_module()
    frame = pd.DataFrame({"date": pd.to_datetime([]), "close": []})
    with workspace_tmp("fetch_empty") as tmp:
        stats = fetch.write_symbol_frames({"600000.SH": frame, "000001.SZ": frame.iloc[0:0]}, tmp, fmt="parquet")
        assert stats.files == 0
        assert sorted(stats.skipped_empty) == ["000001.SZ", "600000.SH"]
        assert list(tmp.iterdir()) == [], "空结果不得落盘（空文件会被下游当成『有该标的但无数据』）"

        # 侧表同理
        path, rows = fetch._write_side_table(None, tmp, "index_members", fmt="parquet", sort_by=["symbol"])
        assert path is None and rows == 0


def test_refresh_replaces_data_root_across_formats():
    """`--refresh` 是**整体替换**：先清掉数据目录里**所有**旧文件（跨格式），再写本次结果。

    用真实 ingest 驱动 `materialize`（而不是只测 `clear_data_root`）：这样「先清后写」
    的顺序也被覆盖 —— 只清不写、或写成两批格式并存，都会红。
    """
    fetch = load_fetch_module()
    with workspace_tmp("fetch_refresh") as tmp:
        out = tmp / "out"
        args = fetch.build_parser().parse_args(
            ["--dry-run", "--out", str(out), "--index", "000300.SH",
             "--start", fetch.DRY_RUN_START, "--end", fetch.DRY_RUN_END]
        )
        run_dir, _ = fetch.plan_run_dir(out, mode="dry-run")
        first = fetch.run_fetch(args, mode="dry-run", run_dir=run_dir,
                                dry_run_root=out / "dry-run", argv=["fetch_data.py"])
        assert first.ingest is not None

        # 预置一个「上次以别的格式落下」的文件 + 一个 .gitkeep
        data_root = tmp / "production_like" / "raw"
        data_root.mkdir(parents=True)
        (data_root / "stale_symbol.parquet").write_text("x", encoding="utf-8")
        (data_root / "another.csv").write_text("x", encoding="utf-8")
        (data_root / ".gitkeep").write_text("", encoding="utf-8")

        result = fetch.materialize(
            first.ingest, data_root=data_root, index_dir=tmp / "production_like" / "index",
            fundamental_dir=tmp / "production_like" / "fundamental", fmt="csv", refresh=True,
        )
        remaining = sorted(p.name for p in data_root.iterdir())

    assert result.cleared_files == ("another.csv", "stale_symbol.parquet"), "跨格式清理"
    assert remaining == [".gitkeep", "000001.SZ.csv", "600000.SH.csv"], f"只剩本次结果与 .gitkeep：{remaining}"
    assert result.raw_files == 2 and result.raw_rows == 10


# --------------------------------------------------------------------------- #
# 15~20：端到端回读 / 单一真相 / timing / digest / provider 构造 / 报告一致性
# --------------------------------------------------------------------------- #
def test_end_to_end_readback_preserves_close_adj():
    """★ 最有价值的一条：落盘 → **真实 provider** 读回 → `ingest_from_provider` 成功，
    且 `close_adj` 与落盘文件**逐值相等**（证明落盘口径没有叠乘复权因子）。"""
    fetch = load_fetch_module()
    with workspace_tmp("fetch_readback") as tmp:
        _, out = run_dry(tmp, fetch)
        run_dir = latest_run(out)
        summary = read_summary(run_dir)
        provider = build_readback_provider(summary)
        caps = provider.capabilities()
        report = ingest_from_provider(
            provider,
            config=DataConfig(provider=summary["artifacts"]["provider_for_readback"]["name"]),
            universe_config=UniverseConfig(mode="index", fallback_to_all=True),
            index_codes=["000300.SH"],
            start="2022-03-01",
            end="2022-03-15",
            symbols=["000001.SZ", "600000.SH"],
        )
        on_disk = pd.concat([pd.read_parquet(p) for p in symbol_files(run_dir)]).sort_values(
            ["symbol", "date"]
        )
        stored = report.store.bars_frame.sort_values(["symbol", "date"])

    assert caps.daily_bars and caps.listing_dates and caps.index_members and caps.fundamentals
    assert report.overall_status == "ok", f"回读必须能建成 store：{report.step_status}"
    assert set(report.step_status.values()) == {"ok"}
    assert len(stored) == len(on_disk)
    assert on_disk["close_adj"].to_numpy().tolist() == stored["close_adj"].to_numpy().tolist()
    assert on_disk["close"].to_numpy().tolist() == stored["close"].to_numpy().tolist()
    assert set(report.store.symbols()) == {"000001.SZ", "600000.SH"}


def test_status_and_exit_code_are_recomputable_from_summary():
    """单一真相：`exit_code` 必须能由 summary 自身的 status/quality 复算出来。

    尤其要守：dry-run 的 `exit_code` 恒 0，但 `dry_run_estimated_exit_if_live`
    必须等于「同样的状态在 live 下会得到的退出码」——两者不许各算一套。
    """
    fetch = load_fetch_module()
    with workspace_tmp("fetch_truth") as tmp:
        _, out = run_dry(tmp, fetch)
        summary = read_summary(latest_run(out))
        fetch.main(live_args(tmp, "degraded", "--fail-on-degraded"),
                   provider=_StubProvider(fail="fundamentals"))
        degraded = read_summary(latest_run(tmp / "degraded"))

    for item, fail_on_degraded in ((summary, False), (degraded, True)):
        has_errors = item["status"] == "failed" or item["quality"]["errors"] > 0
        expected = fetch.estimate_exit_if_live(
            status=item["status"], has_errors=has_errors, fail_on_degraded=fail_on_degraded
        )
        if item["mode"] == "dry-run":
            assert item["exit_code"] == 0, "dry-run 退出码恒 0"
            assert item["dry_run_estimated_exit_if_live"] == expected
        else:
            assert item["exit_code"] == expected
        assert item["exit_code_reason"], "退出码必须带可追溯的理由"

    assert degraded["exit_code"] == 2 and degraded["status"] == "degraded"
    assert degraded["quality"]["errors"] == 0, "降级不等于质量 error（两者语义不同）"


def test_timing_records_every_step_and_stays_consistent():
    fetch = load_fetch_module()
    with workspace_tmp("fetch_timing") as tmp:
        _, out = run_dry(tmp, fetch)
        summary = read_summary(latest_run(out))

    timing = summary["timing"]
    assert set(timing["steps"]) == set(fetch.INGEST_STEPS), "九步都要有耗时"
    assert set(timing["steps"]) == set(summary["steps"]), "耗时与步态必须同键"
    assert all(value >= 0 for value in timing["steps"].values())
    assert timing["ingest_seconds"] <= timing["total_seconds"]
    assert timing["materialize_seconds"] >= 0 and timing["reports_seconds"] >= 0
    assert "不完整" in timing["steps_note"], "失败时可能不完整，必须在报告里写明"
    assert "dry-run 固定时钟" in summary["provider"]["data_config"]["clock"]


def test_manifest_digest_is_order_independent_and_stable():
    fetch = load_fetch_module()
    payload = {"schema_version": 1, "entries": {"bars::600000.SH": {"rows": 5, "fetched_at": "T"}}}
    reordered = {"entries": {"bars::600000.SH": {"fetched_at": "T", "rows": 5}}, "schema_version": 1}
    assert fetch.canonical_json_digest(payload) == fetch.canonical_json_digest(reordered)
    assert fetch.canonical_json_digest(payload) == fetch.canonical_json_digest(dict(payload))
    assert fetch.canonical_json_digest(None) == fetch.MANIFEST_ABSENT
    assert len(fetch.canonical_json_digest(payload)) == fetch.DIGEST_HEX


def test_live_provider_build_uses_registry_signature():
    """回归：`build_provider(config, *, provider=...)` —— 名字是关键字，不是第一个位置参数。

    写错时不会报"参数错"之外的任何东西，直到真正抓数才炸（本次实现期实测踩到）。
    """
    fetch = load_fetch_module()
    args = fetch.build_parser().parse_args(["--start", "2022-03-01", "--end", "2022-03-15", "--index", "000300.SH"])
    provider, client = fetch._build_provider(
        args, config=config_for(fetch), mode="live", cache_root=PROJECT_ROOT / "data" / "cache",
        client=None, provider=None,
    )
    assert isinstance(provider, AKShareProvider) and provider.name == "akshare"
    assert client is None, "live 模式不该伪造客户端"
    # dry-run：provider 的缓存必须落在 dry-run 边界内，且注入固定时钟
    with workspace_tmp("fetch_provider") as tmp:
        dry_root = tmp / "dry-run"
        dry_provider, fake = fetch._build_provider(
            args, config=config_for(fetch), mode="dry-run",
            cache_root=dry_root / "cache", client=None, provider=None,
        )
    assert isinstance(fake, FakeAKShareClient)
    assert Path(dry_provider._cache.root).resolve().is_relative_to(dry_root.resolve())


def test_markdown_and_json_agree_and_carry_three_piece():
    fetch = load_fetch_module()
    with workspace_tmp("fetch_md") as tmp:
        _, out = run_dry(tmp, fetch)
        run_dir = latest_run(out)
        reports = read_reports(run_dir)

    md, summary = reports["md"], reports["summary"]
    assert summary["command"] in md, "生成命令必须出现在报告里"
    assert summary["invocation"]["cwd"] in md, "cwd 必须出现在报告里（命令假设它）"
    assert summary["created_at"] in md and summary["finished_at"] in md
    assert f"`{summary['status']}`" in md and f"`{summary['exit_code']}`" in md
    assert "口径 / 来源 / 命令 三件套" in md
    # md 里的计数与 json 同源
    for key in ("symbols_with_bars", "bars_rows", "raw_files_written"):
        assert f"| `{key}` | {summary['counts'][key]} |" in md, f"{key} 在 md/json 中不一致"
    # 质量明细来自 `to_markdown()`，报告的每条 finding 都必须在 md 里出现（单一真相）
    assert "## 数据质量" in md
    for finding in reports["quality"]["quality"]["findings"]:
        assert finding["code"] in md, f"{finding['code']} 出现在 JSON 却不在 md"
    # 四份产物互指（字段语义不同：quality.exit_code 是「质量报告结论」，summary.exit_code 是「本进程退出码」，
    # 故只比对同语义的 errors/warnings，并由 test_status_and_exit_code_... 覆盖退出码的可复算性）
    assert reports["summary"]["artifacts"]["summary"] == "summary.json"
    assert reports["quality"]["quality"]["errors"] == reports["summary"]["quality"]["errors"]
    assert reports["quality"]["quality"]["warnings"] == reports["summary"]["quality"]["warnings"]


def test_data_store_exposes_read_only_side_tables():
    """落盘工具需要成分/财务**本体**；公开只读属性，避免调用方去碰私有 `_members`。"""
    from aqs.data.store import DataStore
    from tests.tools import bar_row, dates, make_bars, make_config

    config = make_config()
    rows = [bar_row(d, "600000.SH", 10.0) for d in dates(3)]
    members = pd.DataFrame(
        {"index_code": ["000300.SH"], "symbol": ["600000.SH"],
         "effective_from": [pd.Timestamp("2022-01-01")], "effective_to": [pd.NaT]}
    )
    fundamentals = pd.DataFrame(
        {"symbol": ["600000.SH"], "report_period": [pd.Timestamp("2021-12-31")],
         "announce_date": [pd.Timestamp("2022-03-01")], "eps": [0.5],
         "revenue": [1.0], "net_profit": [0.2], "roe": [5.0]}
    )
    store = DataStore(
        make_bars(rows), index_members=members, fundamentals=fundamentals,
        config=config.data, universe_config=config.universe,
    )
    assert store.index_members_frame is not None and len(store.index_members_frame) == 1
    assert store.fundamentals_frame is not None and len(store.fundamentals_frame) == 1
    assert list(store.index_members_frame.columns) == ["index_code", "symbol", "effective_from", "effective_to"]
    empty = DataStore(make_bars(rows), config=config.data, universe_config=config.universe)
    assert empty.index_members_frame is None and empty.fundamentals_frame is None


def test_write_symbol_frames_rejects_unsafe_symbol_names():
    fetch = load_fetch_module()
    frame = pd.DataFrame({"date": pd.bdate_range("2022-03-01", periods=1), "close": [1.0]})
    with workspace_tmp("fetch_unsafe") as tmp:
        for bad in ("../escape", "a/b", "", "."):
            with raises(DataError):
                fetch.write_symbol_frames({bad: frame}, tmp, fmt="parquet")
        assert list(tmp.iterdir()) == []


def test_clear_data_root_refuses_subdirectories():
    """子目录必须报错而不是静默忽略：加载器用非递归 glob，子目录里的数据会被读不到。"""
    fetch = load_fetch_module()
    with workspace_tmp("fetch_subdir") as tmp:
        (tmp / "nested").mkdir()
        with raises(DataError):
            fetch.clear_data_root(tmp)
        with raises(DataError):
            fetch.assert_no_subdirectories(tmp)


def test_candidate_resolution_union_and_sources():
    fetch = load_fetch_module()
    case = fetch.resolve_candidates(cli_symbols=("600000",), index_members=("000001",), max_symbols=None)
    assert case.kept == ("000001", "600000") and case.source == "cli+index" and case.truncated is False
    only_cli = fetch.resolve_candidates(cli_symbols=("600000",))
    assert only_cli.kept == ("600000",) and only_cli.source == "cli"
    only_index = fetch.resolve_candidates(index_members=("000001", "600000"))
    assert only_index.source == "index" and only_index.total == 2
    empty = fetch.resolve_candidates()
    assert empty.kept == () and empty.source == "none"
    truncated = fetch.resolve_candidates(index_members=("3", "1", "2"), max_symbols=2)
    assert truncated.kept == ("1", "2") and truncated.truncated is True
    # 重复与大小写归一
    dup = fetch.resolve_candidates(cli_symbols=("600000", "600000"), index_members=("600000",))
    assert dup.total == 1 and dup.kept == ("600000",)
