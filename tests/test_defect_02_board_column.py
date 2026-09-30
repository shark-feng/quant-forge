"""回归缺陷 #2：``board`` 列被代码前缀推断值覆盖。

原缺陷：``normalize_bars`` 只要发现 board 列有任一 NaN 就用 ``infer_board`` **整体覆盖**，
数据源提供的板块信息被丢弃（其后还有一段冗余 fillna 死代码）。

修复后口径：**优先使用数据源提供的 board，仅对缺失/非法行按代码前缀推断**。
"""

from __future__ import annotations

import pandas as pd

from tests.compat import approx, raises
from tests.tools import bar_row, dates, make_bars

from aqs.core.enums import Board
from aqs.data.schema import normalize_bars, validate_bars

D = dates(3)
SYM = "600000.SH"          # 代码前缀推断为 main
GEM_SYM = "300001.SZ"      # 代码前缀推断为 gem


def frame(board_values=None, symbol=SYM) -> pd.DataFrame:
    rows = [bar_row(day, symbol, close=10.0) for day in D]
    df = make_bars(rows)
    if board_values is not None:
        df["board"] = board_values
    return df


# --------------------------------------------------------------------------- #
# 核心：数据源提供的 board 不被覆盖
# --------------------------------------------------------------------------- #
def test_provided_board_is_kept():
    """600000.SH 从代码前缀看是主板，但数据源标注为创业板 → 必须保留数据源口径。"""
    df = normalize_bars(frame(["gem", "gem", "gem"], symbol=SYM))
    assert set(df["board"]) == {Board.GEM.value}


def test_provided_board_changes_price_limit():
    """board 真正参与涨跌停计算：标为创业板时涨跌停幅度应为 20%。"""
    df = normalize_bars(frame(["gem", "gem", "gem"], symbol=SYM))
    # 首日不设涨跌幅；第 2 日 prev_close=10 → 创业板 20% → 12.0 / 8.0
    assert df["limit_up"].iloc[1] == approx(12.0)
    assert df["limit_down"].iloc[1] == approx(8.0)

    df_main = normalize_bars(frame(["main", "main", "main"], symbol=SYM))
    assert df_main["limit_up"].iloc[1] == approx(11.0)
    assert df_main["limit_down"].iloc[1] == approx(9.0)


def test_missing_board_rows_are_inferred_only():
    """缺失行按代码前缀推断，其余行保持数据源取值。"""
    df = normalize_bars(frame(["gem", None, "gem"], symbol=SYM))
    # 600000.SH 的 None 行 → 推断为主板；另两行保留数据源的 gem
    assert df["board"].tolist() == [Board.GEM.value, Board.MAIN.value, Board.GEM.value]


def test_invalid_board_value_is_inferred_and_reported():
    df = normalize_bars(frame(["main", "X", "main"], symbol=GEM_SYM))
    # 合法的 "main" 保留；非法的 "X" 按代码前缀推断为创业板
    assert df["board"].tolist() == [Board.MAIN.value, Board.GEM.value, Board.MAIN.value]

    report = validate_bars(frame(["main", "X", "main"], symbol=GEM_SYM))
    assert not report.ok
    assert any("board" in e for e in report.errors)


def test_board_column_absent_is_fully_inferred():
    df = normalize_bars(frame(None, symbol=GEM_SYM))
    assert set(df["board"]) == {Board.GEM.value}
    df2 = normalize_bars(frame(None, symbol=SYM))
    assert set(df2["board"]) == {Board.MAIN.value}


def test_provided_board_is_normalised_case_and_space():
    df = normalize_bars(frame([" GEM ", "Gem", "gem"], symbol=SYM))
    assert set(df["board"]) == {Board.GEM.value}


def test_mixed_boards_in_one_frame():
    rows = []
    for i, day in enumerate(D):
        rows.append(bar_row(day, SYM, close=10.0))
        rows.append(bar_row(day, GEM_SYM, close=10.0))
    df = make_bars(rows)
    df["board"] = ["gem", "main"] * len(D)  # 故意交叉标注
    out = normalize_bars(df)
    boards = out.groupby("symbol", observed=True)["board"].first().to_dict()
    assert boards[SYM] == Board.GEM.value
    assert boards[GEM_SYM] == Board.MAIN.value


def test_validate_flags_invalid_board_value():
    df = normalize_bars(frame(None))
    bad = df.copy()
    bad["board"] = "not_a_board"
    report = validate_bars(bad)
    assert not report.ok
    assert any("board 列存在" in e for e in report.errors)


def test_validate_accepts_all_enum_values():
    for board in Board:
        df = normalize_bars(frame(None))
        df["board"] = board.value
        report = validate_bars(df)
        assert not any("board" in e for e in report.errors), board
