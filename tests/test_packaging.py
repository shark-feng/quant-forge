"""工程化一致性守护（第三轮 M2）。

把「许可证一致、依赖分组一致、忽略规则够用、主包不得耦合第三方回测框架」变成机械红灯，
避免靠人工核对（这类漂移正是缺陷 #11 文档漂移的同一类问题）。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from tests.tools import PROJECT_ROOT

PYPROJECT = PROJECT_ROOT / "pyproject.toml"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
GITIGNORE = PROJECT_ROOT / ".gitignore"
LICENSE = PROJECT_ROOT / "LICENSE"
NOTICE = PROJECT_ROOT / "NOTICE"
CHANGELOG = PROJECT_ROOT / "CHANGELOG.md"
UPLOAD_SCRIPT = PROJECT_ROOT / "tools" / "upload_github.ps1"

# 第三方回测框架：**禁止**出现在主包依赖或任何 extras 中
FORBIDDEN_FRAMEWORKS = ("backtrader", "pybroker", "akquant", "backtesting", "zipline", "vnpy")


def _pyproject() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _requirement_names(text: str) -> set[str]:
    """解析 requirements 文本中**未被注释**的依赖名（小写、去掉版本约束）。"""
    names: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!\[; ]", line, maxsplit=1)[0].strip().lower()
        if name:
            names.add(name)
    return names


def _project_requires() -> set[str]:
    names: set[str] = set()
    for spec in _pyproject()["project"]["dependencies"]:
        names.add(re.split(r"[<>=!\[; ]", spec, maxsplit=1)[0].strip().lower())
    return names


# --------------------------------------------------------------------------- #
# 1. 许可证一致
# --------------------------------------------------------------------------- #
def test_license_file_is_mit():
    text = LICENSE.read_text(encoding="utf-8")
    assert text.startswith("MIT License"), "LICENSE 必须以 'MIT License' 开头"
    assert "Permission is hereby granted, free of charge" in text
    assert "WITHOUT WARRANTY OF ANY KIND" in text
    assert "shark-feng" in text, "LICENSE 必须写明版权持有人"


def test_pyproject_license_matches_license_file():
    proj = _pyproject()["project"]
    declared = proj.get("license")
    text = declared.get("text") if isinstance(declared, dict) else declared
    assert text == "MIT", f"pyproject 许可证应为 MIT，实际 {text!r}"
    assert any("MIT License" in c for c in proj.get("classifiers", []))


def test_license_strategy_documented_in_framework_matrix():
    """docs/12 的许可证评估必须与 LICENSE 一致（MIT）。"""
    doc = (PROJECT_ROOT / "docs" / "12_framework_matrix.md").read_text(encoding="utf-8")
    assert "MIT" in doc
    assert "GPL" in doc, "必须说明 GPL 隔离策略"


def test_notice_covers_attribution_and_compliance():
    text = NOTICE.read_text(encoding="utf-8")
    for key in ("AKShare", "MIT", "GPL", "合规", "不分发原始数据", "待核实"):
        assert key in text, f"NOTICE 缺少关键内容：{key}"


# --------------------------------------------------------------------------- #
# 2. 依赖分组一致
# --------------------------------------------------------------------------- #
def test_core_dependencies_are_minimal():
    """核心依赖保持最小（第一阶段只需这三个）。"""
    assert _project_requires() == {"numpy", "pandas", "pyyaml"}


def test_optional_dependency_groups_exist():
    extras = _pyproject()["project"]["optional-dependencies"]
    for group in ("science", "data", "dev", "all"):
        assert group in extras, f"缺少依赖分组：{group}"
        assert extras[group], f"依赖分组 {group} 为空"


def test_all_group_covers_every_other_group():
    extras = _pyproject()["project"]["optional-dependencies"]
    others: set[str] = set()
    for group, specs in extras.items():
        if group == "all":
            continue
        for spec in specs:
            others.add(re.split(r"[<>=!\[; ]", spec, maxsplit=1)[0].strip().lower())
    all_group = {
        re.split(r"[<>=!\[; ]", s, maxsplit=1)[0].strip().lower() for s in extras["all"]
    }
    missing = others - all_group
    assert not missing, f"extras.all 未覆盖以下依赖：{sorted(missing)}"


def test_no_third_party_backtest_framework_in_dependencies():
    """边界纪律：第三方回测框架禁止进入主包依赖或 extras。"""
    proj = _pyproject()["project"]
    specs = list(proj["dependencies"])
    for group in proj.get("optional-dependencies", {}).values():
        specs.extend(group)
    blob = " ".join(specs).lower()
    for framework in FORBIDDEN_FRAMEWORKS:
        assert framework not in blob, (
            f"主包依赖中出现第三方回测框架 {framework} —— "
            "违反 docs/DEVELOPMENT.md §7 与 docs/12_framework_matrix.md"
        )
    # 同一检查必须覆盖 requirements.txt
    req_blob = REQUIREMENTS.read_text(encoding="utf-8").lower()
    for framework in FORBIDDEN_FRAMEWORKS:
        assert framework not in req_blob, f"requirements.txt 中出现 {framework}"


def test_requirements_matches_pyproject_core_group():
    """requirements.txt 的未注释依赖必须与 pyproject 的 core 分组一致。"""
    assert _requirement_names(REQUIREMENTS.read_text(encoding="utf-8")) == _project_requires()


def test_requirements_comments_carry_no_active_optional_deps():
    """可选依赖在 requirements.txt 中必须注释掉（避免误装）。"""
    text = REQUIREMENTS.read_text(encoding="utf-8")
    active = _requirement_names(text)
    for optional in ("scipy", "statsmodels", "cvxpy", "akshare", "pyarrow", "pytest"):
        assert optional not in active, (
            f"{optional} 在 requirements.txt 中处于启用状态；应保持注释，"
            "由 pyproject 的 extras 按需安装"
        )


def test_version_is_single_source_of_truth():
    """pyproject 版本必须与 aqs.__version__ 一致（由 test_defect_11 覆盖一致性细节）。"""
    from aqs import __version__

    assert _pyproject()["project"]["version"] == __version__


def test_package_name_and_import_name_are_documented():
    proj = _pyproject()["project"]
    assert proj["name"] == "quant-forge"
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "quant-forge" in readme
    assert "import aqs" in readme or "src/aqs/" in readme


# --------------------------------------------------------------------------- #
# 3. .gitignore 覆盖度
# --------------------------------------------------------------------------- #
def test_gitignore_covers_required_patterns():
    text = GITIGNORE.read_text(encoding="utf-8")
    required = [
        "__pycache__/",
        ".tmp_tests/",
        "reports/*",
        "data/cache/",
        "*.parquet",
        "*.log",
        ".env",
        "*.pem",
        "*.key",
        "*.jsonl",
    ]
    for pattern in required:
        assert pattern in text, f".gitignore 缺少必需规则：{pattern}"


def test_gitignore_keeps_gitkeep_files():
    text = GITIGNORE.read_text(encoding="utf-8")
    assert "!reports/.gitkeep" in text
    assert (PROJECT_ROOT / "reports" / ".gitkeep").exists()


def test_gitattributes_locks_line_endings_and_binary_types():
    """换行策略必须显式声明（避免 CRLF/LF 混用把 diff 淹没）。"""
    path = PROJECT_ROOT / ".gitattributes"
    assert path.exists(), "缺少 .gitattributes"
    text = path.read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in text, "必须声明默认 LF 策略"
    assert "*.ps1 text eol=crlf" in text, "Windows 脚本应保留 CRLF"
    for binary in ("*.parquet", "*.png", "*.xlsx", "*.pdf"):
        assert binary in text, f".gitattributes 未声明二进制类型：{binary}"


# --------------------------------------------------------------------------- #
# 4. CHANGELOG 与上传脚本
# --------------------------------------------------------------------------- #
def test_changelog_has_current_version_section():
    from aqs import __version__

    text = CHANGELOG.read_text(encoding="utf-8")
    assert f"## [{__version__}]" in text, f"CHANGELOG 缺少当前版本 {__version__} 的小节"
    assert "## [Unreleased]" in text
    # 已交付的缺陷必须留痕
    for defect in ("#13", "#14", "#15", "#16"):
        assert defect in text, f"CHANGELOG 未记录缺陷 {defect}"


def test_upload_script_exists_and_guards_against_overwrite():
    assert UPLOAD_SCRIPT.exists(), "缺少 tools/upload_github.ps1"
    text = UPLOAD_SCRIPT.read_text(encoding="utf-8")
    assert "pull --rebase" in text, "上传脚本必须先 pull --rebase，避免覆盖远端内容"
    assert "git ls-files" in text, "上传脚本必须包含密钥扫描"
    assert "run_tests.py" in text, "上传脚本必须在推送前跑测试"
    assert "quant-forge" in text


def test_upload_script_scan_is_robust_and_non_interactive():
    """Q3 修正的守护：扫描不拼路径列表、支持非交互、计数显式转 int。"""
    text = UPLOAD_SCRIPT.read_text(encoding="utf-8")

    # (1) 不得把受控文件路径列表拼进 git grep 命令行（文件多时会超出命令行长度上限）
    assert "-- $tracked" not in text, "密钥扫描不应拼接 $tracked 路径列表"
    # 用词边界匹配「独立的 $tracked 变量」：`$trackedCount` 这类派生变量不算违规
    assert not re.search(r"\$tracked\b", text), "应移除独立的 $tracked 变量"

    # (2) 必须提供非交互开关，且命中密钥时不再无条件 Read-Host
    assert "[switch]$NonInteractive" in text, "缺少 -NonInteractive 开关"
    assert "if ($NonInteractive) {" in text, "非交互模式下必须先中止而不是询问"

    # (3) 领先提交数必须显式转 int（避免字符串比较的隐式转换）
    assert "[int](git rev-list --count" in text, "localAhead 应显式转换为 int"

    # (4) 未跟踪文件也必须纳入扫描（git grep 只扫受控文件）
    assert "ls-files --others --exclude-standard" in text, "必须额外扫描未跟踪文件"

    # (5) 含中文的 .ps1 必须带 UTF-8 BOM
    #     否则 PowerShell 5.1 与 Parser::ParseFile 会按系统 ANSI（GBK）读取，
    #     中文变乱码、字符串终止符被破坏，进而报出一堆假语法错误（已实测踩到）。
    assert UPLOAD_SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf"), (
        "含中文的 .ps1 必须带 UTF-8 BOM，否则非 UTF-8 默认编码的 PowerShell 会读成乱码"
    )


def test_readme_documents_akshare_runbook():
    text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "AKShare 数据准备" in text
    for key in ("probe_akshare.py", "fetch_data.py", "data/cache", "Q3", "Q12", "strict"):
        assert key in text, f"README 的 AKShare 章节缺少：{key}"


def test_readme_links_resolve():
    """README 中的相对链接（docs/、NOTICE、LICENSE、CHANGELOG）必须都存在。"""
    text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    missing: list[str] = []
    for target in re.findall(r"\]\(([^)#]+)\)", text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (PROJECT_ROOT / target).exists():
            missing.append(target)
    assert not missing, f"README 链接指向不存在的文件：{missing}"
