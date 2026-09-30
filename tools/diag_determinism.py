"""诊断脚本：定位「哈希顺序」引入的非确定性（D4 根因定位）。

原理
----
Python 默认对 str 哈希加盐（``PYTHONHASHSEED`` 随机），因此 ``set`` / ``frozenset``
的迭代顺序**逐进程变化**。若某个决策路径依赖了这种顺序，则同一 seed、同一数据的
回测结果会随进程变化。

本脚本以固定配置跑一次回测，并把**每日决策指纹**写成 JSON：

- ``universe``：当日入池标的（来自 ``store.universe``）
- ``signals`` ：策略输出的信号序列（顺序敏感）
- ``plans``   ：组合层输出的订单计划序列（顺序敏感）
- ``orders``  ：引擎实际提交的订单（顺序敏感）
- ``fills``   ：实际成交（顺序敏感）

用同一命令在不同 ``PYTHONHASHSEED`` 下各跑一次，再比较两份 JSON，
**首个出现差异的日期与环节**即为泄漏点。

用法::

    set PYTHONPATH=D:\\Quantify\\src;D:\\Quantify
    $env:PYTHONHASHSEED="0";     python tools\\diag_determinism.py --out .tmp_diag\\hs0.json
    $env:PYTHONHASHSEED="12345"; python tools\\diag_determinism.py --out .tmp_diag\\hs1.json
    python tools\\diag_determinism.py --compare .tmp_diag\\hs0.json .tmp_diag\\hs1.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

STRATEGY_FILES = {
    "ma_cross": "configs/strategies/ma_cross.yaml",
    "breakout": "configs/strategies/breakout.yaml",
    "volume": "configs/strategies/volume.yaml",
}


def _data_digest(strategy_name: str, n_symbols: int, seed: int, end: str) -> dict:
    """数据层指纹：只做数据生成，不跑回测（用于快速定位 D4 类缺陷）。"""
    from aqs.data.synthetic import generate_market_data

    bundle = generate_market_data(
        n_symbols=n_symbols, start="2022-01-04", end=end, seed=seed
    )
    members = bundle.index_members
    if members is None or len(members) == 0:
        rows: list[list[str]] = []
    else:
        frame = members.sort_values(["symbol", "effective_from"], kind="stable")
        rows = [
            [str(r.symbol), str(r.effective_from), str(r.effective_to)]
            for r in frame.itertuples()
        ]
    symbols = sorted(set(bundle.bars["symbol"].tolist()))
    return {
        "hashseed": os.environ.get("PYTHONHASHSEED", "<unset>"),
        "totals": {
            "index_members": len(rows),
            "symbols": len(symbols),
            "symbols_digest": "|".join(symbols),
        },
        "by_day": {"index_members": rows},
    }


def _run(strategy_name: str, n_symbols: int, seed: int, end: str = "2023-12-29") -> dict:
    from aqs.config.loader import load_base_config
    from aqs.data.store import DataStore
    from aqs.data.synthetic import generate_market_data
    from aqs.engine.backtest import BacktestEngine
    from aqs.portfolio import load_portfolio
    from aqs.strategy import load_spec, load_strategy
    from aqs.strategy.registry import filters_to_data_overrides

    spec = load_spec(STRATEGY_FILES[strategy_name])
    real_strategy = load_strategy(STRATEGY_FILES[strategy_name])
    data_overrides: dict = {"provider": "synthetic", "quality": {"strict": False}}
    data_overrides.update(filters_to_data_overrides(spec.filters))
    config = load_base_config("configs/base.yaml").with_overlay(
        {
            "data": data_overrides,
            "universe": {"mode": "index", "index_code": "000300.SH"},
            "engine": {"start": "2022-01-04", "end": end},
        }
    )
    real_portfolio = load_portfolio(
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

    trace: dict[str, dict] = {}

    def _slot(day: str) -> dict:
        return trace.setdefault(day, {})

    def strategy(ctx):
        intents = list(real_strategy.on_bar(ctx))
        _slot(str(ctx.date))["signals"] = [
            [s.symbol, str(getattr(s.direction, "value", s.direction)), round(float(s.score), 10)]
            for s in intents
        ]
        return intents

    def portfolio(signals, ctx):
        plans = list(real_portfolio.generate_orders(signals, ctx))
        _slot(str(ctx.date))["plans"] = [
            [p.symbol, str(getattr(p.side, "value", p.side)), float(p.quantity), p.tag]
            for p in plans
        ]
        return plans

    engine = BacktestEngine(
        store, config, strategy=strategy, portfolio=portfolio, risk_audit_log=None
    )

    # 每日入池指纹（引擎在开盘前构建，复用引擎所用的同一个 builder）
    original_build = store.universe_builder.build

    def build(day, **kwargs):
        result = original_build(day, **kwargs)
        _slot(str(day))["universe"] = list(result)
        return result

    store.universe_builder.build = build  # type: ignore[method-assign]

    result = engine.run()

    for row in result.orders.to_dict("records"):
        day = str(row.get("submit_date") or row.get("signal_date"))
        _slot(day).setdefault("orders", []).append(
            [row["order_id"], row["symbol"], row["side"], float(row["quantity"]), row["status"]]
        )
    for row in result.trades.to_dict("records"):
        day = str(row.get("trade_date"))
        _slot(day).setdefault("fills", []).append(
            [row["fill_id"], row["symbol"], row["side"], float(row["quantity"])]
        )

    return {
        "hashseed": os.environ.get("PYTHONHASHSEED", "<unset>"),
        "totals": {
            "orders": int(len(result.orders)),
            "trades": int(len(result.trades)),
            "total_return": round(float(result.summary().get("total_return", 0.0)), 12),
        },
        "by_day": {d: trace[d] for d in sorted(trace)},
    }


def _compare(path_a: str, path_b: str) -> int:
    with open(path_a, encoding="utf-8") as fh:
        a = json.load(fh)
    with open(path_b, encoding="utf-8") as fh:
        b = json.load(fh)
    print(f"A: hashseed={a['hashseed']} totals={a['totals']}")
    print(f"B: hashseed={b['hashseed']} totals={b['totals']}")
    print(f"总量一致: {a['totals'] == b['totals']}")
    all_days = sorted(set(a["by_day"]) | set(b["by_day"]))
    diffs = 0
    for day in all_days:
        da = a["by_day"].get(day, {})
        db = b["by_day"].get(day, {})
        for key in ("universe", "signals", "plans", "orders", "fills"):
            va, vb = da.get(key), db.get(key)
            if va != vb:
                diffs += 1
                print(f"\n*** 第 {diffs} 处差异  day={day}  section={key}")
                print(f"    A = {va}")
                print(f"    B = {vb}")
                if diffs >= 4:
                    print("\n（已展示 4 处，停止）")
                    return 1
    if diffs == 0:
        print("逐日决策指纹完全一致。")
    return 0 if diffs == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="哈希顺序非确定性诊断")
    parser.add_argument("--strategy", choices=sorted(STRATEGY_FILES), default="ma_cross")
    parser.add_argument("--generate-symbols", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20240101)
    parser.add_argument("--end", default="2023-12-29", help="回测结束日（缩短区间可加速）")
    parser.add_argument(
        "--level",
        choices=("e2e", "data"),
        default="e2e",
        help="e2e=跑完整回测并比对逐日决策；data=仅比对数据生成结果（快）",
    )
    parser.add_argument("--out", default=None, help="把本次运行的指纹写入该 JSON")
    parser.add_argument("--compare", nargs=2, default=None, metavar=("A", "B"))
    args = parser.parse_args(argv)

    if args.compare:
        return _compare(*args.compare)

    if args.level == "data":
        payload = _data_digest(args.strategy, args.generate_symbols, args.seed, args.end)
        if args.out:
            os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            print(f"已写出 {args.out}  hashseed={payload['hashseed']} totals={payload['totals']}")
        else:
            print(json.dumps(payload["totals"], ensure_ascii=False))
        return 0

    payload = _run(args.strategy, args.generate_symbols, args.seed, args.end)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        print(f"已写出 {args.out}  hashseed={payload['hashseed']} totals={payload['totals']}")
    else:
        print(json.dumps(payload["totals"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
