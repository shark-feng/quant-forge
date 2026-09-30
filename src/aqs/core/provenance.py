"""运行溯源（缺陷 D1）：让每一个落盘数字都能追溯到「哪条命令、哪个版本、什么口径」。

背景：`docs/03_acceptance_report.md` §9 曾出现「表内数字来自 30 只标的的运行，但落盘的
是一次 12 只标的的运行」这种不可追溯情况。仅靠人工纪律无法杜绝，因此把溯源信息
**写进产物本身**（`reports/<run>/summary.json` 的 `diagnostics.invocation`）。

设计约束：

- **不使用 ``subprocess``**：受限沙箱下无法用管道捕获子进程输出；Git 信息直接读 ``.git`` 文件；
- 任何一项探测失败都退化为 ``None``，**绝不因为溯源失败而让回测失败**。
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = ["GitInfo", "read_git_info", "build_invocation", "describe_invocation"]

_GITDIR_FILE = ".git"


@dataclass(slots=True)
class GitInfo:
    """Git 版本信息（探测失败时各字段为 ``None``）。"""

    commit: str | None = None
    short: str | None = None
    branch: str | None = None
    dirty: bool | None = None
    """是否有未提交改动。``None`` 表示无法判断（无 Git 或读取失败）。"""

    def as_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "short": self.short,
            "branch": self.branch,
            "dirty": self.dirty,
        }


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _resolve_git_dir(root: Path) -> Path | None:
    """定位 .git 目录；兼容「.git 是文件（worktree/submodule）」的情形。"""
    candidate = root / _GITDIR_FILE
    if candidate.is_dir():
        return candidate
    if candidate.is_file():
        content = _read_text(candidate)
        if content and content.lower().startswith("gitdir:"):
            target = content.split(":", 1)[1].strip()
            path = Path(target)
            return path if path.is_absolute() else (root / path).resolve()
    return None


def _read_ref(git_dir: Path, ref: str) -> str | None:
    """读 ref 指向的 commit；先查 ``refs/...`` 松散文件，再查 ``packed-refs``。"""
    loose = _read_text(git_dir / ref)
    if loose and len(loose) >= 7:
        return loose
    packed = _read_text(git_dir / "packed-refs")
    if packed:
        for line in packed.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    return None


def read_git_info(root: str | os.PathLike[str] | None = None) -> GitInfo:
    """读取 Git 提交信息（只读文件，不启动子进程）。"""
    base = Path(root) if root is not None else Path.cwd()
    git_dir = _resolve_git_dir(base)
    if git_dir is None:
        return GitInfo()

    head = _read_text(git_dir / "HEAD")
    branch: str | None = None
    commit: str | None = None
    if head:
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            branch = ref.rsplit("/", 1)[-1] if ref.startswith("refs/heads/") else ref
            commit = _read_ref(git_dir, ref)
        else:
            commit = head  # detached HEAD：HEAD 里直接是 commit
    if commit is None:
        return GitInfo(branch=branch)

    # 工作区是否干净：存在 index 且 index mtime 晚于 HEAD 时只作「未知」处理，
    # 这里不解析 index 格式，仅以 index 是否存在作为保守提示。
    dirty: bool | None = None
    index = git_dir / "index"
    if index.exists():
        dirty = None

    return GitInfo(
        commit=commit,
        short=commit[:7] if commit else None,
        branch=branch,
        dirty=dirty,
    )


def build_invocation(
    argv: Sequence[str] | None = None,
    *,
    project_root: str | os.PathLike[str] | None = None,
    extras: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造可写入 `summary.json` 的溯源信息。

    Args:
        argv: 命令行参数（通常传 ``sys.argv``）；``None`` 时使用 ``sys.argv``。
        project_root: 项目根目录（用于读取 Git 信息）。
        extras: 额外键值（例如策略名、种子），会被并入结果。
    """
    args = list(sys.argv if argv is None else argv)
    python_hash_seed = os.environ.get("PYTHONHASHSEED")
    payload: dict[str, Any] = {
        "argv": args,
        "command": " ".join(args),
        "project_root": str(project_root) if project_root is not None else None,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cwd": os.getcwd(),
        "pythonhashseed": python_hash_seed,
        "pythonhashseed_fixed": python_hash_seed is not None,
        "git": read_git_info(project_root).as_dict(),
    }
    if extras:
        payload.update(dict(extras))
    return payload


def describe_invocation(payload: Mapping[str, Any]) -> str:
    """把溯源信息压成一行人类可读文本（用于打印与报告头部）。"""
    git = payload.get("git") or {}
    commit = git.get("short") or "unknown"
    seed = payload.get("pythonhashseed")
    hash_part = f"PYTHONHASHSEED={seed}" if seed else "PYTHONHASHSEED=<unset>"
    return f"command: {payload.get('command', '')} | git: {commit} | {hash_part}"
