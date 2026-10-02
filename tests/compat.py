"""测试兼容层。

测试用例统一使用 pytest 风格（``assert`` + ``raises``），
在未安装 pytest 的环境中由 :mod:`tests.run_tests` 运行，此处提供最小等价实现，
保证同一套测试代码在两种运行方式下都能通过。

**``skip`` 必须统一抛 ``unittest.SkipTest``**（即使 pytest 可用时也一样）：
pytest 的 ``Skipped`` 继承自 ``BaseException`` 而非 ``Exception``，
零依赖运行器只捕获 ``unittest.SkipTest``/``Exception`` —— 实测把 ``skip`` 直接绑到
``pytest.skip`` 后，一次 ``skip()`` 会让**整个运行器中途退出且不打印摘要**
（`RUNNER_EXIT=1`、无「通过/失败」行）。而 ``unittest.SkipTest`` 两种运行器都识别，
故这里是唯一正确的共同分母（单一实现原则，见 `docs/DEVELOPMENT.md` §3）。
"""

from __future__ import annotations

import re
import unittest
from typing import Any

try:  # pragma: no cover - 取决于环境
    import pytest  # type: ignore

    HAS_PYTEST = True
    raises = pytest.raises
    approx = pytest.approx
    fixture = pytest.fixture
    mark = pytest.mark
    pytest_skip = pytest.skip

except ImportError:  # pragma: no cover - 无 pytest 环境
    HAS_PYTEST = False
    pytest_skip = None


def skip(reason: str = "") -> None:
    """跳过当前用例（或模块）。

    抛 ``unittest.SkipTest``：本项目零依赖运行器与 pytest **都**识别它，
    且跳过不计入通过（避免「环境缺依赖」被读成「验证通过」）。
    """
    raise unittest.SkipTest(reason)


#: 兼容旧引用：``from tests.compat import raises, skip`` 等写法保持不变
__all__ = [
    "HAS_PYTEST",
    "raises",
    "approx",
    "skip",
    "fixture",
    "mark",
    "pytest",
    "pytest_skip",
]

if not HAS_PYTEST:  # pragma: no cover - 无 pytest 环境

    class ExceptionInfo:
        """最小化的异常信息对象（对应 pytest 的 ``ExceptionInfo``）。"""

        def __init__(self) -> None:
            self.value: BaseException | None = None
            self.type: type[BaseException] | None = None

    class _RaisesContext:
        def __init__(self, expected: type[BaseException] | tuple[type[BaseException], ...], match: str | None) -> None:
            self.expected = expected
            self.match = match
            self.info = ExceptionInfo()

        def __enter__(self) -> ExceptionInfo:
            return self.info

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            if exc_type is None:
                raise AssertionError(f"预期抛出 {self.expected}，但未抛出任何异常")
            if not issubclass(exc_type, self.expected):  # type: ignore[arg-type]
                return False
            self.info.value = exc
            self.info.type = exc_type
            if self.match is not None and re.search(self.match, str(exc)) is None:
                raise AssertionError(f"异常信息 {str(exc)!r} 不匹配 {self.match!r}")
            return True

    def raises(expected, match: str | None = None):  # type: ignore[no-untyped-def]
        return _RaisesContext(expected, match)

    class _Approx:
        def __init__(self, expected: float, rel: float = 1e-6, abs_: float = 1e-12) -> None:
            self.expected = expected
            self.rel = rel
            self.abs = abs_

        def __eq__(self, other: object) -> bool:
            try:
                return abs(float(other) - self.expected) <= max(self.abs, self.rel * abs(self.expected))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return NotImplemented

        def __repr__(self) -> str:  # pragma: no cover
            return f"approx({self.expected})"

    def approx(expected: float, rel: float = 1e-6, abs: float = 1e-12) -> _Approx:  # noqa: A002
        return _Approx(expected, rel, abs)

    # 注意：`skip` **不在此处定义** —— 它只在上方定义一次并统一抛 `unittest.SkipTest`
    # （见模块 docstring：pytest 的 Skipped 继承 BaseException，会让运行器整体中止）。

    def fixture(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        def decorator(fn):  # type: ignore[no-untyped-def]
            return fn

        if args and callable(args[0]):
            return args[0]
        return decorator

    class _Mark:
        def __getattr__(self, item: str):  # pragma: no cover
            def decorator(fn):  # type: ignore[no-untyped-def]
                return fn

            return decorator

    mark = _Mark()


__all__ = ["HAS_PYTEST", "raises", "approx", "skip", "fixture", "mark", "pytest"]
