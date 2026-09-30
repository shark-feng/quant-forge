"""确定性合成行情生成器（测试、演示、压力场景）。

为什么需要它：真实 A 股数据受版权与网络限制，而单元测试必须覆盖
**停牌 / 涨跌停 / ST / 退市 / 上市不足 / 复权事件 / 指数成分变更** 等极端情形。
本模块用固定随机种子生成可复现的合成市场，使所有偏差测试都能稳定复现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from .loader import MarketDataBundle
from .schema import infer_board, normalize_bars, normalize_fundamentals, normalize_index_members

__all__ = ["SyntheticMarketConfig", "generate_market_data", "CRISIS_WINDOWS"]

# 内置压力场景（仅当与生成区间重叠时生效）
CRISIS_WINDOWS: dict[str, tuple[str, str, float]] = {
    "gfc_2008": ("2008-01-02", "2008-12-31", -0.0028),
    "crash_2015": ("2015-06-12", "2015-09-30", -0.0060),
    "covid_2020": ("2020-01-20", "2020-03-31", -0.0035),
}


@dataclass(slots=True)
class SyntheticMarketConfig:
    """合成市场参数。"""

    n_symbols: int = 30
    start: str = "2018-01-01"
    end: str = "2023-12-31"
    seed: int = 20240101
    initial_price: float = 10.0
    annual_drift: float = 0.06
    annual_vol: float = 0.30
    dividend_prob: float = 0.012          # 每日发生除权的概率
    dividend_size: float = 0.03           # 除权比例
    suspend_prob: float = 0.004           # 每日停牌概率
    st_ratio: float = 0.08                # ST 标的比例
    delist_ratio: float = 0.10            # 退市标的比例
    new_list_ratio: float = 0.15          # 中途上市标的比例
    index_code: str = "000300.SH"
    index_size: int = 15                  # 初始指数成分数量
    rebalance_months: tuple[int, ...] = (1, 7)
    base_daily_amount: float = 300_000_000.0   # 日均成交额（元）
    seed_price_scale: tuple[float, float] = (0.5, 3.0)
    old_listing_offset_days: int = 400    # 「老股」的上市日 = 数据起点 - 该天数（缺陷 #8/#12）
    industries: tuple[str, ...] = (        # 供 RMS 行业暴露规则使用（缺陷 #12）
        "银行",
        "食品饮料",
        "医药生物",
        "电子",
        "房地产",
        "有色金属",
    )
    crisis_windows: Mapping[str, tuple[str, str, float]] = field(default_factory=lambda: dict(CRISIS_WINDOWS))
    include_fundamentals: bool = True
    include_index_history: bool = True


def _trading_days(start: str, end: str) -> list[pd.Timestamp]:
    """工作日日历（合成数据用；真实日历应由行情数据派生）。

    说明：此处不含中国法定节假日，仅用于测试与演示；真实回测请使用 ``TradingCalendar.from_bars``。
    """
    days = pd.bdate_range(start=start, end=end)
    return [d.normalize() for d in days]


_BOARD_FAMILIES: tuple[tuple[str, str, int], ...] = (
    # (代码前缀, 交易所后缀, 相对权重) —— 权重贴近真实市场构成
    ("600", "SH", 6),   # 沪市主板
    ("000", "SZ", 6),   # 深市主板
    ("300", "SZ", 2),   # 创业板
    ("688", "SH", 2),   # 科创板
)


def _symbol_codes(n: int, rng: np.random.Generator | None = None) -> list[str]:
    """生成混合板块的证券代码（确定性、唯一、合法）。

    修复（缺陷 #12）：旧实现按 ``i % 10`` 拼代码并用
    ``s.replace(".", f"{rng.integers(1,9)}.", 1)`` 去重，一旦重复就会产出
    ``6000005..SH`` 这类非法代码。现改为**按板块族各自递增分配**，
    从构造上消除重复（``rng`` 参数保留以兼容旧调用签名）。
    """
    if n < 0:
        raise ValueError("n 不能为负")
    order: list[int] = []
    for family_idx, (_, _, weight) in enumerate(_BOARD_FAMILIES):
        order.extend([family_idx] * weight)
    counters = [1] * len(_BOARD_FAMILIES)  # 从 001 开始（000000 非法）
    out: list[str] = []
    i = 0
    while len(out) < n:
        family_idx = order[i % len(order)]
        prefix, suffix, _ = _BOARD_FAMILIES[family_idx]
        code = f"{prefix}{counters[family_idx]:03d}"
        counters[family_idx] += 1
        out.append(f"{code}.{suffix}")
        i += 1
    return out


def generate_market_data(
    *,
    start: Any = None,
    end: Any = None,
    symbols: Sequence[str] | None = None,
    config: SyntheticMarketConfig | None = None,
    n_symbols: int | None = None,
    seed: int | None = None,
    crisis: Sequence[str] | None = None,
    **overrides: Any,
) -> MarketDataBundle:
    """生成合成市场数据。

    Args:
        start/end: 覆盖生成区间。
        symbols: 指定标的（会覆盖 n_symbols 的自动生成）。
        config: 完整参数；其余关键字会覆盖其中字段。
        crisis: 只启用指定的压力场景（默认全部）。
    """
    cfg = config or SyntheticMarketConfig()
    if start is not None:
        cfg.start = str(pd.Timestamp(start).date())
    if end is not None:
        cfg.end = str(pd.Timestamp(end).date())
    if n_symbols is not None:
        cfg.n_symbols = int(n_symbols)
    if seed is not None:
        cfg.seed = int(seed)
    for key, value in overrides.items():
        if not hasattr(cfg, key):
            raise TypeError(f"未知的合成数据参数：{key}")
        setattr(cfg, key, value)

    rng = np.random.default_rng(cfg.seed)
    days = _trading_days(cfg.start, cfg.end)
    if not days:
        raise ValueError("合成数据区间内没有交易日")
    n_days = len(days)
    syms = list(symbols) if symbols else _symbol_codes(cfg.n_symbols, rng)
    n_sym = len(syms)

    # ------------------------- 压力场景漂移 ------------------------- #
    drift = np.full(n_days, cfg.annual_drift / 252.0)
    vol = np.full(n_days, cfg.annual_vol / np.sqrt(252.0))
    active_crisis = list(cfg.crisis_windows) if crisis is None else list(crisis)
    day_ts = pd.DatetimeIndex(days)
    for name in active_crisis:
        if name not in cfg.crisis_windows:
            continue
        c_start, c_end, c_drift = cfg.crisis_windows[name]
        mask = (day_ts >= pd.Timestamp(c_start)) & (day_ts <= pd.Timestamp(c_end))
        if mask.any():
            drift[mask] += c_drift
            vol[mask] *= 1.6

    # ------------------------- 标的属性 ------------------------- #
    st_flags = rng.random(n_sym) < cfg.st_ratio
    delist_flags = rng.random(n_sym) < cfg.delist_ratio
    new_list_flags = rng.random(n_sym) < cfg.new_list_ratio
    price_scale = rng.uniform(*cfg.seed_price_scale, size=n_sym)

    list_dates: list[pd.Timestamp | pd.NaT] = []
    delist_dates: list[pd.Timestamp | pd.NaT] = []
    first_idx: list[int] = []
    last_idx: list[int] = []
    industries: list[str] = []
    for i in range(n_sym):
        if new_list_flags[i] and n_days > 120:
            f = int(rng.integers(60, max(61, n_days - 60)))
        else:
            f = 0
        if delist_flags[i] and n_days - f > 120:
            l = int(rng.integers(f + 60, n_days - 1))
        else:
            l = n_days - 1
        first_idx.append(f)
        last_idx.append(l)
        # 上市日必须真实存在（缺陷 #8/#12）：新上市股取区间内日期；
        # 老股取数据起点之前的一段日期（真实市场里老股上市日远早于回测窗口）
        list_dates.append(
            days[f] if f > 0 else days[0] - pd.Timedelta(days=cfg.old_listing_offset_days)
        )
        delist_dates.append(days[l] if l < n_days - 1 else pd.NaT)
        industries.append(cfg.industries[int(rng.integers(0, len(cfg.industries)))])

    # ------------------------- 价格路径 ------------------------- #
    records: list[pd.DataFrame] = []
    for i, sym in enumerate(syms):
        first, last = first_idx[i], last_idx[i]
        m = last - first + 1
        if m <= 0:
            continue
        r = rng.normal(drift[first : last + 1], vol[first : last + 1], size=m)
        # 偶发跳空（消息冲击）
        jumps = rng.random(m) < 0.004
        r[jumps] += rng.normal(0.0, 0.05, size=int(jumps.sum()))

        board = infer_board(sym)
        pct = 0.20 if board.value in ("gem", "star") else (0.05 if st_flags[i] else 0.10)

        # 复权因子（除权事件）
        div = rng.random(m) < cfg.dividend_prob
        div_ratio = np.where(div, 1.0 + cfg.dividend_size, 1.0)
        adj_factor = np.cumprod(div_ratio)

        close = np.zeros(m)
        prev = cfg.initial_price * price_scale[i]
        for t in range(m):
            raw = prev * (1.0 + r[t])
            limit_up = round(prev * (1.0 + pct) + 1e-9, 2)
            limit_down = round(prev * (1.0 - pct) + 1e-9, 2)
            # 涨跌停封板：超过限制的价格被截断在板上
            if raw > limit_up:
                raw = limit_up
            elif raw < limit_down:
                raw = limit_down
            close[t] = round(raw + 1e-9, 2)
            prev = close[t]

        open_ = np.empty(m)
        high = np.empty(m)
        low = np.empty(m)
        for t in range(m):
            p_prev = close[t - 1] if t > 0 else close[t]
            l_up = round(p_prev * (1.0 + pct) + 1e-9, 2)
            l_dn = round(p_prev * (1.0 - pct) + 1e-9, 2)
            if close[t] >= l_up and t > 0:
                # 一字涨停（无法买入）
                open_[t] = l_up
            elif close[t] <= l_dn and t > 0:
                open_[t] = l_dn
            else:
                gap = rng.normal(0.0, 0.006)
                open_[t] = np.clip(round(p_prev * (1.0 + gap) + 1e-9, 2), l_dn, l_up)
            hi = max(open_[t], close[t]) * (1.0 + abs(rng.normal(0.0, 0.006)))
            lo = min(open_[t], close[t]) * (1.0 - abs(rng.normal(0.0, 0.006)))
            high[t] = round(min(max(hi, open_[t], close[t]), l_up) + 1e-9, 2)
            low[t] = round(max(min(lo, open_[t], close[t]), l_dn) + 1e-9, 2)

        # 成交量与成交额
        base_vol = cfg.base_daily_amount / np.maximum(close, 1.0)
        volume = np.round(base_vol * np.exp(rng.normal(0.0, 0.5, size=m)) / 100.0) * 100.0

        # 停牌
        suspended = rng.random(m) < cfg.suspend_prob
        suspended[0] = False
        close[suspended] = np.concatenate([[close[0]], close[:-1]])[suspended]
        open_[suspended] = close[suspended]
        high[suspended] = close[suspended]
        low[suspended] = close[suspended]
        volume[suspended] = 0.0
        amount = volume * ((open_ + close) / 2.0)

        frame = pd.DataFrame(
            {
                "date": days[first : last + 1],
                "symbol": sym,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": np.round(amount, 2),
                "adj_factor": adj_factor,
                "is_suspended": suspended,
                "is_st": bool(st_flags[i]),
                "list_date": list_dates[i],
                "delist_date": delist_dates[i],
                "industry": industries[i],
            }
        )
        records.append(frame)

    bars_raw = pd.concat(records, ignore_index=True)
    bars = normalize_bars(bars_raw)

    # ------------------------- 指数历史成分 ------------------------- #
    members = None
    if cfg.include_index_history:
        rows: list[dict[str, Any]] = []
        tradable_syms = [s for s in syms if s in set(bars["symbol"])]
        init = tradable_syms[: min(cfg.index_size, len(tradable_syms))]
        eligible_from = {s: days[first_idx[syms.index(s)]] for s in tradable_syms}
        delist_of = {s: delist_dates[syms.index(s)] for s in tradable_syms}
        for s in init:
            rows.append(
                {"index_code": cfg.index_code, "symbol": s, "effective_from": days[0], "effective_to": pd.NaT}
            )
        # 定期调整：部分退出、部分纳入
        current = set(init)
        pool = [s for s in tradable_syms if s not in current]
        for month in cfg.rebalance_months:
            for ts in day_ts[day_ts.month == month]:
                if not pool:
                    break
                if rng.random() < 0.6:
                    continue
                add = str(pool.pop(0))
                # 必须排序：``current`` 是 set，Python 的 str 哈希默认加盐（PYTHONHASHSEED 随机），
                # 直接迭代 set 会让 ``rng.choice`` 在不同进程中抽到不同标的，
                # 导致「同一 seed 生成不同指数成分历史」——回测结果不可复现（缺陷 #14）。
                cur_list = sorted(s for s in current if not _is_delisted(delist_of[s], ts))
                if not cur_list:
                    break
                drop = str(rng.choice(cur_list))
                for row in rows:
                    if row["symbol"] == drop and pd.isna(row["effective_to"]):
                        row["effective_to"] = ts - pd.Timedelta(days=1)
                rows.append(
                    {
                        "index_code": cfg.index_code,
                        "symbol": add,
                        "effective_from": ts,
                        "effective_to": pd.NaT,
                    }
                )
                current.discard(drop)
                current.add(add)
        # 退市即剔除
        for row in rows:
            s = row["symbol"]
            d = delist_of.get(s)
            if d is not None and not pd.isna(d) and pd.isna(row["effective_to"]):
                row["effective_to"] = d
        members = normalize_index_members(pd.DataFrame(rows))

    # ------------------------- 财务数据（含公告日） ------------------------- #
    fundamentals = None
    if cfg.include_fundamentals:
        frows: list[dict[str, Any]] = []
        for i, sym in enumerate(syms):
            for year in range(pd.Timestamp(cfg.start).year - 1, pd.Timestamp(cfg.end).year + 1):
                for mmdd, lag in (("03-31", 30), ("06-30", 45), ("09-30", 30), ("12-31", 90)):
                    rp = pd.Timestamp(f"{year}-{mmdd}")
                    if rp < pd.Timestamp(cfg.start) - pd.Timedelta(days=400) or rp > pd.Timestamp(cfg.end):
                        continue
                    announce = rp + pd.Timedelta(days=int(lag + rng.integers(0, 15)))
                    frows.append(
                        {
                            "symbol": sym,
                            "report_period": rp,
                            "announce_date": announce,
                            "roe": float(rng.normal(0.10, 0.05)),
                            "net_profit": float(rng.normal(5e8, 1e8)),
                            "revenue": float(rng.normal(5e9, 1e9)),
                            "total_assets": float(abs(rng.normal(2e10, 5e9))),
                            "total_equity": float(abs(rng.normal(8e9, 2e9))),
                        }
                    )
        fundamentals = normalize_fundamentals(pd.DataFrame(frows))

    return MarketDataBundle(
        bars=bars,
        index_members=members,
        fundamentals=fundamentals,
        meta={
            "provider": "synthetic",
            "seed": cfg.seed,
            "n_symbols": len(syms),
            "n_days": n_days,
            "crisis": active_crisis,
        },
    )


def _is_delisted(delist: Any, ts: pd.Timestamp) -> bool:
    return delist is not None and not pd.isna(delist) and ts > delist
