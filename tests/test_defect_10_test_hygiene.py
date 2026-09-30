"""回归缺陷 #10：测试代码坏味道。

已修复的具体问题：
- ``tests/test_events.py``：``__side_buy()`` 定义在第 59 行、首次调用却在第 42 行
  （依赖运行时查找，且 ``__`` 前缀让静态工具失效）→ 已改为直接使用 ``Side.BUY``；
- ``tests/test_portfolio_engine.py``：先调用 ``load_portfolio(...)`` 又立刻被
  ``build_portfolio(...)`` 覆盖（冗余调用，掩盖真实配置来源）→ 已删除。

本文件用 AST 做**通用守护**，避免同类问题再次出现：
测试模块中不得存在「先调用、后定义」的模块级函数。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

from tests.tools import PROJECT_ROOT

TESTS_DIR = PROJECT_ROOT / "tests"


def iter_test_modules() -> Iterator[Path]:
    yield from sorted(TESTS_DIR.glob("test_*.py"))


def module_level_definitions(tree: ast.Module) -> dict[str, int]:
    defs: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs[node.name] = node.lineno
    return defs


def first_use_lines(tree: ast.Module) -> dict[str, int]:
    uses: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            uses.setdefault(node.id, node.lineno)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                uses.setdefault(func.id, node.lineno)
    return uses


# --------------------------------------------------------------------------- #
def test_no_module_level_function_is_used_before_definition():
    offenders: list[str] = []
    for path in iter_test_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        defs = module_level_definitions(tree)
        uses = first_use_lines(tree)
        for name, def_line in defs.items():
            use_line = uses.get(name)
            if use_line is not None and use_line < def_line:
                offenders.append(f"{path.name}:{name} 在第 {use_line} 行使用、第 {def_line} 行定义")
    assert not offenders, "存在「先调用后定义」的模块级函数：\n" + "\n".join(offenders)


def test_no_dunder_private_helpers_in_tests():
    """测试模块不应定义 ``__name`` 形式的私有辅助函数（历史上导致过查找问题）。"""
    offenders: list[str] = []
    for path in iter_test_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name in module_level_definitions(tree):
            if name.startswith("__") and not name.endswith("__"):
                offenders.append(f"{path.name}:{name}")
    assert not offenders, f"测试模块中不应出现 __ 前缀函数：{offenders}"


def test_events_module_uses_side_enum_directly():
    source = (TESTS_DIR / "test_events.py").read_text(encoding="utf-8")
    assert "__side_buy" not in source
    assert "Side.BUY" in source


def test_portfolio_engine_module_has_no_redundant_load_portfolio():
    """只检查**实际调用**（注释里保留说明是允许的）。"""
    path = TESTS_DIR / "test_portfolio_engine.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "load_portfolio" not in called, "组合配置应只通过 build_portfolio 构造（避免冗余覆盖）"
    assert "build_portfolio" in called


def test_all_test_modules_import_cleanly():
    """所有测试模块都能被导入（AST 通过 ≠ 导入通过）。"""
    import importlib

    for path in iter_test_modules():
        importlib.import_module(f"tests.{path.stem}")


def test_test_module_count_is_positive():
    modules = list(iter_test_modules())
    assert len(modules) >= 20
    assert all(m.name.startswith("test_") for m in modules)
