"""零依赖测试运行器（pytest 不可用时的兜底）。

用法：
    python tests/run_tests.py            # 运行 tests/ 下全部 test_*.py
    python tests/run_tests.py test_cost  # 只运行名字包含 test_cost 的模块

行为等价于最朴素的 pytest：收集 ``test_*`` 函数与 ``Test*`` 类中的 ``test_*`` 方法，
不传参调用，断言失败即计为失败。安装 pytest 后可直接用 ``pytest -q``。
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import traceback
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
    for name, obj in vars(module).items():
        if name.startswith("test_") and inspect.isfunction(obj):
            yield name, obj
        elif name.startswith("Test") and inspect.isclass(obj):
            instance = obj()
            for m_name, m in vars(obj).items():
                if m_name.startswith("test_") and inspect.isfunction(m):
                    yield f"{name}.{m_name}", getattr(instance, m_name)


def main(argv: list[str]) -> int:
    pattern = argv[0] if argv else ""
    files = sorted(p for p in TESTS.glob("test_*.py") if pattern in p.stem)
    if not files:
        print(f"未找到匹配的测试文件（pattern={pattern!r}）")
        return 1

    passed = failed = 0
    failures: list[tuple[str, str]] = []
    for path in files:
        try:
            module = _load_module(path)
        except Exception:  # noqa: BLE001
            failed += 1
            failures.append((f"{path.stem} (导入失败)", traceback.format_exc()))
            continue
        for name, fn in _iter_tests(module):
            label = f"{path.stem}::{name}"
            try:
                fn()
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
    print(f"\n通过 {passed} 个，失败 {failed} 个（共 {passed + failed} 个用例）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
