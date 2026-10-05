"""skip 语义探针（V4）：**被跳过才是通过**。

用途只有一个：给 `test_defect_10_test_hygiene.py::test_compat_skip_shows_as_skipped_under_pytest`
提供一个「确定会 skip」的目标 —— 该守护用**子进程实跑 pytest** 并断言本模块的用例
显示为 `SKIPPED`（而不是 `ERROR`）。

为什么需要它（背景 V1，pytest 9.1.1）：`tests/compat.py` 的 `skip` 曾随「装没装 pytest」
在 `unittest.SkipTest` 与 `pytest.skip.Exception` 之间切换，后者继承自 `BaseException`，
导致零依赖运行器在装好 pytest 后**整体中止且不打印摘要**。
语义统一为 `unittest.SkipTest` 之后，必须**两套运行器都把它识别为「跳过」** ——
本模块就是那条断言的靶子：它在两套运行器下都应计为 **skip**，且都不计为通过。

注意：本用例**永远跳过**，这是刻意的 —— 它验证的是"跳过"这一机制本身。
"""

from __future__ import annotations

from tests.compat import skip


def test_compat_skip_is_reported_as_skipped():
    """本用例**被跳过**才是正确结果（它验证 skip 机制，不验证业务逻辑）。"""
    skip("skip 语义探针：被跳过 = 通过（见 docs/DEVELOPMENT.md §3 第四条禁令）")
