"""演示回测：合成 A 股市场 + M3 策略 + M4 组合层 + M5 风控 + 事件驱动引擎。

    python examples/demo_backtest.py [--strategy ma_cross] [--symbols 30] [--cost-scale 1.0]

说明：
- 数据使用 :mod:`aqs.data.synthetic` 生成的**确定性合成行情**（含停牌、涨跌停、ST、退市、
  复权因子与指数成分变更），因此无需外部数据源即可完整跑通链路；
- 策略来自 M3（``aqs.strategy``），组合来自 M4（``aqs.portfolio``），风控来自 M5（``aqs.risk``），
  参数分别来自 ``configs/strategies/*.yaml`` 与 ``configs/risk.yaml``；
- 结果写入 ``reports/<策略名>/``（净值曲线、成交明细、订单、股票池统计、摘要 JSON）。

本项目仅用于量化研究与教育，不构成投资建议。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aqs.config.loader import load_base_config  # noqa: E402
from aqs.core.logging import setup_logging  # noqa: E402
from aqs.data.store import DataStore  # noqa: E402
from aqs.data.synthetic import generate_market_data  # noqa: E402
from aqs.engine.backtest import BacktestEngine  # noqa: E402
from aqs.portfolio import load_portfolio  # noqa: E402
from aqs.strategy import load_spec, load_strategy  # noqa: E402

STRATEGY_FILES = {
    "ma_cross": "configs/strategies/ma_cross.yaml",
    "breakout": "configs/strategies/breakout.yaml",
    "volume": "configs/strategies/volume.yaml",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="AQS 演示回测（合成数据）")
    parser.add_argument("--strategy", choices=sorted(STRATEGY_FILES), default="ma_cross", help="策略名")
    parser.add_argument("--symbols", type=int, default=30, help="标的数量")
    parser.add_argument("--seed", type=int, default=20240101, help="随机种子")
    parser.add_argument("--start", type=str, default="2022-01-04", help="开始日期")
    parser.add_argument("--end", type=str, default="2023-12-29", help="结束日期")
    parser.add_argument("--cost-scale", type=float, default=1.0, help="成本倍数（2.0 = 成本加倍）")
    parser.add_argument("--output", type=str, default=None, help="报告输出目录（默认 reports/<策略名>）")
    args = parser.parse_args()

    setup_logging("INFO")

    # ---- 策略与组合：参数全部来自 YAML，非法参数会直接报错 ----
    spec = load_spec(STRATEGY_FILES[args.strategy])
    strategy = load_strategy(STRATEGY_FILES[args.strategy])
    print(f"策略：{spec.name} 参数={spec.params}")
    print(f"股票池口径：{spec.filters}")

    data_overrides: dict = {"provider": "synthetic", "quality": {"strict": False}}
    data_overrides.update(_filters_to_data(spec.filters))
    config = load_base_config("configs/base.yaml").with_overlay(
        {
            "data": data_overrides,
            "universe": {"mode": "index", "index_code": "000300.SH"},
            "engine": {"start": args.start, "end": args.end},
            "costs": {"scale": args.cost_scale},
        }
    )
    portfolio = load_portfolio(
        STRATEGY_FILES[args.strategy],
        defaults=config.portfolio,
        lot_size=config.engine.lot_size,
    )
    print(f"组合：{portfolio.describe()}")

    bundle = generate_market_data(
        n_symbols=args.symbols,
        start=config.engine.start,
        end=config.engine.end,
        seed=args.seed,
    )
    store = DataStore(
        bundle.bars,
        index_members=bundle.index_members,
        fundamentals=bundle.fundamentals,
        config=config.data,
        universe_config=config.universe,
    )
    print(f"数据就绪：{store.describe()}")

    engine = BacktestEngine(store, config, strategy=strategy, portfolio=portfolio)
    result = engine.run()
    output = args.output or f"reports/{spec.name}"
    written = result.save(output)

    summary = result.summary()
    risk = result.diagnostics["risk"]
    print("\n================ 回测摘要 ================")
    print(f"策略            : {spec.name} {spec.params}")
    print(f"组合            : 权重={portfolio.describe()['weighting']} 最多持仓={portfolio.describe()['max_positions']} "
          f"单票上限={portfolio.describe()['max_weight_per_symbol']:.0%}")
    print(f"区间            : {result.diagnostics['start']} ~ {result.diagnostics['end']}"
          f"（{result.diagnostics['sessions']} 个交易日）")
    print(f"初始资金        : {summary['account_initial_cash']:,.2f}")
    print(f"期末总资产      : {summary['account_total_value']:,.2f}")
    print(f"累计收益        : {summary['total_return'] * 100:.2f}%")
    print(f"年化收益        : {summary['annualized_return'] * 100:.2f}%")
    print(f"最大回撤        : {summary['max_drawdown'] * 100:.2f}%")
    print(f"成交笔数        : {summary['n_trades']}")
    print(f"累计成本(元)    : {summary['account_total_costs']:,.2f}")
    print(f"成本倍数        : {args.cost_scale}")
    print(f"撮合统计        : {result.diagnostics['match_stats']}")
    print(f"事件统计        : {result.diagnostics['events_by_type']}")
    print("\n------------ 风控（RMS）------------")
    print(f"引擎/规则       : {result.diagnostics['risk_engine']} / {len(result.diagnostics['risk_rules'])} 条规则")
    print(f"订单检查/拒单率 : {risk['stats']['checked']} / {risk['stats']['reject_rate']:.2%}")
    print(f"削减次数        : {risk['stats']['reduced']}")
    print(f"暂停/强平       : {result.diagnostics['risk_paused']} / {result.diagnostics['risk_force_close']}")
    print(f"触发延迟(均/最大): {risk['latency']['mean_days']:.1f} / {risk['latency']['max_days']} 个交易日")
    print(f"误杀率(事后5日) : {risk['rejection']['false_reject_rate']:.2%} "
          f"（评估 {risk['rejection']['n_evaluated']}/{risk['rejection']['n_rejections']} 笔被拦订单）")
    print("\n输出文件：")
    for name, path in written.items():
        print(f"  {name:16s} {path}")
    print("\n注意：本结果基于合成数据，仅用于系统自检，不代表任何策略在真实市场上的表现。")
    return 0


def _filters_to_data(filters: dict) -> dict:
    """把策略 filters 段映射为数据层配置（复用策略注册表的映射逻辑）。"""
    from aqs.strategy.registry import filters_to_data_overrides

    return filters_to_data_overrides(filters)


if __name__ == "__main__":
    raise SystemExit(main())

