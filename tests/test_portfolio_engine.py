"""组合层与回测引擎联调测试（端到端）。"""

from __future__ import annotations

from tests.compat import approx
from tests.tools import make_config

from aqs.config.loader import load_base_config
from aqs.data.store import DataStore
from aqs.data.synthetic import generate_market_data
from aqs.engine.backtest import BacktestEngine
from aqs.portfolio.registry import build_portfolio
from aqs.strategy.ma_cross import MACrossStrategy

START = "2022-01-04"
END = "2022-12-30"
SEED = 20240101
SYMBOLS = 20


def make_setup(*, strategy_params=None, portfolio_overrides=None, symbols: int = SYMBOLS):
    base = load_base_config("configs/base.yaml").with_overlay(
        {
            "data": {"quality": {"strict": False}},
            "universe": {"mode": "all"},
            "engine": {"start": START, "end": END, "initial_cash": 1_000_000.0},
        }
    )
    bundle = generate_market_data(n_symbols=symbols, start=START, end=END, seed=SEED)
    store = DataStore(
        bundle.bars,
        index_members=bundle.index_members,
        fundamentals=bundle.fundamentals,
        config=base.data,
        universe_config=base.universe,
    )
    # 组合配置来源：base.yaml 的 portfolio 段 + 本用例的显式覆盖
    # （修正：原实现先调用 load_portfolio 再被 build_portfolio 覆盖，属冗余调用）
    section = {"max_positions": 5, "max_weight_per_symbol": 0.15, "cash_buffer": 0.02}
    section.update(portfolio_overrides or {})
    portfolio = build_portfolio(section, defaults=base.portfolio, lot_size=base.engine.lot_size)
    strategy = MACrossStrategy(strategy_params or {"fast_window": 5, "slow_window": 20})
    engine = BacktestEngine(store, base, strategy=strategy, portfolio=portfolio)
    return engine, store, portfolio


def test_backtest_with_production_portfolio_runs():
    engine, store, portfolio = make_setup()
    result = engine.run()
    assert len(result.trades) > 0
    assert result.account.total_costs > 0
    assert len(result.equity_curve) == len(store.calendar.sessions(START, END))
    assert all(p.side in ("buy", "sell") for p in result.trades.itertuples())


def test_position_count_never_exceeds_max_positions():
    engine, _, _ = make_setup()
    result = engine.run()
    assert result.equity_curve["n_positions"].max() <= 5


def test_single_name_weight_cap_respected():
    engine, _, _ = make_setup()
    result = engine.run()
    equity = result.equity_curve
    max_weight_seen = 0.0
    for row in equity.itertuples():
        if row.positions_value > 0:
            max_weight_seen = max(max_weight_seen, row.gross_exposure)
    assert max_weight_seen <= 1.0
    # 第一笔买入不得超过单票上限（含手续费容差）
    first_buy = result.trades.iloc[0]
    assert first_buy["side"] == "buy"
    assert first_buy["gross_amount"] <= 0.15 * 1_000_000.0 * 1.05


def test_cash_never_negative():
    engine, _, _ = make_setup()
    result = engine.run()
    assert (result.equity_curve["cash"] >= 0).all()
    assert result.account.cash >= 0.0


def test_t_plus_one_still_enforced_with_portfolio():
    engine, store, _ = make_setup()
    result = engine.run()
    assert len(result.orders) > 0
    for row in result.orders.itertuples():
        signal_date = __import__("pandas").Timestamp(row.signal_date).date()
        submit_date = __import__("pandas").Timestamp(row.submit_date).date()
        assert submit_date == store.calendar.next_trading_day(signal_date)


def test_rebalance_increases_turnover():
    engine_off, _, _ = make_setup(portfolio_overrides={"rebalance": False})
    result_off = engine_off.run()
    engine_on, _, _ = make_setup(portfolio_overrides={"rebalance": True})
    result_on = engine_on.run()

    trades_off = len(result_off.trades)
    trades_on = len(result_on.trades)
    assert trades_on > trades_off, (trades_on, trades_off)
    assert result_on.account.total_costs > result_off.account.total_costs


def test_max_positions_limits_holdings():
    engine, _, _ = make_setup(portfolio_overrides={"max_positions": 3})
    result = engine.run()
    assert result.equity_curve["n_positions"].max() <= 3


def test_kelly_portfolio_runs_and_reports():
    engine, _, portfolio = make_setup(
        portfolio_overrides={"kelly": {"enabled": True, "fraction": 0.5, "min_trades": 10}}
    )
    result = engine.run()
    info = portfolio.describe()
    assert 0.0 <= info["last_exposure"] <= 1.0
    assert len(result.trades) > 0
    # 凯利敞口生效后总仓位不会超过全部资金
    assert result.equity_curve["gross_exposure"].max() <= 1.0


def test_portfolio_result_is_reproducible():
    engine1, _, _ = make_setup()
    r1 = engine1.run()
    engine2, _, _ = make_setup()
    r2 = engine2.run()
    assert len(r1.trades) == len(r2.trades)
    assert r1.account.total_value == approx(r2.account.total_value)
