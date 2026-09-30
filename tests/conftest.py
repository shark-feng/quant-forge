"""pytest 配置与共享夹具。

同时保证 ``src`` 在 ``sys.path`` 上（无 pytest 时由 ``tests/run_tests.py`` 兜底）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


@pytest.fixture()
def config():
    from tests.tools import make_config

    return make_config()


@pytest.fixture()
def flat_store():
    """两个标的、40 个交易日、价格恒定的数据仓库。"""
    from tests.tools import flat_market, make_store

    return make_store(flat_market(n=40))
