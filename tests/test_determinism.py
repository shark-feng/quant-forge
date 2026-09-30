"""回归缺陷 #14：回测结果必须**跨进程**可复现（与 Python 哈希顺序无关）。

诊断见 `docs/17_round3_diagnostics.md` §5。缺陷本质：

``synthetic.py`` 迭代一个 ``set`` 后把结果交给 ``rng.choice``，而 Python 对 ``str``
哈希默认加盐（``PYTHONHASHSEED`` 逐进程变化）→ 同一 seed 生成出**不同的指数成分历史**
→ 不同股票池 → 不同订单 → 不同收益。实测同一命令三次运行得到 313 / 537 / 463 张订单。

**为什么必须跨进程**：同一进程内哈希顺序不变，
现有用例 ``test_portfolio_engine::test_portfolio_result_is_reproducible``
是「同进程跑两次」，因此**从未**发现该缺陷。本模块用两个子进程、两个不同的
``PYTHONHASHSEED`` 来覆盖。

**为什么 `index_size < n_symbols` 才能暴露**：指数成分定期调整（``rng.choice``）只在
候选池非空时发生；若全部标的都已入指数（如 12 只标的、index_size=15），
``rng.choice`` 一次都不会被调用，缺陷路径不会被触发。因此本模块固定用
30 只标的（index_size 默认 15）来构造真实的多股票池场景。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.tools import PROJECT_ROOT, workspace_tmp

PROBE = PROJECT_ROOT / "tools" / "diag_determinism.py"
SYMBOLS = 30  # 必须 > 默认 index_size(15)，否则指数调整路径不会被触发
END = "2022-06-30"  # 缩短区间以加速（缺陷与区间长度无关）


def _run_probe(out: Path, hashseed: str, *, level: str = "data", symbols: int = SYMBOLS) -> dict:
    """在**独立子进程**中以指定 PYTHONHASHSEED 运行探测脚本。"""
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hashseed
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(PROBE),
                "--level",
                level,
                "--generate-symbols",
                str(symbols),
                "--end",
                END,
                "--out",
                str(out),
            ],
            check=True,
            cwd=str(PROJECT_ROOT),
            env=env,
            # 用 DEVNULL 而不是 PIPE：受限沙箱下管道捕获子进程输出会被拒绝
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - 环境限制
        raise AssertionError(f"无法启动确定性探测子进程：{exc}") from exc
    assert out.exists(), f"探测脚本未产出结果文件：{out}"
    with out.open(encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# 1. 数据层：同一 seed 必须生成同一份指数成分历史
# --------------------------------------------------------------------------- #
def test_data_generation_is_hash_order_independent():
    """核心回归：两个不同 PYTHONHASHSEED 下生成的数据必须完全一致。"""
    with workspace_tmp("det_data") as tmp:
        a = _run_probe(tmp / "a.json", "0")
        b = _run_probe(tmp / "b.json", "12345")

    assert a["hashseed"] == "0" and b["hashseed"] == "12345", "子进程未继承 PYTHONHASHSEED"
    assert a["totals"] == b["totals"], (
        "数据生成结果依赖哈希顺序：\n"
        f"  PYTHONHASHSEED=0     -> {a['totals']}\n"
        f"  PYTHONHASHSEED=12345 -> {b['totals']}"
    )
    assert a["by_day"] == b["by_day"], "指数成分历史随进程变化（缺陷 #14 复现）"
    assert a["totals"]["index_members"] > 0, "用例前提：应当生成指数成分记录"


def test_data_generation_probe_is_sensitive_enough():
    """哨兵：探测必须真的**走到了指数定期调整**路径，否则上面的相等是假绿灯。

    初始成分数 = ``min(index_size, 全部标的)`` = 15；每次调仓换入一只就多一条记录。
    因此 ``index_members > 15`` 证明 ``rng.choice`` 的抽样路径确实被执行过
    （正是缺陷 #14 的触发点）。
    """
    with workspace_tmp("det_sentinel") as tmp:
        payload = _run_probe(tmp / "s.json", "777")
    assert payload["totals"]["symbols"] == SYMBOLS
    assert payload["totals"]["index_members"] > 15, (
        "指数成分记录数未超过初始成分数，说明定期调整（缺陷触发点）没有被执行，"
        f"该用例无法发现缺陷 #14：{payload['totals']}"
    )


# --------------------------------------------------------------------------- #
# 2. 端到端：不同哈希顺序下回测结果完全相同
# --------------------------------------------------------------------------- #
def test_end_to_end_backtest_is_hash_order_independent():
    with workspace_tmp("det_e2e") as tmp:
        a = _run_probe(tmp / "a.json", "0", level="e2e")
        b = _run_probe(tmp / "b.json", "999", level="e2e")

    assert a["totals"] == b["totals"], (
        f"回测结果依赖哈希顺序：{a['totals']} vs {b['totals']}"
    )
    assert a["by_day"] == b["by_day"], "逐日决策（股票池/信号/计划/订单/成交）随进程变化"
    assert a["totals"]["orders"] > 0, "用例前提：应当产生订单"


def test_same_hashseed_is_also_stable():
    """同一 hashseed 两次运行也必须一致（排除其它来源的非确定性）。"""
    with workspace_tmp("det_same") as tmp:
        a = _run_probe(tmp / "a.json", "42", level="e2e")
        b = _run_probe(tmp / "b.json", "42", level="e2e")
    assert a["totals"] == b["totals"]
