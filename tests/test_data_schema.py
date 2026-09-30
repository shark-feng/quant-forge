"""数据规范与质量校验测试（列规范、复权、涨跌停、公告日）。"""

from __future__ import annotations

import pandas as pd

from tests.compat import approx, raises
from tests.tools import bar_row, dates, make_bars

from aqs.core.enums import Board
from aqs.core.exceptions import SchemaError
from aqs.data.schema import (
    add_adjusted_prices,
    compute_limit_prices,
    infer_board,
    normalize_bars,
    normalize_fundamentals,
    normalize_index_members,
    normalize_symbol,
    select_fundamentals_asof,
    select_index_members,
    validate_bars,
)
from aqs.config.schema import PriceLimitConfig


# --------------------------------------------------------------------------- #
# 代码与板块
# --------------------------------------------------------------------------- #
def test_symbol_normalisation_and_board():
    assert normalize_symbol("600000") == "600000.SH"
    assert normalize_symbol("600000.sh") == "600000.SH"
    assert normalize_symbol("sh600000") == "600000.SH"
    assert normalize_symbol("000001") == "000001.SZ"
    assert normalize_symbol("300750.SZ") == "300750.SZ"
    assert infer_board("600519.SH") is Board.MAIN
    assert infer_board("000001.SZ") is Board.MAIN
    assert infer_board("300750.SZ") is Board.GEM
    assert infer_board("688981.SH") is Board.STAR
    assert infer_board("830799.BJ") is Board.BSE


# --------------------------------------------------------------------------- #
# 归一化
# --------------------------------------------------------------------------- #
def test_normalize_bars_derives_columns():
    d = dates(3)
    rows = [bar_row(d[0], close=10.0), bar_row(d[1], close=11.0), bar_row(d[2], close=12.0)]
    df = normalize_bars(make_bars(rows))
    assert list(df["symbol"].unique()) == ["600000.SH"]
    assert df["bar_seq"].tolist() == [1, 2, 3]
    assert df["prev_close"].tolist()[1] == 10.0
    assert df["prev_close"].tolist()[2] == 11.0
    assert df["board"].iloc[0] == Board.MAIN.value
    for col in ("open_adj", "high_adj", "low_adj", "close_adj", "base_adj_factor", "prev_adj_factor"):
        assert col in df.columns


def test_normalize_bars_requires_columns():
    with raises(SchemaError) as exc:
        normalize_bars(pd.DataFrame({"date": [dates(1)[0]], "symbol": ["600000.SH"], "close": [10.0]}))
    assert "open" in str(exc.value)


def test_adjusted_prices_use_first_bar_as_base():
    d = dates(3)
    rows = [
        bar_row(d[0], close=10.0, adj_factor=1.0),
        bar_row(d[1], close=9.0, adj_factor=2.0),
        bar_row(d[2], close=18.0, adj_factor=2.0),
    ]
    df = normalize_bars(make_bars(rows))
    assert df["close_adj"].tolist() == [10.0, 18.0, 36.0]
    # 后复权价首日 == 原始价
    assert df["close_adj"].iloc[0] == df["close"].iloc[0]


def test_normalize_bars_fills_amount_when_missing():
    d = dates(1)
    rows = [bar_row(d[0], close=10.0, volume=1000.0)]
    frame = make_bars(rows).drop(columns=["amount"])
    df = normalize_bars(frame)
    assert df["amount"].iloc[0] == 10_000.0


# --------------------------------------------------------------------------- #
# 涨跌停推算
# --------------------------------------------------------------------------- #
def test_limit_prices_main_board_and_st():
    d = dates(3)
    rows = [
        bar_row(d[0], "600000.SH", close=10.0),
        bar_row(d[1], "600000.SH", close=11.0),
        bar_row(d[2], "600000.SH", close=11.0),
        bar_row(d[0], "600001.SH", close=10.0, is_st=True),
        bar_row(d[1], "600001.SH", close=10.5, is_st=True),
        bar_row(d[0], "300001.SZ", close=10.0),
        bar_row(d[1], "300001.SZ", close=12.0),
    ]
    df = normalize_bars(make_bars(rows))
    main = df[df["symbol"] == "600000.SH"]
    # 首日不设涨跌幅
    assert pd.isna(main["limit_up"].iloc[0])
    # 主板 ±10%
    assert main["limit_up"].iloc[1] == 11.0
    assert main["limit_down"].iloc[1] == 9.0
    st = df[df["symbol"] == "600001.SH"]
    assert st["limit_up"].iloc[1] == 10.5
    assert st["limit_down"].iloc[1] == 9.5
    gem = df[df["symbol"] == "300001.SZ"]
    assert gem["limit_up"].iloc[1] == 12.0
    assert gem["limit_down"].iloc[1] == 8.0


def test_limit_prices_round_to_cent():
    d = dates(2)
    rows = [bar_row(d[0], close=3.33), bar_row(d[1], close=3.4)]
    df = normalize_bars(make_bars(rows))
    # 3.33 × 1.1 = 3.663 → 3.66
    assert df["limit_up"].iloc[1] == 3.66
    assert df["limit_down"].iloc[1] == 3.0


def test_provided_limits_take_precedence():
    d = dates(2)
    rows = [
        bar_row(d[0], close=10.0),
        bar_row(d[1], close=10.5, limit_up=10.7, limit_down=9.3),
    ]
    df = normalize_bars(make_bars(rows))
    assert df["limit_up"].iloc[1] == 10.7
    assert df["limit_down"].iloc[1] == 9.3


def test_limit_disabled():
    d = dates(2)
    rows = [bar_row(d[0], close=10.0), bar_row(d[1], close=10.5)]
    df = compute_limit_prices(normalize_bars(make_bars(rows)), PriceLimitConfig(enabled=False))
    assert df["limit_up"].isna().all()
    assert df["limit_down"].isna().all()


# --------------------------------------------------------------------------- #
# 质量校验
# --------------------------------------------------------------------------- #
def _good_frame() -> pd.DataFrame:
    d = dates(3)
    return normalize_bars(make_bars([bar_row(x, close=10.0 + i) for i, x in enumerate(d)]))


def test_validate_ok():
    report = validate_bars(_good_frame())
    assert report.ok, report.summary()
    assert report.stats["rows"] == 3
    assert report.stats["symbols"] == 1


def test_validate_duplicate_key():
    df = _good_frame()
    dup = pd.concat([df, df.iloc[[0]]], ignore_index=True)
    report = validate_bars(dup)
    assert not report.ok
    assert any("重复" in e for e in report.errors)


def test_validate_bad_price_and_range():
    df = _good_frame()
    df.loc[1, "close"] = -1.0
    report = validate_bars(df)
    assert any("非正值" in e for e in report.errors)

    df2 = _good_frame()
    df2.loc[1, "high"] = 1.0
    df2.loc[1, "low"] = 20.0
    report2 = validate_bars(df2)
    assert any("high < low" in e for e in report2.errors)


def test_validate_bad_adj_factor():
    df = _good_frame()
    df.loc[2, "adj_factor"] = 0.0
    report = validate_bars(df)
    assert any("adj_factor" in e for e in report.errors)


def test_validate_suspension_is_warning_only():
    d = dates(3)
    frame = make_bars([bar_row(x, close=10.0) for x in d])
    # 手工制造「标记停牌但有成交量」的不一致：应只告警、不阻断
    frame.loc[1, "is_suspended"] = True
    frame.loc[1, "volume"] = 5000.0
    report = validate_bars(normalize_bars(frame))
    assert report.ok
    assert any("停牌" in w for w in report.warnings)


def test_validate_catches_future_listing_date():
    d = dates(3)
    rows = [bar_row(x, close=10.0, list_date=pd.Timestamp(d[2])) for x in d]
    report = validate_bars(normalize_bars(make_bars(rows)))
    assert any("早于上市日期" in e for e in report.errors)


# --------------------------------------------------------------------------- #
# 指数成分
# --------------------------------------------------------------------------- #
def test_index_members_effective_window():
    df = pd.DataFrame(
        [
            {"index_code": "000300.SH", "symbol": "600000.SH", "effective_from": "2020-01-01", "effective_to": "2020-12-31"},
            {"index_code": "000300.SH", "symbol": "000001.SZ", "effective_from": "2021-01-01", "effective_to": None},
        ]
    )
    members = normalize_index_members(df)
    assert select_index_members(members, "000300.SH", "2020-06-30") == ["600000.SH"]
    assert select_index_members(members, "000300.SH", "2021-06-30") == ["000001.SZ"]
    assert select_index_members(members, "000300.SH", "2019-06-30") == []
    assert select_index_members(members, "000905.SH", "2021-06-30") == []


def test_index_members_requires_columns():
    with raises(SchemaError):
        normalize_index_members(pd.DataFrame([{"symbol": "600000.SH"}]))


# --------------------------------------------------------------------------- #
# 财务数据：公告日 vs 报告期（防未来函数）
# --------------------------------------------------------------------------- #
def _fundamentals_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            # 报告期 2022Q1，公告日 2022-04-28
            {"symbol": "600000.SH", "report_period": "2022-03-31", "announce_date": "2022-04-28", "roe": 0.10},
            # 报告期 2022Q2，公告日 2022-08-30
            {"symbol": "600000.SH", "report_period": "2022-06-30", "announce_date": "2022-08-30", "roe": 0.12},
            # 同一报告期的修正公告（公告日更晚）
            {"symbol": "600000.SH", "report_period": "2022-06-30", "announce_date": "2022-09-15", "roe": 0.13},
        ]
    )


def test_fundamentals_requires_announce_date():
    with raises(SchemaError) as exc:
        normalize_fundamentals(pd.DataFrame([{"symbol": "600000.SH", "report_period": "2022-03-31"}]))
    assert "announce_date" in str(exc.value)


def test_fundamentals_announce_date_must_be_after_report_period():
    bad = pd.DataFrame(
        [{"symbol": "600000.SH", "report_period": "2022-03-31", "announce_date": "2022-03-01", "roe": 0.1}]
    )
    with raises(SchemaError):
        normalize_fundamentals(bad)


def test_fundamentals_point_in_time_visibility():
    """核心用例：报告期已过但公告日未到 → 数据不可见（防未来函数）。"""
    f = normalize_fundamentals(_fundamentals_frame())
    # 2022-04-27（Q1 报告期已结束，但尚未公告）
    assert select_fundamentals_asof(f, "2022-04-27").empty
    # 2022-04-28 公告当天即可见
    visible = select_fundamentals_asof(f, "2022-04-28")
    assert list(visible.index) == ["600000.SH"]
    assert visible.loc["600000.SH", "report_period"] == pd.Timestamp("2022-03-31")
    # 2022-07-01：Q2 报告期已结束但未公告，仍只能看到 Q1
    visible_q1 = select_fundamentals_asof(f, "2022-07-01")
    assert visible_q1.loc["600000.SH", "report_period"] == pd.Timestamp("2022-03-31")
    # 2022-09-01：Q2 首次公告可见
    visible_q2 = select_fundamentals_asof(f, "2022-09-01")
    assert visible_q2.loc["600000.SH", "report_period"] == pd.Timestamp("2022-06-30")
    assert visible_q2.loc["600000.SH", "roe"] == approx(0.12)
    # 2022-09-15 之后：取修正后的数值
    visible_fix = select_fundamentals_asof(f, "2022-09-20")
    assert visible_fix.loc["600000.SH", "roe"] == approx(0.13)
    assert visible_fix.loc["600000.SH", "announce_date"] == pd.Timestamp("2022-09-15")


def test_fundamentals_symbol_filter():
    f = normalize_fundamentals(_fundamentals_frame())
    assert select_fundamentals_asof(f, "2022-12-31", symbols=["000001.SZ"]).empty
    assert len(select_fundamentals_asof(f, "2022-12-31", symbols=["600000.SH"])) == 1
