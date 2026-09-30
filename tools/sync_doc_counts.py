"""把文档里的用例数 / 模块数同步为**实际值**（M4 起的维护工具）。

为什么需要它
------------
`tests/test_defect_11_docs_consistency.py` 会把文档数字与代码实际收集到的用例数比对，
数字一旦漂移就红灯。但每加一批测试都要手工改 6 个文件的 11 处数字 ——
既繁琐又容易漏（本项目已因此红灯多次）。本工具把这件事变成一条命令。

安全设计
--------
1. **只做定点替换**：每条规则是一个锚定正则，只改数字，不动其它文字；
2. **不允许静默失效**：某条规则匹配 0 次 → 报错退出（文档改版后正则失配不会假装成功）；
3. **默认 dry-run**：不加 `--write` 只打印将要做的改动；
4. **显式 UTF-8**：读写都指定 encoding（本项目被编码问题坑过四次）；
5. 运行后用 `git diff` 复核，并跑 `run_tests.py` 确认守护转绿。

用法::

    python tools\\sync_doc_counts.py            # 预览
    python tools\\sync_doc_counts.py --write    # 实际写入
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import re
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


# --------------------------------------------------------------------------- #
# 1. 统计真实用例数（与 tests/run_tests.py 的收集规则保持一致）
# --------------------------------------------------------------------------- #
def collect_counts() -> tuple[int, int, dict[str, int]]:
    """返回 ``(用例总数, 模块数, {模块文件名: 用例数})``。"""
    per_module: dict[str, int] = {}
    modules = 0
    for path in sorted(TESTS.glob("test_*.py")):
        modules += 1
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:  # pragma: no cover - 理论不可达
            per_module[path.name] = 0
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[path.stem] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001
            print(f"  !! 模块导入失败（用例数记为 0）：{path.name}: {exc}")
            per_module[path.name] = 0
            continue
        count = 0
        for name, obj in vars(module).items():
            if name.startswith("test_") and inspect.isfunction(obj):
                count += 1
            elif name.startswith("Test") and inspect.isclass(obj):
                count += sum(
                    1
                    for m_name, m in vars(obj).items()
                    if m_name.startswith("test_") and inspect.isfunction(m)
                )
        per_module[path.name] = count
    return sum(per_module.values()), modules, per_module


# --------------------------------------------------------------------------- #
# 2. 替换规则：锚定正则 → 整段重建（比 group 回填更不易错）
# --------------------------------------------------------------------------- #
class Rule:
    """一条定点替换规则。

    ``renderer`` 为 ``None`` 时用 :func:`_render` 的固定模板；
    需要**保留匹配文本中的部分内容**（如只换数字、保留阶段描述）时传入自定义 renderer。
    """

    def __init__(
        self,
        path: str,
        pattern: str,
        label: str,
        renderer: Callable[[re.Match[str], int, int], str] | None = None,
    ) -> None:
        self.path = ROOT / path
        self.regex = re.compile(pattern)
        self.label = label
        self.renderer = renderer

    def render(self, m: re.Match[str], cases: int, modules: int) -> str:
        if self.renderer is not None:
            return self.renderer(m, cases, modules)
        return _render(self.label, cases, modules)


def _render(label: str, cases: int, modules: int) -> str:
    table = {
        "README 概要行": f"**{cases} 个单元测试用例全部通过**（{modules} 个测试模块）",
        "README 目录树": f"tests/              单元测试（{cases} 个用例）",
        "docs/00 合计行": f"合计 {cases} 个单元测试用例",
        "docs/03 用例总数": f"| 用例总数 | **{cases}** |",
        "docs/03 通过": f"| 通过 | **{cases}** |",
        "docs/03 模块数": f"| 测试模块 | {modules} 个 |",
        "docs/03 §11 测试行": (
            f"**{cases} 个用例全部通过**（{modules} 个模块；第一轮 398 → 本轮 {cases}）"
        ),
        "DEVELOPMENT 看板": f"当前测试状态**：{cases} 个用例全绿（{modules} 个测试模块）",
        "CHANGELOG 用例数变化": f"用例数 **398 → {cases}**",
        "CHANGELOG 通过行": f"通过 {cases} / 失败 0（{modules} 个测试模块）",
        "docs/17 §8.5 最终状态": f"**最终状态：{cases} 个用例全部通过（{modules} 个测试模块）。**",
    }
    return table[label]


def build_rules() -> list[Rule]:
    def _snapshot_row(m: re.Match[str], cases: int, modules: int) -> str:
        return f"{m.group(1)}{cases}{m.group(2)}{modules}{m.group(3)}"

    def _latest_ref(m: re.Match[str], cases: int, modules: int) -> str:
        return f"最新值见 §8.5：{cases} 用例 / {modules} 模块"

    return [
        Rule("README.md", r"\*\*\d+ 个单元测试用例全部通过\*\*（\d+ 个测试模块）", "README 概要行"),
        Rule("README.md", r"tests/ {14}单元测试（\d+ 个用例）", "README 目录树"),
        Rule("docs/00_system_design.md", r"合计 \d+ 个单元测试用例", "docs/00 合计行"),
        Rule("docs/03_acceptance_report.md", r"\|\s*用例总数\s*\|\s*\*\*\d+\*\*\s*\|", "docs/03 用例总数"),
        Rule("docs/03_acceptance_report.md", r"\|\s*通过\s*\|\s*\*\*\d+\*\*\s*\|", "docs/03 通过"),
        Rule("docs/03_acceptance_report.md", r"\|\s*测试模块\s*\|\s*\d+\s*个\s*\|", "docs/03 模块数"),
        Rule(
            "docs/03_acceptance_report.md",
            r"\*\*\d+ 个用例全部通过\*\*（\d+ 个模块；第一轮 398 → 本轮 \d+）",
            "docs/03 §11 测试行",
        ),
        Rule("docs/DEVELOPMENT.md", r"当前测试状态\*\*：\d+ 个用例全绿（\d+ 个测试模块）", "DEVELOPMENT 看板"),
        Rule("CHANGELOG.md", r"用例数 \*\*398 → \d+\*\*", "CHANGELOG 用例数变化"),
        Rule("CHANGELOG.md", r"通过 \d+ / 失败 0（\d+ 个测试模块）", "CHANGELOG 通过行"),
        Rule(
            "CHANGELOG.md",
            r"0\.1\.0 轮次新增 \d+ 条",
            "CHANGELOG 新增条数",
            renderer=lambda m, c, mm: f"0.1.0 轮次新增 {c - 398} 条",
        ),
        Rule(
            "docs/17_round3_diagnostics.md",
            r"\*\*最终状态：\d+ 个用例全部通过（\d+ 个测试模块）。\*\*",
            "docs/17 §8.5 最终状态",
        ),
        Rule(
            "docs/17_round3_diagnostics.md",
            r"(\| \*\*§8\.5 / 本处\*\* \| [^|]*\| \*\*)\d+(\*\* \| \*\*)\d+(\*\* \|)",
            "docs/17 §8.5 快照表行",
            renderer=_snapshot_row,
        ),
        Rule(
            "docs/17_round3_diagnostics.md",
            r"最新值见 §8\.5：\d+ 用例 / \d+ 模块",
            "docs/17 最新值标注",
            renderer=_latest_ref,
        ),
    ]


MODULE_ROW = re.compile(r"\|\s*`(test_[a-z0-9_]+\.py)`\s*\|\s*\d+\s*\|")


def _sync_module_rows(text: str, per_module: dict[str, int]) -> tuple[str, int]:
    """docs/03 分模块表：把每个 `test_x.py` 行的用例数改为实际值。"""
    out: list[str] = []
    pos = 0
    n = 0
    for m in MODULE_ROW.finditer(text):
        name = m.group(1)
        if name not in per_module:
            continue
        out.append(text[pos: m.start()])
        out.append(f"| `{name}` | {per_module[name]} |")
        pos = m.end()
        n += 1
    out.append(text[pos:])
    return "".join(out), n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同步文档中的用例数 / 模块数")
    parser.add_argument("--write", action="store_true", help="实际写入（默认只预览）")
    args = parser.parse_args(argv)

    cases, modules, per_module = collect_counts()
    print(f"实际收集：{cases} 个用例 / {modules} 个模块")

    by_file: dict[Path, list[Rule]] = {}
    for rule in build_rules():
        by_file.setdefault(rule.path, []).append(rule)

    changed = 0
    problems: list[str] = []
    for path, file_rules in by_file.items():
        text = path.read_text(encoding="utf-8")
        original = text
        for rule in file_rules:
            matches = list(rule.regex.finditer(text))
            if not matches:
                problems.append(f"{path.relative_to(ROOT)}: 规则「{rule.label}」未匹配到任何内容")
                continue
            # 逐个匹配渲染并倒序替换，避免位移影响（也让自定义 renderer 能用到各自的捕获组）
            for m in reversed(matches):
                text = text[: m.start()] + rule.render(m, cases, modules) + text[m.end():]
            print(f"  {path.name}: {rule.label} → {len(matches)} 处")

        if path.name == "03_acceptance_report.md":
            text, n = _sync_module_rows(text, per_module)
            if n:
                print(f"  {path.name}: 分模块表 → {n} 行")

        if text != original:
            changed += 1
            if args.write:
                # newline="\n" 是必须的：默认 newline=None 会把 \n 翻译成 os.linesep，
                # 在 Windows 上把文档写成 CRLF，与 .gitattributes 的 eol=lf 冲突
                # （git 会警告 "CRLF will be replaced by LF"）。
                with path.open("w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
                print(f"  已写入 {path.relative_to(ROOT)}")
            else:
                print(f"  [dry-run] 将修改 {path.relative_to(ROOT)}")

    if problems:
        print("\n!! 有规则未匹配（文档可能已改版，需要同步更新正则）：")
        for p in problems:
            print(f"   - {p}")
        return 2

    if args.write:
        print(f"\n完成：已写入 {changed} 个文件。请复核 git diff 并运行 tests\\run_tests.py")
    else:
        print(f"\n预览完成：将修改 {changed} 个文件。加 --write 实际写入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
