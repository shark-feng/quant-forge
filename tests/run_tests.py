"""零依赖测试运行器（pytest 不可用时的兜底）。

用法：
    python tests/run_tests.py            # 运行 tests/ 下全部 test_*.py
    python tests/run_tests.py test_cost  # 只运行名字包含 test_cost 的模块

行为等价于最朴素的 pytest：收集 ``test_*`` 函数与 ``Test*`` 类中的 ``test_*`` 方法，
不传参调用，断言失败即计为失败。安装 pytest 后可直接用 ``pytest -q``。

**跳过（skip）**：抛出 ``unittest.SkipTest`` 视为跳过并单独计数
（pytest 原生支持同一写法）。跳过**不计入通过**，以免「环境缺失」被读成「验证通过」。
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import traceback
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for p in (ROOT / "src", ROOT, TESTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def _iter_tests(module):
    """收集模块里的用例：``test_*`` 函数 + ``Test*`` 类中的 ``test_*`` 方法。

    **必须包含继承来的方法**：契约测试（`tests/contracts/provider_contract.py`）把检查项
    定义在 mixin 里，4 个子类只继承不重写 —— 若只遍历类自身的 ``__dict__``，
    运行器会收集到 0 条而 pytest 会收集到全部。**收集阶段的不一致不会报错，
    只会让「看起来全绿」变成「实际没跑」**，故此处与 pytest 的查找规则对齐：
    沿 MRO 查找 ``test_*`` 属性，且只认函数（``@property`` 之类的描述符不算方法）。
    """
    for name, obj in vars(module).items():
        if name.startswith("test_") and inspect.isfunction(obj):
            yield name, obj
        elif name.startswith("Test") and inspect.isclass(obj):
            instance = obj()
            for m_name, m in inspect.getmembers(type(instance), predicate=inspect.isfunction):
                if m_name.startswith("test_"):
                    yield f"{name}.{m_name}", getattr(instance, m_name)


def main(argv: list[str]) -> int:
    pattern = argv[0] if argv else ""
    files = sorted(p for p in TESTS.glob("test_*.py") if pattern in p.stem)
    if not files:
        print(f"未找到匹配的测试文件（pattern={pattern!r}）")
        return 1

    passed = failed = skipped = 0
    failures: list[tuple[str, str]] = []
    skips: list[tuple[str, str]] = []
    for path in files:
        try:
            module = _load_module(path)
        except unittest.SkipTest as exc:  # pragma: no cover - 模块级跳过
            skipped += 1
            skips.append((f"{path.stem} (模块级跳过)", str(exc)))
            print(f"skip {path.stem} (模块级跳过: {exc})")
            continue
        except Exception:  # noqa: BLE001
            failed += 1
            failures.append((f"{path.stem} (导入失败)", traceback.format_exc()))
            continue
        for name, fn in _iter_tests(module):
            label = f"{path.stem}::{name}"
            try:
                fn()
            except unittest.SkipTest as exc:
                skipped += 1
                skips.append((label, str(exc)))
                print(f"skip {label} ({exc})")
            except Exception:  # noqa: BLE001
                failed += 1
                failures.append((label, traceback.format_exc()))
                print(f"FAIL {label}")
            else:
                passed += 1
                print(f"ok   {label}")

    print("-" * 72)
    for label, tb in failures:
        print(f"\n===== {label} =====")
        print(tb)
    if skips:
        print("\n----- 跳过（环境不满足，未验证）-----")
        for label, reason in skips:
            print(f"skip {label}: {reason}")
    summary = f"\n通过 {passed} 个，失败 {failed} 个"
    if skipped:
        summary += f"，跳过 {skipped} 个"
    print(summary + f"（共 {passed + failed + skipped} 个用例）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
