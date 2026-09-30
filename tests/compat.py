"""测试兼容层。

测试用例统一使用 pytest 风格（``assert`` + ``raises``），
在未安装 pytest 的环境中由 :mod:`tests.run_tests` 运行，此处提供最小等价实现，
保证同一套测试代码在两种运行方式下都能通过。
"""

from __future__ import annotations

import re
from typing import Any

try:  # pragma: no cover - 取决于环境
    import pytest  # type: ignore

    HAS_PYTEST = True
    raises = pytest.raises
    approx = pytest.approx
    skip = pytest.skip
    fixture = pytest.fixture
    mark = pytest.mark

except ImportError:  # pragma: no cover - 无 pytest 环境
    HAS_PYTEST = False

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

    def skip(reason: str = "") -> None:  # type: ignore[no-untyped-def]
        """跳过当前用例。

        抛 ``unittest.SkipTest``：本项目零依赖运行器与 pytest **都**识别它，
        且跳过不计入通过（避免「环境缺依赖」被读成「验证通过」）。
        """
        import unittest

        raise unittest.SkipTest(reason)

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
