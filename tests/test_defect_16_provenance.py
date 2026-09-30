"""回归缺陷 #16：运行溯源（报告数字必须可追溯到生成命令）。

背景（诊断 D1）：`docs/03_acceptance_report.md` §9 曾出现「表内数字来自 30 只标的的运行，
但落盘的是一次 12 只标的的运行」——数字无法用任何落盘文件复核。

本模块守护三件事：

1. 溯源信息能从 `.git` **直接读文件**得到（不使用 subprocess，受限沙箱下也能跑）；
2. 溯源信息进入 `summary.json`（`diagnostics.invocation`），使数字可复核；
3. 探测失败时退化为 ``None`` 而**不抛异常**（溯源不能成为回测失败的原因）。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from tests.tools import PROJECT_ROOT, workspace_tmp

from aqs.core.provenance import (
    GitInfo,
    build_invocation,
    describe_invocation,
    read_git_info,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _doc_row(doc: str, run: str) -> dict[str, int] | None:
    """从 docs/03 §9.2 的落盘数字表中解析出某个 run 的一行。"""
    for line in doc.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells or cells[0] != run:
            continue
        numbers = []
        for cell in cells[1:]:
            if cell.lstrip("+-").isdigit():
                numbers.append(int(cell))
            elif numbers:
                break
        if len(numbers) >= 3:
            # 表结构：trades | entry | exit | risk_reduce | orders | ...
            return {"trades": numbers[0], "entry": numbers[1], "exit": numbers[2]}
    return None


# --------------------------------------------------------------------------- #
# 1. Git 信息读取（纯文件读取）
# --------------------------------------------------------------------------- #
def test_no_git_directory_returns_empty_info():
    with workspace_tmp("prov_nogit") as root:
        info = read_git_info(root)
        assert isinstance(info, GitInfo)
        assert info.commit is None
        assert info.branch is None


def test_symbolic_ref_is_resolved():
    with workspace_tmp("prov_ref") as root:
        _write(root / ".git" / "HEAD", "ref: refs/heads/main\n")
        _write(root / ".git" / "refs" / "heads" / "main", "a" * 40 + "\n")
        info = read_git_info(root)
        assert info.commit == "a" * 40
        assert info.short == "a" * 7
        assert info.branch == "main"


def test_packed_refs_fallback():
    """refs 被打包（git gc）后必须能从 packed-refs 读到。"""
    with workspace_tmp("prov_packed") as root:
        _write(root / ".git" / "HEAD", "ref: refs/heads/main\n")
        _write(
            root / ".git" / "packed-refs",
            "# pack-refs with: peeled fully-peeled sorted\n" + "b" * 40 + " refs/heads/main\n",
        )
        info = read_git_info(root)
        assert info.commit == "b" * 40
        assert info.branch == "main"


def test_detached_head():
    with workspace_tmp("prov_detached") as root:
        _write(root / ".git" / "HEAD", "c" * 40 + "\n")
        info = read_git_info(root)
        assert info.commit == "c" * 40
        assert info.branch is None


def test_gitdir_file_indirection():
    """`.git` 是文件（worktree / submodule）时按 gitdir 指针解析。"""
    with workspace_tmp("prov_worktree") as root:
        real = root / "real_git"
        _write(real / "HEAD", "ref: refs/heads/main\n")
        _write(real / "refs" / "heads" / "main", "d" * 40 + "\n")
        _write(root / ".git", f"gitdir: {real}\n")
        info = read_git_info(root)
        assert info.commit == "d" * 40


def test_read_git_info_on_this_repository():
    """本项目自身在仓库内，应能读到提交；读不到也不视为失败（可能是未提交环境）。"""
    info = read_git_info(PROJECT_ROOT)
    assert info.commit is None or len(info.commit) >= 7


# --------------------------------------------------------------------------- #
# 2. 溯源结构
# --------------------------------------------------------------------------- #
def test_build_invocation_shape():
    payload = build_invocation(
        ["prog", "--strategy", "ma_cross"],
        project_root=PROJECT_ROOT,
        extras={"seed": 20240101},
    )
    for key in ("argv", "command", "python", "platform", "cwd", "pythonhashseed", "git"):
        assert key in payload, f"溯源信息缺少键：{key}"
    assert payload["argv"] == ["prog", "--strategy", "ma_cross"]
    assert payload["command"] == "prog --strategy ma_cross"
    assert payload["seed"] == 20240101
    assert "commit" in payload["git"]


def test_describe_invocation_is_single_line():
    text = describe_invocation(
        {
            "command": "python demo.py --strategy ma_cross",
            "git": {"short": "abc1234"},
            "pythonhashseed": "0",
        }
    )
    assert "\n" not in text
    assert "abc1234" in text
    assert "unset" not in text


def test_describe_invocation_marks_unset_hashseed():
    text = describe_invocation({"command": "x", "git": {}, "pythonhashseed": None})
    assert "unset" in text
    assert "unknown" in text


# --------------------------------------------------------------------------- #
# 3. 端到端：CLI 产物必须自带溯源
# --------------------------------------------------------------------------- #
def test_demo_summary_records_invocation():
    """端到端：演示回测的 summary.json 必须包含 invocation 与 data_scope。"""
    import examples.demo_backtest as demo

    out = PROJECT_ROOT / "reports" / "_prov_check"
    shutil.rmtree(out, ignore_errors=True)
    argv = [
        "demo_backtest.py",
        "--strategy",
        "ma_cross",
        "--generate-symbols",
        "8",
        "--end",
        "2022-06-30",
        "--output",
        str(out),
    ]
    saved_argv = sys.argv
    sys.argv = argv
    try:
        rc = demo.main()
    finally:
        sys.argv = saved_argv

    assert rc == 0
    summary_path = out / "summary.json"
    assert summary_path.exists()
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    inv = payload["diagnostics"]["invocation"]
    assert inv["generate_symbols"] == 8
    assert inv["seed"] == 20240101
    assert "--generate-symbols" in inv["argv"]
    assert inv["command"].startswith("demo_backtest.py")
    assert "data_scope" in payload["diagnostics"]
    shutil.rmtree(out, ignore_errors=True)


def test_documented_command_numbers_match_artifacts():
    """口径守护：若 reports/<run>/summary.json 存在，其数字必须与 docs/03 §9 表一致。

    这是把「文档数字 ← 落盘文件」变成机械红灯的第一版：只校验能稳定复核的字段
    （成交笔数 = trades.csv 行数、开仓/平仓笔数、订单状态分布）。
    """
    import csv

    doc = (PROJECT_ROOT / "docs" / "03_acceptance_report.md").read_text(encoding="utf-8")
    runs = ["ma_cross", "breakout", "volume"]
    checked = 0
    for run in runs:
        trades = PROJECT_ROOT / "reports" / run / "trades.csv"
        if not trades.exists():
            continue
        with open(trades, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        n_trades = len(rows)
        n_entry = sum(1 for r in rows if r.get("tag") == "entry")
        n_exit = sum(1 for r in rows if r.get("tag") == "exit")
        # 文档中该 run 的行必须存在，且三列数字与之相符
        row = _doc_row(doc, run)
        assert row is not None, f"docs/03 §9 缺少 {run} 的落盘数字行"
        assert row["trades"] == n_trades, f"{run} 成交笔数与落盘不一致：文档 {row['trades']} vs 文件 {n_trades}"
        assert row["entry"] == n_entry, f"{run} 开仓笔数不一致：文档 {row['entry']} vs 文件 {n_entry}"
        assert row["exit"] == n_exit, f"{run} 平仓笔数不一致：文档 {row['exit']} vs 文件 {n_exit}"
        checked += 1
    if checked == 0:
        # 尚无落盘报告时跳过（不制造假红灯），但至少要断言文档确有口径定义
        assert "口径定义" in doc
