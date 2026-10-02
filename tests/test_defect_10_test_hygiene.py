"""回归缺陷 #10：测试代码坏味道。

已修复的具体问题：
- ``tests/test_events.py``：``__side_buy()`` 定义在第 59 行、首次调用却在第 42 行
  （依赖运行时查找，且 ``__`` 前缀让静态工具失效）→ 已改为直接使用 ``Side.BUY``；
- ``tests/test_portfolio_engine.py``：先调用 ``load_portfolio(...)`` 又立刻被
  ``build_portfolio(...)`` 覆盖（冗余调用，掩盖真实配置来源）→ 已删除。

本文件用 AST 做**通用守护**，避免同类问题再次出现：
测试模块中不得存在「先调用、后定义」的模块级函数。

另外守护**测试基础设施本身**：零依赖运行器与 pytest 的**收集规则**必须一致。
收集阶段不一致不会报错，只会让「看起来全绿」变成「实际没跑」——
M4-9 的契约测试把检查项定义在 mixin 里、4 个子类只继承不重写，正是这种情形。
"""

from __future__ import annotations

import ast
import inspect
import types
import unittest
from pathlib import Path
from typing import Iterator

from tests import run_tests as runner
from tests.compat import raises
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


# --------------------------------------------------------------------------- #
# 收集规则一致性（运行器 ↔ pytest）
# --------------------------------------------------------------------------- #
class _ContractMixin:
    """模拟契约基类：名字不以 ``Test`` 开头 → 自身不得被收集。"""

    def test_inherited(self) -> None:
        pass

    def test_inherited_second(self) -> None:
        pass

    def helper_not_a_test(self) -> None:  # pragma: no cover - 非用例
        pass

    @property
    def test_property_must_not_be_collected(self) -> int:  # pragma: no cover
        raise AssertionError("属性被当成用例执行了：说明收集时对属性做了求值")


class TestChildOfContractMixin(_ContractMixin):
    """空子类：只继承，不重写。"""

    def test_own(self) -> None:
        pass


class _PlainNotATest:
    """有 test_* 方法但类名不以 Test 开头 → 不得被收集。"""

    def test_should_not_be_collected(self) -> None:  # pragma: no cover
        raise AssertionError("非 Test* 类被收集了")


def _namespace() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        _ContractMixin=_ContractMixin,
        TestChildOfContractMixin=TestChildOfContractMixin,
        _PlainNotATest=_PlainNotATest,
    )


def test_runner_collects_inherited_test_methods():
    """正例：mixin 里定义的 ``test_*`` 必须被子类收集；反例：mixin/属性/非 Test 类不得被收集。"""
    collected = sorted(name for name, _ in runner._iter_tests(_namespace()))

    assert collected == [
        "TestChildOfContractMixin.test_inherited",
        "TestChildOfContractMixin.test_inherited_second",
        "TestChildOfContractMixin.test_own",
    ], f"实际收集：{collected}"
    # 反例 1：mixin 自身不以 Test 开头 → 绝不能出现在收集结果里
    assert not any(name.startswith("_ContractMixin") for name in collected)
    # 反例 2：@property 是描述符不是方法 → 不能被当成用例（否则会在收集时求值）
    assert not any("property" in name for name in collected)
    # 反例 3：非 Test* 类里的 test_* 方法不得被收集
    assert not any("should_not_be_collected" in name for name in collected)
    # 反例 4：普通辅助方法不得被收集
    assert not any("helper_not_a_test" in name for name in collected)


def test_runner_collection_matches_pytest_and_unittest_rules():
    """两种运行器的**收集集合**必须一致（本机未装 pytest，故独立实现同一规则比对）。

    - pytest 规则：``Test*`` 类 + 以 ``test`` 开头的**函数属性**（沿 MRO 查找，
      属性用 ``getattr_static`` 取以免求值）；
    - unittest 规则：``TestLoader.loadTestsFromTestCase`` 的实际加载结果。

    这两条与运行器的收集结果必须**逐项相等**，否则「看起来全绿」可能是「实际没跑」。
    """
    namespace = _namespace()

    def pytest_like() -> list[str]:
        found: list[str] = []
        for name, obj in vars(namespace).items():
            if inspect.isfunction(obj) and name.startswith("test"):
                found.append(name)
            elif inspect.isclass(obj) and name.startswith("Test"):
                for attr in dir(obj):
                    if not attr.startswith("test"):
                        continue
                    if inspect.isfunction(inspect.getattr_static(obj, attr)):
                        found.append(f"{name}.{attr}")
        return sorted(found)

    runner_like = sorted(name for name, _ in runner._iter_tests(namespace))
    assert runner_like == pytest_like(), (
        f"运行器与 pytest 收集不一致：\n运行器 {runner_like}\npytest {pytest_like()}"
    )

    # unittest 路径（TestCase 子类）也要与运行器一致
    class _MixinCase(unittest.TestCase):
        def test_inherited_case(self) -> None:
            pass

    class TestCaseChild(_MixinCase):
        def test_own_case(self) -> None:
            pass

    loader = unittest.TestLoader()
    loaded = sorted(
        f"{case.__class__.__name__}.{case._testMethodName}"
        for case in loader.loadTestsFromTestCase(TestCaseChild)
    )
    case_namespace = types.SimpleNamespace(_MixinCase=_MixinCase, TestCaseChild=TestCaseChild)
    by_runner = sorted(name for name, _ in runner._iter_tests(case_namespace))
    assert by_runner == loaded, f"TestCase 路径不一致：运行器 {by_runner} / unittest {loaded}"


def test_contract_mixin_is_not_collected_as_a_test_class():
    """契约基类的名字不能以 Test 开头：否则同一批检查会被跑两遍并挂在错误名下。"""
    assert not _ContractMixin.__name__.startswith("Test")
    assert not any(
        name.startswith(_ContractMixin.__name__)
        for name, _ in runner._iter_tests(_namespace())
    )


def test_skip_helper_raises_unittest_skiptest():
    """`skip()` 必须抛 `unittest.SkipTest`，**即使 pytest 可用时也一样**。

    实测（V1，pytest 9.1.1）：`tests/compat.py` 曾把 `skip` 绑成 `pytest.skip`，
    而 pytest 的 `Skipped` 继承自 **`BaseException`** —— 零依赖运行器只捕获
    `unittest.SkipTest`/`Exception`，于是**一个 `skip()` 就让整个运行中途退出且不打印摘要**
    （`RUNNER_EXIT=1`、无「通过/失败」行）。`unittest.SkipTest` 两套运行器都识别，
    是唯一正确的共同分母。
    """
    from tests.compat import skip

    with raises(unittest.SkipTest):
        skip("probe")

    # 把「为什么危险」编码进测试：pytest 的 Skipped 不是 Exception 子类
    try:
        import pytest
    except ImportError:
        return
    assert not issubclass(pytest.skip.Exception, Exception), (
        "pytest 的 Skipped 若变成 Exception 子类，本守护的前提需重新评估（并更新 docs/17 §10.2）"
    )
