"""回测引擎端到端测试：T+1、事件顺序、涨跌停/停牌顺延、成本后收益。

这些用例是回测引擎的**验收测试**（对应合同 §八、§九）：
1. 未来函数：T 日收盘信号只能 T+1 成交，且信号层无法访问未来数据；
2. A 股硬约束：停牌/涨停无法成交则顺延、T+1 卖出限制；
3. 事件顺序：每个交易日的事件序列与设计文档一致；
4. 成本：成本加倍后交易成本同步翻倍。
"""

from __future__ import annotations

import json
from datetime import date as _date
from datetime import timedelta
from pathlib import Path

from tests.compat import approx, raises
from tests.tools import (
    bar_row,
    dates,
    entry_signal,
    exit_signal,
    make_bars,
    make_config,
    make_store,
    workspace_tmp,
)

from aqs.core.enums import OrderStatus, Side, SignalDirection
from aqs.core.exceptions import FutureFunctionError, LookaheadError
from aqs.engine.account import Account
from aqs.engine.backtest import BacktestEngine
from aqs.engine.broker import Broker
from aqs.portfolio.base import OrderPlan

D = dates(12)
SYM = "600000.SH"


# --------------------------------------------------------------------------- #
# 测试用策略与组合
# --------------------------------------------------------------------------- #
class DatedStrategy:
    """在指定交易日发出信号的最小策略（用于精确控制 T 与 T+1）。"""

    name = "dated"

    def __init__(self, entries=(), exits=()) -> None:
        self.entries = {D[i] for i in entries}
        self.exits = {D[i] for i in exits}

    def on_bar(self, ctx):
        out = []
        if ctx.date in self.entries:
            out.append(entry_signal(SYM, ctx.date, reason="entry"))
        if ctx.date in self.exits:
            out.append(exit_signal(SYM, ctx.date, reason="exit"))
        return out


class NaivePortfolio:
    """等额买入 + 有仓位则清仓的最简组合（仅用于测试引擎链路）。"""

    name = "naive"

    def __init__(self, quantity: float = 1000.0) -> None:
        self.quantity = quantity

    def generate_orders(self, signals, ctx):
        plans: list[OrderPlan] = []
        for sig in signals:
            held = ctx.account.total_quantity(sig.symbol)
            if sig.direction is SignalDirection.ENTRY and held == 0:
                plans.append(OrderPlan(sig.symbol, Side.BUY, self.quantity, tag="entry"))
            elif sig.direction is SignalDirection.EXIT and held > 0:
                plans.append(OrderPlan(sig.symbol, Side.SELL, held, tag="exit"))
        return plans


class NaughtyStrategy:
    """故意访问未来数据的策略：必须被 PITView 拦下。"""

    name = "naughty"

    def on_bar(self, ctx):
        nxt = ctx.calendar.next_trading_day(ctx.date)
        ctx.data.history(SYM, nxt, 5)  # 未来数据！
        return []


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def market(overrides: dict[int, dict] | None = None, *, n: int = 12, volume: float = 1_000_000.0):
    rows = []
    for i, d in enumerate(D[:n]):
        kw: dict = {"close": 10.0, "open_": 10.0, "prev_close": 10.0, "volume": volume}
        if overrides and i in overrides:
            kw.update(overrides[i])
        rows.append(bar_row(d, SYM, **kw))
    return make_bars(rows)


def config(**costs_over):
    over = {
        "engine": {"start": D[0], "end": D[-1], "initial_cash": 1_000_000.0, "max_defer_days": 5},
        "costs": {"impact": {"advisory_window": 5}},
    }
    cfg = make_config(over)
    if costs_over:
        cfg = cfg.with_overlay({"costs": costs_over})
    return cfg


def run_backtest(*, entries=(4,), exits=(), overrides=None, volume=1_000_000.0, cfg=None, n=12):
    store = make_store(market(overrides, n=n, volume=volume))
    engine = BacktestEngine(
        store,
        cfg or config(),
        strategy=DatedStrategy(entries=entries, exits=exits),
        portfolio=NaivePortfolio(),
    )
    return engine.run()


# --------------------------------------------------------------------------- #
# T+1：T 日收盘信号 → T+1 开盘成交
# --------------------------------------------------------------------------- #
def test_signal_fills_on_next_trading_day_open():
    res = run_backtest(entries=(4,), overrides={5: {"open_": 10.5}})
    trades = res.trades
    assert len(trades) == 1
    row = trades.iloc[0]
    assert row["symbol"] == SYM
    assert row["side"] == "buy"
    assert str(row["trade_date"]) == str(D[5])
    assert row["price"] == approx(10.5 * 1.001)


def test_no_fill_on_signal_day():
    res = run_backtest(entries=(4,), overrides={4: {"open_": 10.5}})
    trades = res.trades
    assert len(trades) == 1
    assert str(trades.iloc[0]["trade_date"]) == str(D[5])
    assert res.orders.iloc[0]["signal_date"] == str(D[4])
    assert res.orders.iloc[0]["submit_date"] == str(D[5])


def test_order_time_semantics_enforced():
    cfg = config()
    broker = Broker(Account(1_000_000.0), engine_config=cfg.engine)
    with raises(FutureFunctionError):
        broker.create_order(SYM, Side.BUY, 1000, signal_date=D[4], submit_date=D[4])
    with raises(FutureFunctionError):
        broker.create_order(SYM, Side.BUY, 1000, signal_date=D[4], submit_date=D[3])
    # 合法的 T+1
    order = broker.create_order(SYM, Side.BUY, 1000, signal_date=D[4], submit_date=D[5])
    assert order.submit_date == D[5]


def test_strategy_cannot_read_future_data():
    store = make_store(market())
    engine = BacktestEngine(store, config(), strategy=NaughtyStrategy(), portfolio=NaivePortfolio())
    with raises(LookaheadError):
        engine.run()


# --------------------------------------------------------------------------- #
# 顺延：停牌 / 涨停
# --------------------------------------------------------------------------- #
def test_suspension_defers_execution():
    res = run_backtest(entries=(4,), overrides={5: {"is_suspended": True}})
    trades = res.trades
    assert len(trades) == 1
    assert str(trades.iloc[0]["trade_date"]) == str(D[6])
    assert int(trades.iloc[0]["deferred_days"]) == 1
    assert res.broker.stats.deferred_events >= 1


def test_limit_up_defers_execution():
    overrides = {5: {"open_": 11.0, "close": 11.0, "limit_up": 11.0, "limit_down": 9.0}}
    res = run_backtest(entries=(4,), overrides=overrides)
    trades = res.trades
    assert len(trades) == 1
    assert str(trades.iloc[0]["trade_date"]) == str(D[6])
    assert res.diagnostics["match_stats"].get("limit_up", 0) == 1


def test_order_expires_after_max_defer_days():
    cfg = config()
    cfg.engine.max_defer_days = 0
    res = run_backtest(entries=(4,), overrides={5: {"is_suspended": True}}, cfg=cfg)
    assert len(res.trades) == 0
    assert res.broker.stats.expired == 1
    statuses = set(res.orders["status"].tolist())
    assert OrderStatus.EXPIRED.value in statuses


def test_partial_fill_spreads_over_days():
    res = run_backtest(entries=(4,), volume=5_000.0)
    trades = res.trades
    # 参与率 10% → 每日最多 500 股，1000 股需要两天
    assert len(trades) == 2
    assert trades["quantity"].tolist() == [500.0, 500.0]
    assert str(trades.iloc[0]["trade_date"]) == str(D[5])
    assert str(trades.iloc[1]["trade_date"]) == str(D[6])
    assert trades["order_id"].nunique() == 1


# --------------------------------------------------------------------------- #
# T+1 卖出限制
# --------------------------------------------------------------------------- #
def test_buy_then_sell_respects_t_plus_one():
    res = run_backtest(entries=(4,), exits=(5,))
    trades = res.trades
    assert len(trades) == 2
    buy, sell = trades.iloc[0], trades.iloc[1]
    assert buy["side"] == "buy" and str(buy["trade_date"]) == str(D[5])
    assert sell["side"] == "sell" and str(sell["trade_date"]) == str(D[6])


def test_sell_signal_on_buy_day_cannot_use_same_day_shares():
    """买入当日（D[5]）发出的卖出信号只能在 D[6] 成交，且不会出现 T+1 违规成交。"""
    res = run_backtest(entries=(4,), exits=(4, 5))
    dates_ = res.trades["trade_date"].astype(str).tolist()
    assert dates_ == [str(D[5]), str(D[6])]
    assert res.diagnostics["match_stats"].get("t1_lock", 0) == 0


# --------------------------------------------------------------------------- #
# 事件顺序
# --------------------------------------------------------------------------- #
def test_event_sequence_within_a_day():
    res = run_backtest(entries=(4,), exits=(5,))
    records = [r for r in res.recorder.records if r.timestamp.date() == D[5]]
    types = [r.event_type for r in records]
    assert types[:6] == ["TIMER", "MARKET_DATA", "RISK_CHECK", "FILL", "MARKET_DATA", "TIMER"]
    assert types[-3:] == ["SIGNAL", "RISK_CHECK", "ORDER"]
    timestamps = [r.timestamp for r in records]
    assert timestamps == sorted(timestamps)


def test_event_counts_match_engine_loop():
    res = run_backtest(entries=(4,))
    by_type = res.diagnostics["events_by_type"]
    assert by_type["MARKET_DATA"] == 2 * 12
    assert by_type["TIMER"] == 3 * 12
    # 第 4 天（索引 4）发出 1 次信号；第 5 天撮合产生 1 笔成交
    assert by_type["SIGNAL"] == 1
    assert by_type["FILL"] == 1
    assert by_type["ORDER"] == 1
    # 开盘撮合前风控复核 1 次 + 盘后下单风控 1 次
    assert by_type["RISK_CHECK"] == 2


def test_market_data_event_order_is_open_then_close():
    res = run_backtest(entries=())
    open_days = [r for r in res.recorder.records if "MarketData(open" in r.summary]
    close_days = [r for r in res.recorder.records if "MarketData(close" in r.summary]
    assert len(open_days) == 12 and len(close_days) == 12
    assert open_days[0].timestamp < close_days[0].timestamp


# --------------------------------------------------------------------------- #
# 账户、净值与成本
# --------------------------------------------------------------------------- #
def test_equity_curve_covers_every_session():
    res = run_backtest(entries=(4,))
    assert len(res.equity_curve) == 12
    assert str(res.equity_curve.index[0])[:10] == str(D[0])
    assert str(res.equity_curve.index[-1])[:10] == str(D[-1])
    assert res.universe_records  # 股票池统计已记录
    assert len(res.universe_records) == 12


def test_buy_reduces_cash_and_positions_increase():
    res = run_backtest(entries=(4,))
    eq = res.equity_curve
    before = eq.iloc[4]
    after = eq.iloc[6]
    assert before["n_positions"] == 0
    assert after["n_positions"] == 1
    assert after["cash"] < before["cash"]
    assert res.account.total_quantity(SYM) == 1000


def test_costs_reduce_total_value():
    res = run_backtest(entries=(4,))
    assert res.account.total_costs > 0
    # 买入后账户总资产 = 现金 + 持仓市值，且因成本低于初始资金
    assert res.account.total_value < res.account.initial_cash
    detail = res.account.cost_detail
    assert detail["commission"] > 0
    assert detail["transfer_fee"] > 0
    assert detail["slippage"] > 0


def test_cost_doubling_doubles_total_cost():
    base = run_backtest(entries=(4,))
    doubled = run_backtest(entries=(4,), cfg=config(scale=2.0))
    ratio = doubled.account.total_costs / base.account.total_costs
    assert abs(ratio - 2.0) < 0.02, ratio
    assert doubled.account.total_value < base.account.total_value


def test_impact_cost_present_when_adv_available():
    res = run_backtest(entries=(4,))
    assert res.account.cost_detail.get("impact", 0.0) > 0
    assert res.diagnostics["cost_missing_adv"] == 0


def test_orders_on_last_day_are_dropped_not_executed():
    res = run_backtest(entries=(11,))
    assert len(res.trades) == 0
    assert res.diagnostics["orders_dropped_no_next_day"] == 1


# --------------------------------------------------------------------------- #
# 结果导出与摘要
# --------------------------------------------------------------------------- #
def test_trades_frame_columns():
    res = run_backtest(entries=(4,))
    for col in ("fill_id", "order_id", "symbol", "side", "quantity", "price", "trade_date", "total_cost", "cost_commission"):
        assert col in res.trades.columns


def test_summary_and_save():
    res = run_backtest(entries=(4,))
    summary = res.summary()
    assert summary["account_total_value"] > 0
    assert summary["broker_filled"] == 1
    assert "diagnostics" in summary
    assert summary["diagnostics"]["t_plus_one"] is True
    # M5 之后默认启用 RMS（配置 configs/risk.yaml）
    assert summary["diagnostics"]["risk_engine"] == "RuleRiskEngine"
    assert summary["diagnostics"]["risk_enabled"] is True
    assert "risk" in summary["diagnostics"]
    assert "reject_rate" in summary["diagnostics"]["risk"]["stats"]

    with workspace_tmp("backtest_report") as tmp:
        written = res.save(tmp)
        assert set(written) >= {"equity_curve", "trades", "orders", "summary", "universe_stats"}
        for path in written.values():
            assert Path(path).exists()
        payload = json.loads(Path(written["summary"]).read_text(encoding="utf-8"))
        assert payload["n_trades"] == 1


def test_backtest_without_strategy_runs_clean():
    store = make_store(market())
    engine = BacktestEngine(store, config())
    res = engine.run()
    assert len(res.trades) == 0
    assert len(res.equity_curve) == 12
    assert res.account.total_value == approx(res.account.initial_cash)


def test_empty_range_rejected():
    from aqs.core.exceptions import EngineError

    store = make_store(market())
    engine = BacktestEngine(store, config())
    with raises(EngineError):
        engine.run(start=D[0], end=D[0] - timedelta(days=10))


# --------------------------------------------------------------------------- #
# 回归：现金约束
# --------------------------------------------------------------------------- #
class GreedyPortfolio:
    """故意「贪心」的组合：每笔买单一律按可用现金的 98% 下单。

    这是最容易触发「现金透支」的写法（多笔订单共用同一份现金估算）。
    撮合层必须逐笔按**剩余现金**重新计算可买数量，包括冲击成本。
    """

    name = "greedy"

    def __init__(self, ratio: float = 0.98) -> None:
        self.ratio = ratio

    def generate_orders(self, signals, ctx):
        plans = []
        for sig in signals:
            bar = ctx.snapshot.bar(sig.symbol)
            if bar is None or bar.close <= 0:
                continue
            budget = min(ctx.account.total_value, ctx.account.available_cash()) * self.ratio
            qty = int(budget / bar.close // 100) * 100
            if qty >= 100:
                plans.append(OrderPlan(sig.symbol, Side.BUY, qty, tag="greedy"))
        return plans


def test_multiple_buy_orders_same_day_never_overdraw_cash():
    """回归用例：同一交易日多笔买单同时撮合，现金不得为负。"""
    symbols = ["600000.SH", "000001.SZ", "600001.SH"]
    rows = []
    for i, d in enumerate(D):
        for sym in symbols:
            rows.append(bar_row(d, sym, close=10.0, open_=10.0, prev_close=10.0, volume=200_000.0))
    store = make_store(make_bars(rows))
    cfg = config()
    engine = BacktestEngine(
        store,
        cfg,
        strategy=DatedStrategy(entries=(4,)),
        portfolio=GreedyPortfolio(),
    )
    # 策略只对 SYM 发信号，这里手工替换为对三个标的都发信号
    signals = [entry_signal(sym, D[4]) for sym in symbols]
    engine.strategy = lambda ctx: signals if ctx.date == D[4] else []

    result = engine.run()
    assert result.account.cash >= 0.0
    assert len(result.trades) > 0
    # 第一笔几乎用光现金，后续订单必须被削减
    quantities = result.trades["quantity"].tolist()
    assert quantities[0] > quantities[-1]


def test_cash_never_negative_across_whole_backtest():
    res = run_backtest(entries=(4,), exits=(5,))
    assert (res.equity_curve["cash"] >= 0).all()
    assert res.account.cash >= 0.0
