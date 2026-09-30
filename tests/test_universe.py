"""股票池测试：ST / 停牌 / 上市天数 / 流动性 / 历史指数成分 / 幸存者偏差。"""

from __future__ import annotations

import pandas as pd

from tests.compat import approx
from tests.tools import bar_row, dates, make_bars, make_store

from aqs.config.schema import DataConfig, UniverseConfig, construct

D = dates(80)

SYM_OK = "600000.SH"
SYM_ST = "600001.SH"
SYM_SUSP = "600002.SH"
SYM_NEW = "600003.SH"
SYM_ILLIQ = "600004.SH"
SYM_DELISTED = "000005.SZ"

TARGET = D[79]
STRICT_DATA = {
    "quality": {"strict": False},
    "min_list_days": 60,
    "liquidity": {"window": 20, "min_amount": 50_000_000.0},
    "exclude_st": True,
    "exclude_suspended": True,
}


def _market() -> pd.DataFrame:
    rows = []
    for i, d in enumerate(D):
        rows.append(bar_row(d, SYM_OK, close=10.0, amount=100_000_000.0, list_date=D[0]))
        rows.append(bar_row(d, SYM_ST, close=5.0, amount=100_000_000.0, is_st=True, list_date=D[0]))
        rows.append(
            bar_row(
                d,
                SYM_SUSP,
                close=8.0,
                amount=100_000_000.0,
                list_date=D[0],
                is_suspended=(i == 79),
            )
        )
        if i >= 65:  # 上市仅 15 个交易日
            rows.append(bar_row(d, SYM_NEW, close=12.0, amount=100_000_000.0, list_date=D[65]))
        rows.append(bar_row(d, SYM_ILLIQ, close=3.0, amount=1_000_000.0, list_date=D[0]))
        if i <= 70:  # 在 D[70] 退市
            rows.append(
                bar_row(d, SYM_DELISTED, close=6.0, amount=100_000_000.0, list_date=D[0], delist_date=D[70])
            )
    return make_bars(rows)


def _store(**overrides):
    cfg = dict(STRICT_DATA)
    cfg.update(overrides)
    return make_store(_market(), data_config=construct(DataConfig, cfg), universe_config=UniverseConfig(mode="all"))


# --------------------------------------------------------------------------- #
# 单条过滤规则
# --------------------------------------------------------------------------- #
def test_universe_applies_all_filters():
    store = _store()
    uni = store.universe(TARGET)
    assert uni == [SYM_OK]


def test_stats_breakdown():
    store = _store()
    stats = store.universe_builder.build_stats(TARGET)
    # D[79] 时 SYM_DELISTED 已无行情（D[70] 退市），因此候选为 5 个
    assert stats.candidates == 5
    assert SYM_ST in stats.excluded["st"]
    assert SYM_SUSP in stats.excluded["suspended"]
    assert SYM_NEW in stats.excluded["listing"]
    assert SYM_ILLIQ in stats.excluded["liquidity"]
    assert stats.final == 1
    assert stats.dropped == 4


def test_st_filter_can_be_disabled():
    store = _store(exclude_st=False)
    uni = store.universe(TARGET)
    assert SYM_ST in uni
    assert SYM_OK in uni


def test_suspension_filter_can_be_disabled():
    store = _store(exclude_suspended=False)
    uni = store.universe(TARGET)
    assert SYM_SUSP in uni


def test_listing_days_filter_disabled():
    # 关闭上市天数限制后，新上市标的仍会因「ADV 窗口不足 20 日」被流动性规则拦下
    store = _store(min_list_days=0)
    assert SYM_NEW not in store.universe(TARGET)
    assert SYM_NEW in store.universe_builder._filter(TARGET)[1].excluded["liquidity"]
    # 同时关闭流动性阈值后，新上市标的进入股票池
    loose = _store(min_list_days=0, liquidity={"window": 20, "min_amount": 0.0})
    assert SYM_NEW in loose.universe(TARGET)


def test_liquidity_filter_disabled():
    store = _store(liquidity={"window": 20, "min_amount": 0.0})
    assert SYM_ILLIQ in store.universe(TARGET)


def test_liquidity_threshold_boundary():
    # 阈值设为 100 万：恰好等于日均成交额 → 保留（>=）
    store = _store(liquidity={"window": 20, "min_amount": 1_000_000.0})
    assert SYM_ILLIQ in store.universe(TARGET)


def test_listed_days_are_trading_days():
    store = _store(min_list_days=0)
    assert store.listed_days(SYM_OK, TARGET) == 80
    assert store.listed_days(SYM_NEW, TARGET) == 15
    # 上市不足 60 日 → 在 65 天前尚不存在
    assert store.listed_days(SYM_NEW, D[60]) == 0


# --------------------------------------------------------------------------- #
# 幸存者偏差
# --------------------------------------------------------------------------- #
def test_delisted_symbol_included_before_delisting():
    store = _store()
    assert SYM_DELISTED in store.all_listed(D[60])
    assert SYM_DELISTED in store.universe(D[60])
    assert SYM_DELISTED not in store.all_listed(D[75])
    assert SYM_DELISTED not in store.universe(D[75])


def test_universe_is_deterministic():
    store = _store()
    first = store.universe(TARGET)
    second = store.universe(TARGET)
    assert first == second


# --------------------------------------------------------------------------- #
# 历史指数成分（不是当前成分）
# --------------------------------------------------------------------------- #
def test_index_mode_uses_historical_membership():
    members = pd.DataFrame(
        [
            {"index_code": "000300.SH", "symbol": SYM_OK, "effective_from": D[0], "effective_to": D[69]},
            {"index_code": "000300.SH", "symbol": SYM_ILLIQ, "effective_from": D[70], "effective_to": None},
        ]
    )
    store = make_store(
        _market(),
        data_config=construct(DataConfig, STRICT_DATA),
        universe_config=UniverseConfig(mode="index", index_code="000300.SH"),
        index_members=members,
    )
    # D[65]：成分是 SYM_OK，且已上市满 60 日
    assert store.index_members("000300.SH", D[65]) == [SYM_OK]
    assert store.universe(D[65]) == [SYM_OK]
    # D[75]：成分换成 SYM_ILLIQ，但流动性不足 → 空池
    assert store.universe(D[75]) == []
    # 放宽流动性后可见（证明股票池随历史成分变化，而不是用当前成分回溯）
    store_loose = make_store(
        _market(),
        data_config=construct(DataConfig, {**STRICT_DATA, "liquidity": {"window": 20, "min_amount": 0.0}}),
        universe_config=UniverseConfig(mode="index", index_code="000300.SH"),
        index_members=members,
    )
    assert store_loose.universe(D[75]) == [SYM_ILLIQ]
    assert store_loose.universe(D[65]) == [SYM_OK]


def test_index_mode_without_history_falls_back_to_all():
    store = make_store(
        _market(),
        data_config=construct(DataConfig, STRICT_DATA),
        universe_config=UniverseConfig(mode="index", index_code="000300.SH", fallback_to_all=True),
    )
    assert store.universe(TARGET) == [SYM_OK]


def test_index_mode_without_fallback_returns_empty():
    store = make_store(
        _market(),
        data_config=construct(DataConfig, STRICT_DATA),
        universe_config=UniverseConfig(mode="index", index_code="000300.SH", fallback_to_all=False),
    )
    assert store.universe(TARGET) == []


def test_universe_adv_within_window_matches_manual():
    store = _store()
    expected = store.adv(SYM_OK, TARGET, 20)
    assert expected == approx(100_000_000.0)
    assert store.universe_builder.adv_on(SYM_OK, TARGET, 20) == approx(100_000_000.0)
    # 历史不足 → None（不会被误判为 0）
    assert store.universe_builder.adv_on(SYM_NEW, D[66], 20) is None
