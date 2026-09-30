"""诊断脚本：追踪「非整手订单」的完整生命周期（D2 根因定位）。

复现 ``examples/demo_backtest.py`` 的回测配置（同一 seed / 区间 / 策略），
然后对每一张数量非整手的订单，打印：

- 订单终态字段（quantity / filled_quantity / status / reject_reason / deferred_days）；
- 该订单的 RMS 削减记录（``engine._rejections`` 中 action=reduce 的条目）；
- 该订单的全部审计记录（风控 check / 成交 fill / 订单 expired）。

只读诊断：审计流仅保留在内存（``risk_audit_log=None``），不写任何报告文件。

用法::

    set PYTHONPATH=D:\\Quantify\\src;D:\\Quantify
    python tools\\diag_order_timeline.py --strategy ma_cross
"""

from __future__ import annotations

import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from aqs.config.loader import load_base_config  # noqa: E402
from aqs.data.store import DataStore  # noqa: E402
from aqs.data.synthetic import generate_market_data  # noqa: E402
from aqs.engine.backtest import BacktestEngine  # noqa: E402
from aqs.portfolio import load_portfolio  # noqa: E402
from aqs.strategy import load_spec, load_strategy  # noqa: E402

LOT_SIZE = 100

STRATEGY_FILES = {
    "ma_cross": "configs/strategies/ma_cross.yaml",
    "breakout": "configs/strategies/breakout.yaml",
    "volume": "configs/strategies/volume.yaml",
}


def is_integral_lot(qty: float) -> bool:
    return abs(qty - round(qty)) < 1e-9 and int(round(qty)) % LOT_SIZE == 0


def build_engine(strategy_name: str, n_symbols: int, seed: int):
    spec = load_spec(STRATEGY_FILES[strategy_name])
    strategy = load_strategy(STRATEGY_FILES[strategy_name])
    from aqs.strategy.registry import filters_to_data_overrides

    data_overrides: dict = {"provider": "synthetic", "quality": {"strict": False}}
    data_overrides.update(filters_to_data_overrides(spec.filters))
    config = load_base_config("configs/base.yaml").with_overlay(
        {
            "data": data_overrides,
            "universe": {"mode": "index", "index_code": "000300.SH"},
            "engine": {"start": "2022-01-04", "end": "2023-12-29"},
        }
    )
    portfolio = load_portfolio(
        STRATEGY_FILES[strategy_name], defaults=config.portfolio, lot_size=config.engine.lot_size
    )
    bundle = generate_market_data(
        n_symbols=n_symbols,
        start=config.engine.start,
        end=config.engine.end,
        seed=seed,
    )
    store = DataStore(
        bundle.bars,
        index_members=bundle.index_members,
        fundamentals=bundle.fundamentals,
        config=config.data,
        universe_config=config.universe,
    )
    # 审计仅内存，避免污染 reports/
    engine = BacktestEngine(
        store, config, strategy=strategy, portfolio=portfolio, risk_audit_log=None
    )
    return engine, config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="非整手订单生命周期诊断")
    parser.add_argument("--strategy", choices=sorted(STRATEGY_FILES), default="ma_cross")
    parser.add_argument("--generate-symbols", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20240101)
    parser.add_argument("--limit", type=int, default=8, help="最多打印多少张问题订单的时间线")
    parser.add_argument("--status", default=None, help="只打印该终态的订单（如 risk_rejected）")
    args = parser.parse_args(argv)

    engine, _cfg = build_engine(args.strategy, args.generate_symbols, args.seed)
    result = engine.run()
    frame = result.orders
    records = engine.risk.audit.records
    rejections = list(engine._rejections)

    print(f"策略={args.strategy}  订单={len(frame)}  成交={len(result.trades)}")
    print(f"RMS={type(engine.risk).__name__}  规则数={len(getattr(engine.risk, 'rules', []))}")
    print(f"审计记录={len(records)}  削减/拦截记录={len(rejections)}")
    print()

    problems = []
    for row in frame.to_dict("records"):
        qty = float(row["quantity"])
        filled = float(row["filled_quantity"])
        if not is_integral_lot(qty) or not is_integral_lot(filled):
            if args.status and row["status"] != args.status:
                continue
            problems.append(row)

    print(f"=== 非整手订单共 {len(problems)} 张（按 side 统计）===")
    from collections import Counter

    print("   ", dict(Counter(r["side"] for r in problems)))
    print("   ", dict(Counter(r["status"] for r in problems)))
    print()

    for row in problems[: args.limit]:
        oid = row["order_id"]
        print("=" * 78)
        print(
            f"{oid} side={row['side']} tag={row.get('tag')} "
            f"qty={row['quantity']!r} filled={row['filled_quantity']!r}"
        )
        print(
            f"    status={row['status']} reason={row['reject_reason']} "
            f"deferred={row['deferred_days']} signal={row['signal_date']} submit={row['submit_date']} "
            f"avg_px={row['avg_fill_price']}"
        )
        red = [r for r in rejections if r.order_id == oid]
        for r in red:
            print(
                f"    [RMS] action={r.action} rule={r.rule} qty={r.quantity!r} day={r.day} px={r.price}"
            )
        mine = [rec for rec in records if rec.payload.get("order_id") == oid]
        print(f"    [审计] {len(mine)} 条")
        for rec in mine:
            payload = {k: v for k, v in rec.payload.items() if k != "order_id"}
            print(f"      {rec.ts} {rec.category}/{rec.action} {payload}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
