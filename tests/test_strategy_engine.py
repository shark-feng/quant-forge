"""策略层与回测引擎联调测试（端到端）。

验证：
- 三种内置策略都能在真实引擎上跑通；
- 成交日 == 信号日的下一个交易日（T+1）；
- 策略上下文的时间上界 == 当日，任何越界取数抛 `LookaheadError`；
- 同一配置 + 同一种子 → 结果完全可复现。
"""

from __future__ import annotations

from typing import Sequence

from tests.compat import raises
from tests.tools import make_config

from aqs.core.enums import Side, SignalDirection
from aqs.core.exceptions import LookaheadError
from aqs.data.store import DataStore
from aqs.data.synthetic import generate_market_data
from aqs.engine.backtest import BacktestEngine
from aqs.portfolio.base import OrderPlan
from aqs.strategy.base import BaseStrategy, StrategyContext
from aqs.strategy.breakout import BreakoutStrategy
from aqs.strategy.ma_cross import MACrossStrategy
from aqs.strategy.volume import VolumeStrategy

START = "2022-01-04"
END = "2022-12-30"
SEED = 20240101


class BuyTopN:
    """最小组合实现（M4 正式版本待交付）：按 score 排序，等权买入前 N 只。"""

    name = "buy_top_n"

    def __init__(self, max_positions: int = 5, weight: float = 0.15) -> None:
        self.max_positions = max_positions
        self.weight = weight

    def generate_orders(self, signals, ctx: StrategyContext) -> Sequence[OrderPlan]:
        plans: list[OrderPlan] = []
        for sig in signals:
            if sig.direction is SignalDirection.EXIT:
                held = ctx.account.total_quantity(sig.symbol)
                if held > 0:
                    plans.append(OrderPlan(sig.symbol, Side.SELL, held, tag="exit"))
        ranked = sorted(
            (s for s in signals if s.direction is SignalDirection.ENTRY),
            key=lambda s: s.score,
            reverse=True,
        )
        slots = self.max_positions - len(ctx.account.positions)
        for sig in ranked[: max(slots, 0)]:
            bar = ctx.snapshot.bar(sig.symbol)
            if bar is None or bar.close <= 0:
                continue
            budget = min(ctx.account.total_value * self.weight, ctx.account.available_cash() * 0.95)
            qty = int(budget / bar.close // 100) * 100
            if qty >= 100:
                plans.append(OrderPlan(sig.symbol, Side.BUY, qty, tag="entry"))
        return plans


def make_setup(strategy: BaseStrategy, *, symbols: int = 15, seed: int = SEED):
    cfg = make_config(
        {
            "data": {"quality": {"strict": False}},
            "universe": {"mode": "all"},
            "engine": {"start": START, "end": END, "initial_cash": 1_000_000.0},
        }
    )
    bundle = generate_market_data(n_symbols=symbols, start=START, end=END, seed=seed)
    store = DataStore(
        bundle.bars,
        index_members=bundle.index_members,
        fundamentals=bundle.fundamentals,
        config=cfg.data,
        universe_config=cfg.universe,
    )
    engine = BacktestEngine(store, cfg, strategy=strategy, portfolio=BuyTopN())
    return engine, store


# --------------------------------------------------------------------------- #
# 联调
# --------------------------------------------------------------------------- #
def test_ma_cross_runs_end_to_end():
    engine, store = make_setup(MACrossStrategy({"fast_window": 5, "slow_window": 20}))
    result = engine.run()
    assert len(result.equity_curve) == len(store.calendar.sessions(START, END))
    assert result.diagnostics["events_by_type"]["SIGNAL"] > 0
    assert len(result.trades) > 0
    assert result.account.total_costs > 0


def test_every_fill_is_one_day_after_signal():
    engine, store = make_setup(MACrossStrategy({"fast_window": 3, "slow_window": 10}))
    result = engine.run()
    assert len(result.orders) > 0
    for row in result.orders.itertuples():
        signal_date = __import__("pandas").Timestamp(row.signal_date).date()
        submit_date = __import__("pandas").Timestamp(row.submit_date).date()
        assert submit_date == store.calendar.next_trading_day(signal_date)
    for trade in result.trades.itertuples():
        order = result.orders[result.orders["order_id"] == trade.order_id].iloc[0]
        signal_date = __import__("pandas").Timestamp(order["signal_date"]).date()
        assert str(trade.trade_date) >= str(store.calendar.next_trading_day(signal_date))


def test_all_builtin_strategies_run():
    for strategy in (
        MACrossStrategy({"fast_window": 5, "slow_window": 20}),
        BreakoutStrategy({"window": 20}),
        VolumeStrategy({"volume_window": 20, "volume_ratio": 1.5}),
    ):
        engine, _ = make_setup(strategy, symbols=12)
        result = engine.run()
        assert result.diagnostics["events_processed"] > 0
        assert len(result.equity_curve) > 0


def test_strategy_context_is_point_in_time():
    calls: list[str] = []

    class FuturePeeker(MACrossStrategy):
        name = "future_peeker"

        def on_bar(self, ctx):
            calls.append(str(ctx.date))
            assert ctx.data.as_of == ctx.date
            nxt = ctx.calendar.next_trading_day(ctx.date)
            if nxt is not None:
                ctx.data.history(ctx.universe[0], nxt, 5)  # 未来数据 → 必须报错
            return super().on_bar(ctx)

    engine, _ = make_setup(FuturePeeker({"fast_window": 5, "slow_window": 20}), symbols=6)
    with raises(LookaheadError):
        engine.run()
    assert calls  # 至少执行过一次盘后信号计算


def test_backtest_is_reproducible():
    first_engine, _ = make_setup(MACrossStrategy({"fast_window": 5, "slow_window": 20}), symbols=12)
    first = first_engine.run()
    second_engine, _ = make_setup(MACrossStrategy({"fast_window": 5, "slow_window": 20}), symbols=12)
    second = second_engine.run()

    assert len(first.trades) == len(second.trades)
    assert first.account.total_value == second.account.total_value
    assert first.equity_curve["total_value"].tolist() == second.equity_curve["total_value"].tolist()
    if len(first.trades):
        assert first.trades["symbol"].tolist() == second.trades["symbol"].tolist()
        assert first.trades["trade_date"].astype(str).tolist() == second.trades["trade_date"].astype(str).tolist()


def test_signals_never_precede_universe_membership():
    """所有成交标的必须在成交当日属于股票池（策略只对池内标的出信号）。"""
    engine, store = make_setup(MACrossStrategy({"fast_window": 5, "slow_window": 20}))
    result = engine.run()
    for trade in result.trades.itertuples():
        day = __import__("pandas").Timestamp(trade.trade_date).date()
        assert trade.symbol in store.universe(day)
