"""回归缺陷 #6：RMS ``audit_log`` 配置未生效。

原缺陷：``_build_risk_engine`` 显式 ``dataclasses.replace(cfg, audit_log=None)``，
把 ``configs/risk.yaml`` 里的 ``audit_log: reports/rms_audit.jsonl`` 抹掉了，
审计永远只存内存，回测结束即丢失。

修复后：
- 默认跟随 ``risk.yaml`` 的 ``audit_log``（相对路径按项目根解析）；
- ``engine.risk_audit_log`` 或构造参数可覆盖：``None`` / ``""`` / ``"none"`` = 仅内存；
- 回测结束调用 ``risk.close()``，确保审计文件完整落盘。
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.compat import approx
from tests.tools import DEFAULT_START, bar_row, dates, entry_signal, make_bars, make_config, make_store, workspace_tmp

from aqs.config.loader import PROJECT_ROOT, load_base_config, resolve_path
from aqs.data.store import DataStore
from aqs.data.synthetic import generate_market_data
from aqs.engine.backtest import BacktestEngine
from aqs.portfolio.base import OrderPlan
from aqs.strategy.base import StrategyContext

D = dates(12)
SYM = "600000.SH"


class OneShotStrategy:
    name = "one_shot"

    def on_bar(self, ctx: StrategyContext):
        return [entry_signal(SYM, ctx.date)] if ctx.date == D[4] else []


class BuyOnce:
    name = "buy_once"

    def generate_orders(self, signals, ctx: StrategyContext):
        return [OrderPlan(SYM, __import__("aqs.core.enums", fromlist=["Side"]).Side.BUY, 1000, tag="t")] if signals else []


def make_market():
    rows = [bar_row(day, SYM, close=10.0, open_=10.0, prev_close=10.0, volume=1_000_000.0) for day in D]
    return make_bars(rows)


def build_engine(**kwargs) -> BacktestEngine:
    config = make_config(
        {
            "data": {"quality": {"strict": False}},
            "universe": {"mode": "all"},
            "engine": {"start": D[0], "end": D[-1]},
        }
    )
    store = make_store(make_market())
    return BacktestEngine(store, config, strategy=OneShotStrategy(), portfolio=BuyOnce(), **kwargs)


# --------------------------------------------------------------------------- #
# 路径解析
# --------------------------------------------------------------------------- #
def test_default_audit_log_follows_risk_config():
    """不传参数时使用 base.yaml → risk.yaml 的 audit_log 配置。"""
    engine = build_engine()
    expected = resolve_path("reports/rms_audit.jsonl")
    assert engine.risk_audit_log == expected


def test_relative_audit_log_is_resolved_under_project_root():
    engine = build_engine(risk_audit_log="reports/__test_relative_audit.jsonl")
    assert engine.risk_audit_log == PROJECT_ROOT / "reports/__test_relative_audit.jsonl"


def test_memory_only_variants():
    for value in (None, "", "none", "memory"):
        engine = build_engine(risk_audit_log=value)
        assert engine.risk_audit_log is None, value


def test_engine_config_can_disable_audit_file():
    config = make_config(
        {
            "data": {"quality": {"strict": False}},
            "universe": {"mode": "all"},
            "engine": {"start": D[0], "end": D[-1], "risk_audit_log": "none"},
        }
    )
    store = make_store(make_market())
    engine = BacktestEngine(store, config, strategy=OneShotStrategy(), portfolio=BuyOnce())
    assert engine.risk_audit_log is None


# --------------------------------------------------------------------------- #
# 真实落盘
# --------------------------------------------------------------------------- #
def test_audit_file_is_written_with_jsonl_records():
    with workspace_tmp("audit") as tmp:
        path = tmp / "rms_audit.jsonl"
        engine = build_engine(risk_audit_log=str(path))
        result = engine.run()

        assert path.exists(), "审计文件必须生成"
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) >= 1
        first = json.loads(lines[0])
        assert set(first) >= {"ts", "category", "action", "payload"}
        assert any(json.loads(ln)["category"] == "risk" for ln in lines)

        diag = result.diagnostics["risk"]
        assert diag["audit_log"] == str(path)
        assert diag["audit_records"] >= 1
        assert diag["audit_records"] == len(lines)


def test_memory_only_run_keeps_records_in_memory():
    with workspace_tmp("audit") as tmp:
        unused = tmp / "should_not_exist.jsonl"
        engine = build_engine(risk_audit_log=None)
        result = engine.run()
        assert not unused.exists()
        assert result.diagnostics["risk"]["audit_log"] is None
        assert result.diagnostics["risk"]["audit_records"] >= 1  # 内存中仍有记录


def test_repeated_run_is_idempotent_after_close():
    with workspace_tmp("audit") as tmp:
        path = tmp / "rms_audit.jsonl"
        engine = build_engine(risk_audit_log=str(path))
        engine.run()
        engine.risk.close()  # 幂等：重复关闭不报错
        engine.risk.close()
        assert path.exists()


def test_synthetic_end_to_end_writes_audit():
    """端到端（合成数据 + 默认配置）确认审计落盘，且诊断路径与文件一致。"""
    from aqs.strategy.ma_cross import MACrossStrategy

    with workspace_tmp("audit") as tmp:
        path = tmp / "e2e_audit.jsonl"
        base = load_base_config("configs/base.yaml").with_overlay(
            {
                "data": {"quality": {"strict": False}},
                "universe": {"mode": "all"},
                "engine": {"start": "2022-01-04", "end": "2022-12-30"},
            }
        )
        bundle = generate_market_data(n_symbols=8, start="2022-01-04", end="2022-12-30", seed=5)
        store = DataStore(
            bundle.bars,
            index_members=bundle.index_members,
            fundamentals=bundle.fundamentals,
            config=base.data,
            universe_config=base.universe,
        )
        engine = BacktestEngine(
            store,
            base,
            strategy=MACrossStrategy({"fast_window": 3, "slow_window": 8}),
            portfolio=BuyOnce(),
            risk_audit_log=str(path),
        )
        result = engine.run()
        assert path.exists()
        assert result.diagnostics["risk"]["audit_records"] > 0
        assert path.stat().st_size > 0
        assert result.diagnostics["risk"]["audit_log"] == str(path)
