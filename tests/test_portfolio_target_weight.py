"""目标权重组合测试：订单生成、约束、卖出/调仓、T+1、凯利敞口。"""

from __future__ import annotations

from tests.compat import approx
from tests.tools import bar_row, dates, make_bars, make_context, make_store

from aqs.core.enums import Side
from aqs.core.models import CostBreakdown, Fill
from aqs.engine.account import Account
from aqs.portfolio.sizing import KellySizing, SizingConfig
from aqs.portfolio.target_weight import TargetWeightPortfolio
from tests.tools import entry_signal, exit_signal

D = dates(6)
DAY = D[3]
SYMS = ["600000.SH", "600001.SH", "600002.SH", "600003.SH", "600004.SH", "600005.SH"]


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def make_market(symbols=SYMS, *, price: float = 10.0, n: int = 6):
    rows = []
    for d in D[:n]:
        for i, sym in enumerate(symbols):
            rows.append(bar_row(d, sym, close=price + i, open_=price + i, prev_close=price + i, volume=1_000_000.0))
    return make_store(make_bars(rows))


def buy_position(account: Account, symbol: str, quantity: float, price: float, day=DAY, unlock: bool = True) -> None:
    fill = Fill(
        fill_id=f"F-{symbol}",
        order_id=f"O-{symbol}",
        symbol=symbol,
        side=Side.BUY,
        quantity=quantity,
        price=price,
        trade_date=day,
        gross_amount=quantity * price,
        cost=CostBreakdown(),
    )
    account.apply_fill(fill)
    if unlock:
        account.unlock_t1()


def make_portfolio(**overrides) -> TargetWeightPortfolio:
    base = dict(
        weighting="equal",
        max_positions=10,
        max_weight_per_symbol=0.10,
        cash_buffer=0.02,
        allow_reentry=True,
        rebalance=False,
    )
    base.update(overrides)
    return TargetWeightPortfolio(SizingConfig(**base))


def signals(symbols, direction="entry", score=1.0):
    if direction == "entry":
        return [entry_signal(s, DAY, score=score if score is None else score) for s in symbols]
    return [exit_signal(s, DAY) for s in symbols]


def planned_notional(plans, store=None, price_map=None) -> float:
    total = 0.0
    for plan in plans:
        price = (price_map or {}).get(plan.symbol, 10.0)
        total += plan.quantity * price
    return total


# --------------------------------------------------------------------------- #
# 买入
# --------------------------------------------------------------------------- #
def test_single_entry_produces_lot_sized_buy():
    store = make_market()
    account = Account(1_000_000.0)
    ctx = make_context(store, DAY, account=account, universe=SYMS)
    portfolio = make_portfolio()
    plans = portfolio.generate_orders(signals(["600000.SH"]), ctx)

    assert len(plans) == 1
    plan = plans[0]
    assert plan.side is Side.BUY
    # 单票上限 10% × 100 万 = 10 万；价格 10 元 → 1 万股
    assert plan.quantity == 10_000
    assert plan.quantity % 100 == 0


def test_max_weight_per_symbol_respected():
    store = make_market()
    account = Account(1_000_000.0)
    ctx = make_context(store, DAY, account=account, universe=SYMS)
    portfolio = make_portfolio(max_weight_per_symbol=0.05)
    plans = portfolio.generate_orders(signals(["600000.SH"]), ctx)
    assert plans[0].quantity == 5_000  # 5% × 100 万 / 10 元


def test_max_positions_limits_new_positions():
    store = make_market()
    account = Account(1_000_000.0)
    ctx = make_context(store, DAY, account=account, universe=SYMS)
    portfolio = make_portfolio(max_positions=3)
    sigs = [entry_signal(s, DAY, score=float(i)) for i, s in enumerate(SYMS)]
    plans = portfolio.generate_orders(sigs, ctx)
    assert len(plans) == 3
    # 按 score 降序取前 3
    assert {p.symbol for p in plans} == set(SYMS[3:])


def test_held_positions_count_toward_max_positions():
    store = make_market()
    account = Account(1_000_000.0)
    buy_position(account, SYMS[0], 1000, 10.0)
    buy_position(account, SYMS[1], 1000, 11.0)
    ctx = make_context(store, DAY, account=account, universe=SYMS)
    portfolio = make_portfolio(max_positions=3)
    plans = portfolio.generate_orders(signals(SYMS), ctx)
    assert len(plans) == 1  # 已有 2 个持仓 → 只能再开 1 个


def test_cash_buffer_limits_total_investment():
    store = make_market()
    account = Account(1_000_000.0)
    ctx = make_context(store, DAY, account=account, universe=SYMS)
    portfolio = make_portfolio(cash_buffer=0.02)
    plans = portfolio.generate_orders(signals(SYMS), ctx)
    price_map = {sym: 10.0 + i for i, sym in enumerate(SYMS)}
    notional = sum(p.quantity * price_map[p.symbol] for p in plans)
    assert notional <= 1_000_000.0 * 0.98 + 1e-6


def test_cash_buffer_zero_invests_everything_available():
    store = make_market(symbols=SYMS[:2])
    account = Account(1_000_000.0)
    ctx = make_context(store, DAY, account=account, universe=SYMS[:2])
    portfolio = make_portfolio(cash_buffer=0.0, max_weight_per_symbol=0.50)
    plans = portfolio.generate_orders(signals(SYMS[:2]), ctx)
    price_map = {SYMS[0]: 10.0, SYMS[1]: 11.0}
    notions = [p.quantity * price_map[p.symbol] for p in plans]
    # 每个 50% × 100 万 = 50 万；受整手约束，总额略低于 100 万
    assert sum(notions) <= 1_000_000.0 + 1e-6
    assert sum(notions) >= 995_000.0


def test_held_symbol_is_not_bought_again():
    store = make_market()
    account = Account(1_000_000.0)
    buy_position(account, SYMS[0], 1000, 10.0)
    ctx = make_context(store, DAY, account=account, universe=SYMS)
    portfolio = make_portfolio()
    plans = portfolio.generate_orders(signals([SYMS[0]]), ctx)
    assert plans == []


def test_missing_price_symbol_skipped():
    store = make_market(symbols=SYMS[:2])
    account = Account(1_000_000.0)
    ctx = make_context(store, DAY, account=account, universe=SYMS)
    portfolio = make_portfolio()
    plans = portfolio.generate_orders(signals(["999999.SH", SYMS[0]]), ctx)
    assert {p.symbol for p in plans} == {SYMS[0]}


# --------------------------------------------------------------------------- #
# 卖出 / 调仓 / T+1
# --------------------------------------------------------------------------- #
def test_exit_signal_sells_sellable_quantity():
    store = make_market()
    account = Account(1_000_000.0)
    buy_position(account, SYMS[0], 1500, 10.0)
    ctx = make_context(store, DAY, account=account, universe=SYMS)
    portfolio = make_portfolio()
    plans = portfolio.generate_orders(signals([SYMS[0]], direction="exit"), ctx)
    assert len(plans) == 1
    assert plans[0].side is Side.SELL
    assert plans[0].quantity == 1500
    assert plans[0].tag == "exit"


def test_t_plus_one_blocked_position_produces_no_sell():
    store = make_market()
    account = Account(1_000_000.0)
    buy_position(account, SYMS[0], 1000, 10.0, unlock=False)  # 当日买入，T+1 未解锁
    ctx = make_context(store, DAY, account=account, universe=SYMS)
    portfolio = make_portfolio()
    plans = portfolio.generate_orders(signals([SYMS[0]], direction="exit"), ctx)
    assert plans == []  # 可卖为 0 → 不生成订单（引擎无需拒单）


def test_rebalance_disabled_keeps_old_positions():
    store = make_market()
    account = Account(1_000_000.0)
    buy_position(account, SYMS[0], 1000, 10.0)
    ctx = make_context(store, DAY, account=account, universe=SYMS)
    portfolio = make_portfolio(rebalance=False)
    plans = portfolio.generate_orders(signals([SYMS[1]]), ctx)
    assert all(p.side is Side.BUY for p in plans)
    assert all(p.symbol != SYMS[0] for p in plans)


def test_rebalance_enabled_sells_non_targets():
    store = make_market()
    account = Account(1_000_000.0)
    buy_position(account, SYMS[0], 1000, 10.0)
    ctx = make_context(store, DAY, account=account, universe=SYMS)
    portfolio = make_portfolio(rebalance=True)
    plans = portfolio.generate_orders(signals([SYMS[1]]), ctx)
    sells = [p for p in plans if p.side is Side.SELL]
    assert len(sells) == 1 and sells[0].symbol == SYMS[0]
    assert sells[0].tag == "rebalance"


def test_sells_come_before_buys():
    store = make_market()
    account = Account(1_000_000.0)
    buy_position(account, SYMS[0], 1000, 10.0)
    ctx = make_context(store, DAY, account=account, universe=SYMS)
    portfolio = make_portfolio(rebalance=True)
    plans = portfolio.generate_orders(
        [exit_signal(SYMS[0], DAY), entry_signal(SYMS[1], DAY)], ctx
    )
    sides = [p.side for p in plans]
    assert sides == [Side.SELL, Side.BUY]


def test_allow_reentry_false_blocks_second_entry():
    store = make_market()
    account = Account(1_000_000.0)
    buy_position(account, SYMS[0], 1000, 10.0)
    ctx = make_context(store, DAY, account=account, universe=SYMS)
    portfolio = make_portfolio(allow_reentry=False, rebalance=False)

    # 第一次卖出 → 记录已交易标的
    sells = portfolio.generate_orders(signals([SYMS[0]], direction="exit"), ctx)
    assert len(sells) == 1
    account.apply_fill(
        Fill("F2", "O2", SYMS[0], Side.SELL, 1000, 10.0, DAY, gross_amount=10_000.0, cost=CostBreakdown())
    )
    # 再次出现买入信号 → 被 allow_reentry=False 拦住
    ctx2 = make_context(store, D[4], account=account, universe=SYMS)
    assert portfolio.generate_orders(signals([SYMS[0]]), ctx2) == []


# --------------------------------------------------------------------------- #
# 凯利敞口
# --------------------------------------------------------------------------- #
def test_kelly_exposure_scales_down_buys():
    store = make_market()
    account = Account(1_000_000.0)
    # 构造 20 笔平仓记录：10 笔 +15%、10 笔 -5% → 胜率 0.5、赔率 3
    # → f* = 0.5 - 0.5/3 = 0.3333，半凯利敞口 = 0.1667
    from aqs.core.models import Fill as _Fill

    for i in range(20):
        qty, price = 1000.0, 10.0
        buy_position(account, SYMS[0], qty, price)
        sell_price = price * (1.15 if i % 2 == 0 else 0.95)
        account.apply_fill(
            _Fill(
                f"S{i}",
                f"O{i}",
                SYMS[0],
                Side.SELL,
                qty,
                sell_price,
                DAY,
                gross_amount=qty * sell_price,
                cost=CostBreakdown(),
            )
        )
        account.unlock_t1()

    ctx = make_context(store, DAY, account=account, universe=SYMS)
    plain = make_portfolio()
    kelly = make_portfolio(kelly=KellySizing(enabled=True, fraction=0.5, cap=1.0, min_trades=20))
    targets = [SYMS[1], SYMS[2], SYMS[3]]
    price_map = {sym: 10.0 + i for i, sym in enumerate(SYMS)}

    plans_plain = plain.generate_orders(signals(targets), ctx)
    plans_kelly = kelly.generate_orders(signals(targets), ctx)

    assert plans_plain and plans_kelly
    notional_plain = sum(p.quantity * price_map[p.symbol] for p in plans_plain)
    notional_kelly = sum(p.quantity * price_map[p.symbol] for p in plans_kelly)
    ratio = notional_kelly / notional_plain
    # 半凯利敞口 ≈ 16.7% → 总仓位约降至满仓的一半
    assert 0.4 < ratio < 0.65, ratio
    info = kelly.describe()
    assert info["last_kelly"]["ready"] is True
    assert info["last_exposure"] == approx(0.5 * (0.5 - 0.5 / 3.0))
    assert info["last_kelly"]["stats_win_rate"] == approx(0.5)
    assert info["last_kelly"]["stats_payoff_ratio"] == approx(3.0)


def test_kelly_disabled_means_full_exposure():
    store = make_market()
    account = Account(1_000_000.0)
    ctx = make_context(store, DAY, account=account, universe=SYMS)
    portfolio = make_portfolio()
    portfolio.generate_orders(signals([SYMS[0]]), ctx)
    assert portfolio.describe()["last_exposure"] == approx(1.0)
    assert portfolio.describe()["last_kelly"] is None


# --------------------------------------------------------------------------- #
# 其它
# --------------------------------------------------------------------------- #
def test_no_signals_returns_empty():
    store = make_market()
    ctx = make_context(store, DAY, account=Account(1_000_000.0), universe=SYMS)
    assert make_portfolio().generate_orders([], ctx) == []


def test_score_prop_weighting():
    store = make_market()
    account = Account(1_000_000.0)
    ctx = make_context(store, DAY, account=account, universe=SYMS)
    portfolio = make_portfolio(weighting="score_prop", max_weight_per_symbol=0.90)
    plans = portfolio.generate_orders(
        [entry_signal(SYMS[0], DAY, score=3.0), entry_signal(SYMS[1], DAY, score=1.0)], ctx
    )
    by_symbol = {p.symbol: p for p in plans}
    price_map = {sym: 10.0 + i for i, sym in enumerate(SYMS)}
    # 权重 0.735 : 0.245 = 3:1（金额比例，数量会因价格不同而不同）
    n0 = by_symbol[SYMS[0]].quantity * price_map[SYMS[0]]
    n1 = by_symbol[SYMS[1]].quantity * price_map[SYMS[1]]
    assert n0 > n1
    assert 2.95 < n0 / n1 < 3.05


def test_last_targets_exposed_for_reporting():
    store = make_market()
    ctx = make_context(store, DAY, account=Account(1_000_000.0), universe=SYMS)
    portfolio = make_portfolio()
    portfolio.generate_orders(signals([SYMS[0]]), ctx)
    targets = portfolio.last_targets
    assert len(targets) == 1
    assert targets[0].symbol == SYMS[0]
    assert targets[0].weight == approx(0.10)


def test_describe_contains_config():
    info = make_portfolio(max_positions=5).describe()
    assert info["max_positions"] == 5
    assert info["weighting"] == "equal"
    assert info["kelly"]["enabled"] is False
