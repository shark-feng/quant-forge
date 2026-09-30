"""测试数据构造工具：手工构造精确可控的行情、账户与订单。

设计原则：测试用例必须**显式写出**它依赖的每一个价格、涨跌停、停牌状态，
不依赖合成数据的随机性；合成数据（``aqs.data.synthetic``）只用于端到端与压力场景。
"""

from __future__ import annotations

import shutil
import uuid
from contextlib import contextmanager
from datetime import date as _date
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import pandas as pd

from aqs.config.schema import BaseConfig, DataConfig, UniverseConfig, construct
from aqs.core.enums import OrderType, Side, SignalDirection, TimeInForce
from aqs.core.models import Bar, Order, SignalIntent
from aqs.data.store import DataStore

__all__ = [
    "DEFAULT_START",
    "PROJECT_ROOT",
    "dates",
    "bar_row",
    "make_bars",
    "flat_market",
    "make_store",
    "make_bar",
    "make_snapshot",
    "make_config",
    "make_order",
    "make_context",
    "make_risk_context",
    "buy_position",
    "price_series",
    "entry_signal",
    "exit_signal",
    "workspace_tmp",
]

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_START = "2022-03-01"


# --------------------------------------------------------------------------- #
# 日期与行情
# --------------------------------------------------------------------------- #
def dates(n: int, start: str = DEFAULT_START) -> list[_date]:
    """返回 n 个工作日（先后顺序）。"""
    return [d.date() for d in pd.bdate_range(start=start, periods=n)]


def bar_row(
    day: Any,
    symbol: str = "600000.SH",
    close: float = 10.0,
    *,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1_000_000.0,
    amount: float | None = None,
    adj_factor: float = 1.0,
    is_suspended: bool = False,
    is_st: bool = False,
    limit_up: float | None = None,
    limit_down: float | None = None,
    prev_close: float | None = None,
    list_date: Any = None,
    delist_date: Any = None,
) -> dict[str, Any]:
    """构造一行原始行情（未归一）。

    默认：``open = close``、``high = low = close``、``amount = volume × close``。
    """
    o = close if open_ is None else open_
    h = max(o, close) if high is None else high
    lo = min(o, close) if low is None else low
    if is_suspended:
        volume = 0.0
        o = h = lo = close
    return {
        "date": pd.Timestamp(day),
        "symbol": symbol,
        "open": float(o),
        "high": float(h),
        "low": float(lo),
        "close": float(close),
        "volume": float(volume),
        "amount": float(volume * close if amount is None else amount),
        "adj_factor": float(adj_factor),
        "is_suspended": bool(is_suspended),
        "is_st": bool(is_st),
        "limit_up": limit_up,
        "limit_down": limit_down,
        "prev_close": prev_close,
        "list_date": list_date,
        "delist_date": delist_date,
    }


def make_bars(rows: Iterable[Mapping[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame([dict(r) for r in rows])


def flat_market(
    symbols: Sequence[str] = ("600000.SH", "000001.SZ"),
    n: int = 40,
    price: float = 10.0,
    *,
    start: str = DEFAULT_START,
    drift: float = 0.0,
    volume: float = 1_000_000.0,
    **bar_kwargs: Any,
) -> pd.DataFrame:
    """构造价格平缓变化的行情（每个标的独立价格路径）。"""
    rows: list[dict[str, Any]] = []
    for i, sym in enumerate(symbols):
        p = price * (1.0 + 0.1 * i)
        for d in dates(n, start):
            rows.append(bar_row(d, sym, round(p, 2), volume=volume, **bar_kwargs))
            p = p * (1.0 + drift)
    return make_bars(rows)


# --------------------------------------------------------------------------- #
# 数据仓库与配置
# --------------------------------------------------------------------------- #
def make_config(overrides: Mapping[str, Any] | None = None) -> BaseConfig:
    """构造测试用主配置：默认关闭股票池过滤，避免用例被无关过滤干扰。"""
    base: dict[str, Any] = {
        "project": {"seed": 42},
        "data": {
            "provider": "synthetic",
            "quality": {"strict": False},
            "min_list_days": 0,
            "liquidity": {"window": 20, "min_amount": 0.0},
            "exclude_st": True,
            "exclude_suspended": True,
            # 测试夹具多为手工造数、通常不提供 list_date → 用 proxy 策略（显式降级，非静默）。
            # 生产默认是 strict（configs/base.yaml），两条路径都有专门用例覆盖。
            "listing_date": {"policy": "proxy"},
        },
        "universe": {"mode": "all", "fallback_to_all": True},
        "engine": {
            "initial_cash": 1_000_000.0,
            "execution_price": "open",
            "t_plus_one": True,
            "lot_size": 100,
            "max_defer_days": 5,
        },
        "costs": {"min_commission": 5.0, "impact": {"enabled": True}},
    }
    cfg = construct(BaseConfig, base)
    return cfg.with_overlay(overrides) if overrides else cfg


def make_store(
    bars: pd.DataFrame | Iterable[Mapping[str, Any]],
    *,
    config: BaseConfig | None = None,
    data_config: DataConfig | Mapping[str, Any] | None = None,
    universe_config: UniverseConfig | Mapping[str, Any] | None = None,
    index_members: pd.DataFrame | None = None,
    fundamentals: pd.DataFrame | None = None,
    validate: bool = True,
) -> DataStore:
    """构造数据仓库；默认使用「不过滤」的测试配置。"""
    frame = bars if isinstance(bars, pd.DataFrame) else make_bars(bars)
    cfg = config or make_config()
    return DataStore(
        frame,
        index_members=index_members,
        fundamentals=fundamentals,
        config=data_config if data_config is not None else cfg.data,
        universe_config=universe_config if universe_config is not None else cfg.universe,
        validate=validate,
    )


# --------------------------------------------------------------------------- #
# Bar / Snapshot / 订单
# --------------------------------------------------------------------------- #
def make_bar(
    symbol: str = "600000.SH",
    day: Any = DEFAULT_START,
    close: float = 10.0,
    *,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1_000_000.0,
    amount: float | None = None,
    adj_factor: float = 1.0,
    prev_adj_factor: float = float("nan"),
    is_suspended: bool = False,
    is_st: bool = False,
    limit_up: float | None = None,
    limit_down: float | None = None,
    prev_close: float | None = None,
    delist_date: Any = None,
    listed_days: int = 200,
) -> Bar:
    """直接构造 :class:`Bar`（用于撮合/成本等单元测试，无需经过数据层）。"""
    o = close if open_ is None else open_
    h = max(o, close) if high is None else high
    lo = min(o, close) if low is None else low
    if is_suspended:
        volume = 0.0
        o = h = lo = close
    return Bar(
        symbol=symbol,
        date=pd.Timestamp(day).date(),
        open=float(o),
        high=float(h),
        low=float(lo),
        close=float(close),
        volume=float(volume),
        amount=float(volume * close if amount is None else amount),
        adj_factor=float(adj_factor),
        prev_adj_factor=float(prev_adj_factor),
        prev_close=float("nan") if prev_close is None else float(prev_close),
        limit_up=float("nan") if limit_up is None else float(limit_up),
        limit_down=float("nan") if limit_down is None else float(limit_down),
        is_suspended=bool(is_suspended),
        is_st=bool(is_st),
        listed_days=listed_days,
        delist_date=None if delist_date is None else pd.Timestamp(delist_date).date(),
    )


def make_snapshot(
    bars: Sequence[Bar],
    *,
    day: Any = DEFAULT_START,
    phase: Any = None,
    adv_volume: Mapping[str, float] | None = None,
) -> Any:
    """构造 :class:`MarketSnapshot`。"""
    from aqs.core.enums import SessionPhase
    from aqs.core.models import MarketSnapshot

    ph = phase or SessionPhase.OPEN
    d = pd.Timestamp(day).date()
    return MarketSnapshot(
        date=d,
        phase=ph,
        bars={b.symbol: b for b in bars},
        adv_volume=dict(adv_volume or {}),
        ts=pd.Timestamp(day) + pd.Timedelta(hours=9, minutes=30),
    )


def entry_signal(symbol: str, day: Any, *, score: float = 1.0, reason: str = "test") -> SignalIntent:
    return SignalIntent(symbol=symbol, direction=SignalDirection.ENTRY, signal_date=pd.Timestamp(day).date(), score=score, reason=reason)


def price_series(
    symbol: str,
    closes: Sequence[float],
    *,
    start: str = DEFAULT_START,
    volumes: Sequence[float] | None = None,
    highs: Sequence[float] | None = None,
    lows: Sequence[float] | None = None,
    **bar_kwargs: Any,
) -> list[dict[str, Any]]:
    """按给定收盘价序列构造行情行（可指定每日成交量/最高价/最低价）。"""
    days = dates(len(closes), start)
    rows: list[dict[str, Any]] = []
    for i, close in enumerate(closes):
        vol = 1_000_000.0 if volumes is None else float(volumes[i])
        kw: dict[str, Any] = {"close": float(close), "volume": vol}
        if highs is not None:
            kw["high"] = float(highs[i])
        if lows is not None:
            kw["low"] = float(lows[i])
        kw.update(bar_kwargs)
        rows.append(bar_row(days[i], symbol, **kw))
    return rows


def make_risk_context(
    *,
    order: Any = None,
    account: Any = None,
    bars: Sequence[Any] | None = None,
    day: Any = DEFAULT_START,
    adv_volume: Mapping[str, float] | None = None,
    day_state: Any = None,
    engine_state: Any = None,
    start_value: float | None = None,
    industry_of: Any = None,
    returns_of: Any = None,
) -> Any:
    """构造风控上下文（真实订单/账户/快照 + 可控的日内状态）。"""
    from aqs.engine.account import Account
    from aqs.risk.base import DayState, EngineState, RiskContext

    acc = account if account is not None else Account(1_000_000.0)
    snapshot = make_snapshot(list(bars or [make_bar(day=day)]), day=day, adv_volume=adv_volume)
    state = day_state or DayState()
    if state.day is None:
        state.reset(day, start_value if start_value is not None else acc.total_value)
    elif start_value is not None:
        state.start_value = float(start_value)
    engine = engine_state or EngineState()
    if engine.current_value <= 0:
        engine.update_value(acc.total_value)
    return RiskContext(
        order=order if order is not None else make_order(),
        account=acc,
        snapshot=snapshot,
        day=state,
        engine=engine,
        industry_of=industry_of,
        returns_of=returns_of,
    )


def buy_position(account: Any, symbol: str, quantity: float, price: float, day: Any = DEFAULT_START) -> None:
    """给账户加一笔持仓（含成本价，可卖量已解锁）。"""
    from aqs.core.models import CostBreakdown, Fill

    account.apply_fill(
        Fill(
            fill_id=f"F-{symbol}",
            order_id=f"O-{symbol}",
            symbol=symbol,
            side=Side.BUY,
            quantity=quantity,
            price=price,
            trade_date=pd.Timestamp(day).date(),
            gross_amount=quantity * price,
            cost=CostBreakdown(),
        )
    )
    account.unlock_t1()


def make_context(
    store: DataStore,
    day: Any,
    *,
    universe: Sequence[str] | None = None,
    account: Any = None,
    phase: Any = None,
) -> Any:
    """构造策略上下文（真实 PITView + 真实快照，供策略单元测试使用）。"""
    from aqs.core.enums import SessionPhase
    from aqs.core.models import MarketSnapshot
    from aqs.engine.account import Account
    from aqs.strategy.base import StrategyContext

    ph = phase or SessionPhase.POST_CLOSE
    d = pd.Timestamp(day).date()
    snapshot = MarketSnapshot(
        date=d,
        phase=ph,
        bars=store.bars_on(d),
        adv_volume={},
        ts=store.calendar.timestamp(d, ph),
    )
    return StrategyContext(
        date=d,
        phase=ph,
        data=store.as_of(d),
        account=account if account is not None else Account(1_000_000.0),
        snapshot=snapshot,
        calendar=store.calendar,
        universe=list(universe) if universe is not None else store.universe(d),
        strategy_name="test",
    )


def exit_signal(symbol: str, day: Any, *, reason: str = "test") -> SignalIntent:
    return SignalIntent(symbol=symbol, direction=SignalDirection.EXIT, signal_date=pd.Timestamp(day).date(), reason=reason)


def make_order(
    symbol: str = "600000.SH",
    side: Side = Side.BUY,
    quantity: float = 1000.0,
    *,
    order_id: str = "T0001",
    signal_date: Any = DEFAULT_START,
    submit_date: Any = None,
    order_type: OrderType = OrderType.MARKET,
    limit_price: float | None = None,
    time_in_force: TimeInForce = TimeInForce.GTC,
    max_defer_days: int = 5,
) -> Order:
    """直接构造订单（不经过 Broker 的 T+1 校验，便于构造边界场景）。"""
    return Order(
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type,
        limit_price=limit_price,
        signal_date=pd.Timestamp(signal_date).date() if signal_date is not None else None,
        submit_date=pd.Timestamp(submit_date).date() if submit_date is not None else None,
        time_in_force=time_in_force,
        max_defer_days=max_defer_days,
        status=_submitted_status(),
    )


def _submitted_status() -> Any:
    from aqs.core.enums import OrderStatus

    return OrderStatus.SUBMITTED


# --------------------------------------------------------------------------- #
# 临时目录（写入工作区内，避免受系统临时目录权限限制）
# --------------------------------------------------------------------------- #
@contextmanager
def workspace_tmp(prefix: str = "test") -> Iterator[Path]:
    """在项目工作区下创建临时目录，退出时自动清理。"""
    root = PROJECT_ROOT / ".tmp_tests"
    root.mkdir(exist_ok=True)
    path = root / f"{prefix}_{uuid.uuid4().hex[:8]}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
