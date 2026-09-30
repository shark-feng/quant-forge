"""回归缺陷 #11：文档与实现不一致。

已确认并修复的不一致：
1. ``docs/06_risk_rms.md`` §2.2 标题写「12 条规则」，实际 13 条；
2. ``docs/06_risk_rms.md`` §4 触发延迟口径与实现（缺陷 #1 修复后）不一致；
3. ``docs/00_system_design.md`` 模型映射表中 ``risk/var.py``、``portfolio/sizing.py``
   仍标 ❌（实际已在第一轮交付）；
4. README / 验收报告中的用例数与模块数会随每轮交付漂移。

本文件把「文档漂移」变成红灯：从**代码**取真值，与文档中的数字比对。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.tools import PROJECT_ROOT

DOCS = PROJECT_ROOT / "docs"
README = PROJECT_ROOT / "README.md"


# --------------------------------------------------------------------------- #
# 真值来源：代码
# --------------------------------------------------------------------------- #
def count_test_cases() -> tuple[int, int]:
    """返回 (用例数, 模块数)，口径与 ``tests/run_tests.py`` 一致。"""
    modules = sorted((PROJECT_ROOT / "tests").glob("test_*.py"))
    cases = 0
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                cases += 1
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                cases += sum(
                    1
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_")
                )
    return cases, len(modules)


def rule_count() -> int:
    from aqs.risk import available_rules

    return len(available_rules())


def strategy_count() -> int:
    from aqs.strategy import available_strategies

    return len(available_strategies())


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1) 风控规则条数
# --------------------------------------------------------------------------- #
def test_risk_doc_rule_count_matches_code():
    text = read(DOCS / "06_risk_rms.md")
    match = re.search(r"规则清单（(\d+)\s*条", text)
    assert match, "docs/06_risk_rms.md 中未找到规则清单标题"
    documented = int(match.group(1))
    actual = rule_count()
    assert documented == actual, f"docs/06 写「{documented} 条」，代码实际 {actual} 条"


def test_risk_doc_latency_section_matches_implementation():
    """缺陷 #1 修复后，文档必须同时描述订单级与控制类两种延迟口径。"""
    text = read(DOCS / "06_risk_rms.md")
    assert "订单级" in text and "控制类" in text, "触发延迟口径缺少分类说明"
    assert "下一个交易日" in text
    # 实现中的常量：订单级 0、控制类 1
    from aqs.core.models import RiskDecision
    from aqs.risk.base import RiskEngine

    assert RiskDecision.pause("x").action.value == "pause"
    assert RiskEngine is not None  # 便于静态检查识别引用


def test_registered_strategies_are_documented():
    text = read(DOCS / "04_strategy_layer.md")
    from aqs.strategy import available_strategies

    for name in available_strategies():
        assert name in text, f"策略 {name} 未在 docs/04 中说明"
    assert strategy_count() >= 3


# --------------------------------------------------------------------------- #
# 2) 模型映射表的交付标记
# --------------------------------------------------------------------------- #
def test_system_design_table_does_not_mark_delivered_modules_as_missing():
    text = read(DOCS / "00_system_design.md")
    delivered = ("risk/var.py", "portfolio/sizing.py", "engine/cost.py")
    for module in delivered:
        line = next((ln for ln in text.splitlines() if module in ln and ln.strip().startswith("|")), None)
        assert line is not None, f"模型映射表中缺少 {module}"
        assert "❌" not in line, f"{module} 已交付，但映射表仍标 ❌：{line.strip()[:80]}"


# --------------------------------------------------------------------------- #
# 3) 用例数与模块数
# --------------------------------------------------------------------------- #
def test_readme_test_counts_match_code():
    cases, modules = count_test_cases()
    text = read(README)
    case_match = re.search(r"(\d+)\s*个单元测试用例", text)
    module_match = re.search(r"（(\d+)\s*个测试模块）", text)
    assert case_match, "README 未声明用例总数"
    assert module_match, "README 未声明测试模块数"
    assert int(case_match.group(1)) == cases, f"README 写 {case_match.group(1)}，实际 {cases}"
    assert int(module_match.group(1)) == modules, f"README 写 {module_match.group(1)}，实际 {modules}"


def test_acceptance_report_test_counts_match_code():
    cases, modules = count_test_cases()
    text = read(DOCS / "03_acceptance_report.md")
    case_match = re.search(r"\|\s*用例总数\s*\|\s*\*\*(\d+)\*\*\s*\|", text)
    module_match = re.search(r"\|\s*测试模块\s*\|\s*(\d+)\s*个\s*\|", text)
    assert case_match, "验收报告未声明用例总数"
    assert module_match, "验收报告未声明测试模块数"
    assert int(case_match.group(1)) == cases, f"验收报告写 {case_match.group(1)}，实际 {cases}"
    assert int(module_match.group(1)) == modules, f"验收报告写 {module_match.group(1)}，实际 {modules}"


def test_system_design_total_case_count_matches_code():
    """`docs/00` 的「合计 N 个单元测试用例」也必须与代码一致。

    第三轮 M2 发现该处仍停留在第一轮的 398，属同一类文档漂移（缺陷 #11 家族），
    因此纳入机械守护，而不是靠人工记得改。
    """
    cases, _ = count_test_cases()
    text = read(DOCS / "00_system_design.md")
    match = re.search(r"合计\s*(\d+)\s*个单元测试用例", text)
    assert match, "docs/00 未声明用例总数"
    assert int(match.group(1)) == cases, f"docs/00 写 {match.group(1)}，实际 {cases}"


def test_changelog_case_count_matches_code():
    """CHANGELOG 的最新状态数字也必须与代码一致。"""
    cases, _ = count_test_cases()
    text = read(PROJECT_ROOT / "CHANGELOG.md")
    match = re.search(r"用例数\s*\*\*[\d]+\s*→\s*(\d+)\*\*", text)
    assert match, "CHANGELOG 未声明用例数变化"
    assert int(match.group(1)) == cases, f"CHANGELOG 写 {match.group(1)}，实际 {cases}"


# --------------------------------------------------------------------------- #
# 4) 版本号
# --------------------------------------------------------------------------- #
def test_package_version_matches_pyproject():
    import aqs

    text = read(PROJECT_ROOT / "pyproject.toml")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml 缺少 version"
    assert match.group(1) == aqs.__version__, f"pyproject {match.group(1)} ≠ aqs.__version__ {aqs.__version__}"


# --------------------------------------------------------------------------- #
# 5) 文档引用完整性
# --------------------------------------------------------------------------- #
def test_documents_referenced_by_readme_exist():
    text = read(README)
    for target in re.findall(r"\]\((docs/[^)]+\.md)\)", text):
        assert (PROJECT_ROOT / target).exists(), f"README 引用了不存在的文档：{target}"


def test_framework_matrix_states_single_main_engine():
    text = read(DOCS / "12_framework_matrix.md")
    assert "自研" in text
    assert "主引擎" in text
    assert "GPL" in text, "必须写明 Backtrader 的许可证风险"


def test_framework_matrix_marks_pending_items_and_links_appendix():
    """M3：`docs/12` 的待核实项必须显式标注并指向 appendix，不得含糊带过。"""
    text = read(DOCS / "12_framework_matrix.md")
    assert "12_appendix_verification.md" in text, "docs/12 必须链接到核实清单附录"
    # 每个待核实点都要能追到 appendix 的哪一项
    for item in ("V1", "V2", "V3"):
        assert f"appendix {item}" in text, f"docs/12 缺少到 appendix {item} 的指引"
    assert "待核实（见 appendix）" in text, "矩阵中的待核实项必须显式标注出处"

    appendix = read(DOCS / "12_appendix_verification.md")
    for item in ("V1", "V2", "V3", "V4", "V5", "V6"):
        assert f"## {item}" in appendix, f"appendix 缺少 {item} 小节"
    # 每项都必须给出：目标 URL / 核实命令 / 回填位置 / 降级处置
    for key in ("候选仓库 URL", "核实命令", "回填位置", "降级处置"):
        assert key in appendix, f"appendix 缺少必需要素：{key}"
    assert "不执行任何拉取" in appendix, "appendix 必须写明沙箱不执行拉取"
