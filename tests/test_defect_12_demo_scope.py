"""回归缺陷 #12：``--symbols`` 参数口径与输出不一致。

**复现结论（实测）**：``generate_market_data(n_symbols=12/15/30/50)`` 的
生成代码数 / 唯一代码数 / ``bundle.symbols()`` **三者完全一致**（30 → 30），
生成器没有丢标的。真实问题是**三个口径被混为一谈**：

- ``--symbols 30``   = 生成 30 只；
- ``index_size=15``  = 其中 15 只进入指数成分（股票池候选上限）；
- 再经 ST/停牌/上市不足 60 日/流动性过滤后 = 当日入池约 12 只。

次要健壮性问题：``_symbol_codes`` 的去重兜底
``s.replace(".", f"{rng.integers(1,9)}.", 1)`` 会产出非法代码（如 ``6000005..SH``）。
当前参数下该分支是死代码（实测无重复），但已改为确定性分配从根上消除。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

from tests.compat import approx
from tests.tools import PROJECT_ROOT, workspace_tmp

from aqs.data.loader import describe_data_scope
from aqs.data.store import DataStore
from aqs.data.synthetic import _symbol_codes, generate_market_data

CODE_RE = re.compile(r"^\d{6}\.(SH|SZ)$")
DEMO = PROJECT_ROOT / "examples" / "demo_backtest.py"


# --------------------------------------------------------------------------- #
# 1) 代码生成：唯一、合法、确定性
# --------------------------------------------------------------------------- #
def test_symbol_codes_are_unique_and_valid():
    import numpy as np

    rng = np.random.default_rng(1)
    for n in (1, 12, 50, 200):
        codes = _symbol_codes(n, rng)
        assert len(codes) == n
        assert len(set(codes)) == n, f"n={n} 存在重复代码"
        assert all(CODE_RE.match(c) for c in codes), f"n={n} 存在非法代码：{codes[:5]}"


def test_symbol_codes_are_deterministic():
    import numpy as np

    assert _symbol_codes(20, np.random.default_rng(1)) == _symbol_codes(20, np.random.default_rng(2))


def test_symbol_codes_negative_n_rejected():
    import numpy as np

    try:
        _symbol_codes(-1, np.random.default_rng(1))
    except ValueError:
        return
    raise AssertionError("负数应报错")


def test_symbol_codes_cover_multiple_boards():
    import numpy as np

    codes = _symbol_codes(30, np.random.default_rng(1))
    prefixes = {c[:3] for c in codes}
    assert {"600", "000"} <= prefixes
    assert any(p in prefixes for p in ("300",)), prefixes


# --------------------------------------------------------------------------- #
# 2) 合成数据：每个标的都有 list_date 与 industry
# --------------------------------------------------------------------------- #
def test_synthetic_supplies_list_date_for_all_symbols():
    bundle = generate_market_data(n_symbols=20, start="2022-01-04", end="2022-12-30", seed=3)
    assert bundle.bars["list_date"].isna().sum() == 0, "合成数据必须提供上市日（缺陷 #8 前置）"
    assert len(bundle.symbols()) == 20
    assert set(bundle.bars["symbol"]) == set(_symbol_codes(20))


def test_synthetic_supplies_industry():
    bundle = generate_market_data(n_symbols=12, start="2022-01-04", end="2022-12-30", seed=3)
    assert "industry" in bundle.bars.columns
    assert bundle.bars["industry"].notna().all()
    assert bundle.bars["industry"].nunique() >= 2


def test_synthetic_old_symbols_are_not_treated_as_new():
    bundle = generate_market_data(n_symbols=10, start="2022-01-04", end="2022-12-30", seed=3)
    store = DataStore(
        bundle.bars,
        index_members=bundle.index_members,
        fundamentals=bundle.fundamentals,
        config={"quality": {"strict": False}},
        universe_config={"mode": "all"},
    )
    day = store.calendar.days[40]
    symbol = store.all_listed(day)[0]
    assert store.listed_days(symbol, day) > 60
    assert store.listed_days_source(symbol) in ("list_date", "list_date_window")


# --------------------------------------------------------------------------- #
# 3) 数据口径四元组自洽
# --------------------------------------------------------------------------- #
def test_describe_data_scope_is_self_consistent():
    bundle = generate_market_data(n_symbols=30, index_size=15, start="2022-01-04", end="2022-12-30", seed=3)
    store = DataStore(
        bundle.bars,
        index_members=bundle.index_members,
        fundamentals=bundle.fundamentals,
        config={"quality": {"strict": False}},
        universe_config={"mode": "index", "index_code": "000300.SH"},
    )
    scope = describe_data_scope(bundle, store)
    assert scope["generated"] == 30
    assert scope["with_bars"] == 30
    assert scope["index_members_avg"] > 0
    assert scope["index_members_avg"] <= scope["with_bars"]
    assert scope["universe_avg"] <= scope["index_members_avg"]
    assert scope["universe_min"] <= scope["universe_avg"] <= scope["universe_max"]
    assert len(scope["universe_sizes"]) == len(scope["universe_sample_days"])


def test_index_size_is_configurable():
    small = generate_market_data(n_symbols=20, index_size=5, start="2022-01-04", end="2022-12-30", seed=3)
    store = DataStore(
        small.bars,
        index_members=small.index_members,
        fundamentals=small.fundamentals,
        config={"quality": {"strict": False}},
        universe_config={"mode": "index"},
    )
    scope = describe_data_scope(small, store)
    assert scope["index_members_avg"] <= 5
    assert scope["index_members_avg"] > 0
    assert scope["universe_avg"] <= scope["index_members_avg"]


# --------------------------------------------------------------------------- #
# 4) CLI
# --------------------------------------------------------------------------- #
def test_cli_exposes_generate_symbols_and_index_size():
    proc = subprocess.run(
        [sys.executable, str(DEMO), "--help"],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    assert "--generate-symbols" in proc.stdout
    assert "--index-size" in proc.stdout


def test_cli_runs_and_reports_data_scope():
    with workspace_tmp("demo") as tmp:
        proc = subprocess.run(
            [
                sys.executable,
                str(DEMO),
                "--generate-symbols",
                "10",
                "--index-size",
                "4",
                "--start",
                "2022-01-04",
                "--end",
                "2022-06-30",
                "--output",
                str(tmp / "out"),
            ],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            encoding="utf-8",
            errors="replace",
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert "数据口径" in proc.stdout

        summary = json.loads((tmp / "out" / "summary.json").read_text(encoding="utf-8"))
        scope = summary["diagnostics"]["data_scope"]
        assert scope["generated"] == 10
        assert scope["with_bars"] == 10
        assert scope["index_members_avg"] <= 4
        assert scope["universe_avg"] <= scope["index_members_avg"]


def test_cli_deprecated_alias_still_works():
    with workspace_tmp("demo_alias") as tmp:
        proc = subprocess.run(
            [
                sys.executable,
                str(DEMO),
                "--symbols",
                "8",
                "--start",
                "2022-01-04",
                "--end",
                "2022-03-31",
                "--output",
                str(tmp / "out"),
            ],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            encoding="utf-8",
            errors="replace",
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert "deprecation" in proc.stdout
        summary = json.loads((tmp / "out" / "summary.json").read_text(encoding="utf-8"))
        assert summary["diagnostics"]["data_scope"]["generated"] == 8
