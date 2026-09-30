"""诊断脚本：订单/持仓的「整手不变量」体检（D2 根因量化）。

检查项
------
I1. BUY 订单 ``quantity`` 是否为 100 的整数倍（A 股买入最小单位）；
I2. SELL 订单 ``quantity`` 是否为**整数股**（零股可卖，但股数必须是整数）；
I3. 任何订单的 ``filled_quantity`` 是否为整数股；
I4. 部分成交后剩余不足一手 → 是否被误判为硬拒单（``status=rejected`` 且 ``filled_quantity>0``）；
I5. 期末持仓股数是否为整数；
I6. ``filled`` 终态订单是否仍携带非空 ``reject_reason``（字段语义，见 D3）；
I7. 疑似虚假拒单量：``stats.rejected`` 与 I4 的重叠。

用法::

    set PYTHONPATH=D:\\Quantify\\src;D:\\Quantify
    python tools\\diag_lot_invariants.py --strategy ma_cross --generate-symbols 30
"""

from __future__ import annotations

import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

STRATEGY_FILES = {
    "ma_cross": "configs/strategies/ma_cross.yaml",
    "breakout": "configs/strategies/breakout.yaml",
    "volume": "configs/strategies/volume.yaml",
}

LOT = 100


def _int(x: float) -> bool:
    return abs(float(x) - round(float(x))) < 1e-9


def _lot(x: float) -> bool:
    return _int(x) and int(round(float(x))) % LOT == 0


def run(strategy_name: str, n_symbols: int, seed: int, end: str):
    from aqs.config.loader import load_base_config
    from aqs.data.store import DataStore
    from aqs.data.synthetic import generate_market_data
    from aqs.engine.backtest import BacktestEngine
    from aqs.portfolio import load_portfolio
    from aqs.strategy import load_spec, load_strategy
    from aqs.strategy.registry import filters_to_data_overrides

    spec = load_spec(STRATEGY_FILES[strategy_name])
    strategy = load_strategy(STRATEGY_FILES[strategy_name])
    data_overrides: dict = {"provider": "synthetic", "quality": {"strict": False}}
    data_overrides.update(filters_to_data_overrides(spec.filters))
    config = load_base_config("configs/base.yaml").with_overlay(
        {
            "data": data_overrides,
            "universe": {"mode": "index", "index_code": "000300.SH"},
            "engine": {"start": "2022-01-04", "end": end},
        }
    )
    portfolio = load_portfolio(
        STRATEGY_FILES[strategy_name], defaults=config.portfolio, lot_size=config.engine.lot_size
    )
    bundle = generate_market_data(
        n_symbols=n_symbols, start=config.engine.start, end=config.engine.end, seed=seed
    )
    store = DataStore(
        bundle.bars,
        index_members=bundle.index_members,
        fundamentals=bundle.fundamentals,
        config=config.data,
        universe_config=config.universe,
    )
    engine = BacktestEngine(store, config, strategy=strategy, portfolio=portfolio, risk_audit_log=None)
    result = engine.run()
    return engine, result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="整手不变量体检")
    parser.add_argument("--strategy", choices=sorted(STRATEGY_FILES), default="ma_cross")
    parser.add_argument("--generate-symbols", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20240101)
    parser.add_argument("--end", default="2023-12-29")
    parser.add_argument("--show", type=int, default=6)
    args = parser.parse_args(argv)

    engine, result = run(args.strategy, args.generate_symbols, args.seed, args.end)
    rows = result.orders.to_dict("records")
    print(f"策略={args.strategy} 生成={args.generate_symbols} 区间至={args.end}")
    print(f"订单={len(rows)}  成交={len(result.trades)}")
    print()

    buys = [r for r in rows if r["side"] == "buy"]
    sells = [r for r in rows if r["side"] == "sell"]

    i1 = [r for r in buys if not _lot(r["quantity"])]
    i2 = [r for r in sells if not _int(r["quantity"])]
    i3 = [r for r in rows if not _int(r["filled_quantity"])]
    i4 = [
        r for r in rows
        if r["status"] in ("rejected", "risk_rejected")
        and float(r["filled_quantity"]) > 0
        and r["reject_reason"] == "lot_size"
    ]
    positions = getattr(result.account, "positions", {})
    i5 = {s: p.total_quantity for s, p in positions.items() if not _int(p.total_quantity)}
    i6 = [
        r for r in rows
        if r["status"] == "filled" and r["reject_reason"] not in ("none", "", None)
    ]
    stats = result.broker.stats
    match_stats = dict(result.broker.matching.stats)

    print(f"I1 BUY 订单非整手               : {len(i1)} / {len(buys)}")
    print(f"I2 SELL 订单非整数股            : {len(i2)} / {len(sells)}")
    print(f"I3 成交股数非整数               : {len(i3)} / {len(rows)}")
    print(f"I4 部分成交后剩余<1手被硬拒单   : {len(i4)}")
    print(f"I5 期末持仓非整数股             : {len(i5)}  {dict(list(i5.items())[:5])}")
    print(f"I6 filled 订单仍带 reject_reason : {len(i6)}")
    print()
    print(f"broker.stats.rejected           : {stats.rejected}")
    print(f"broker.stats.expired            : {stats.expired}")
    print(f"matching.stats                  : {match_stats}")
    print()

    def _show(title, items):
        if not items:
            return
        print(f"--- {title}（前 {min(len(items), args.show)} 条）---")
        for r in items[: args.show]:
            print(
                f"    {r['order_id']} {r['side']:4s} qty={r['quantity']!r} "
                f"filled={r['filled_quantity']!r} status={r['status']} "
                f"reason={r['reject_reason']} deferred={r['deferred_days']} tag={r['tag']}"
            )

    _show("I1 BUY 非整手", i1)
    _show("I2 SELL 非整数股", i2)
    _show("I3 成交非整数", i3)
    _show("I4 部分成交后剩余不足一手被硬拒", i4)
    _show("I6 filled 但带 reject_reason", i6)
    print()
    print(
        "结论口径：I1/I2/I3/I5 应为 0（物理不变量）；"
        "I4 应为 0（剩余不足一手应记 EXPIRED，不计入拒单）；"
        "I6 允许 >0，但字段名必须能自解释（见 D3）。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
