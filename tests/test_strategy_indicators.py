"""指标层测试：手算一致性、offset 语义、NaN 严格性、与整段序列版一致。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from tests.compat import approx, raises

from aqs.strategy.indicators import (
    cross_over,
    cross_under,
    is_finite,
    latest_row,
    rolling_max,
    rolling_min,
    sma,
    tail_max,
    tail_mean,
    tail_min,
    tail_window,
    volume_ratio,
)


def _panel(cols: dict[str, list[float]]) -> pd.DataFrame:
    return pd.DataFrame(cols)


# --------------------------------------------------------------------------- #
# 尾部窗口
# --------------------------------------------------------------------------- #
def test_tail_window_inclusive_and_exclusive():
    panel = _panel({"A": [1.0, 2.0, 3.0, 4.0, 5.0]})
    assert tail_window(panel, 3).to_numpy().ravel().tolist() == [3.0, 4.0, 5.0]
    assert tail_window(panel, 3, offset=1).to_numpy().ravel().tolist() == [2.0, 3.0, 4.0]


def test_tail_window_insufficient_rows_returns_empty():
    panel = _panel({"A": [1.0, 2.0]})
    assert len(tail_window(panel, 5)) == 0
    assert len(tail_window(panel, 2, offset=1)) == 0


def test_tail_window_rejects_bad_arguments():
    panel = _panel({"A": [1.0, 2.0, 3.0]})
    with raises(ValueError):
        tail_window(panel, 0)
    with raises(ValueError):
        tail_window(panel, 2, offset=-1)


def test_tail_mean_hand_computed():
    panel = _panel({"A": [1.0, 2.0, 3.0, 4.0, 5.0], "B": [10.0, 20.0, 30.0, 40.0, 50.0]})
    now = tail_mean(panel, 3)
    prev = tail_mean(panel, 3, offset=1)
    assert now["A"] == approx(4.0)
    assert prev["A"] == approx(3.0)
    assert now["B"] == approx(40.0)
    assert prev["B"] == approx(30.0)


def test_tail_mean_insufficient_history_is_nan():
    panel = _panel({"A": [1.0, 2.0]})
    out = tail_mean(panel, 5)
    assert np.isnan(out["A"])


def test_tail_max_min_hand_computed():
    panel = _panel({"A": [3.0, 7.0, 2.0, 9.0, 4.0]})
    assert tail_max(panel, 3)["A"] == approx(9.0)            # [2, 9, 4]
    assert tail_max(panel, 3, offset=1)["A"] == approx(9.0)  # [7, 2, 9]
    assert tail_max(panel, 2, offset=1)["A"] == approx(9.0)  # [2, 9]
    assert tail_min(panel, 3)["A"] == approx(2.0)            # [2, 9, 4]
    assert tail_min(panel, 3, offset=1)["A"] == approx(2.0)  # [7, 2, 9]
    assert tail_min(panel, 2, offset=1)["A"] == approx(2.0)  # [2, 9]


def test_nan_in_window_propagates():
    panel = _panel({"A": [1.0, np.nan, 3.0, 4.0]})
    assert np.isnan(tail_mean(panel, 3)["A"])
    assert np.isnan(tail_max(panel, 3)["A"])


# --------------------------------------------------------------------------- #
# 与整段序列版一致（防止两套实现漂移）
# --------------------------------------------------------------------------- #
def test_tail_mean_matches_sma_last_row():
    rng = np.random.default_rng(0)
    panel = pd.DataFrame(rng.normal(10.0, 1.0, size=(40, 3)), columns=list("ABC"))
    for window in (2, 5, 20):
        expected = sma(panel, window).iloc[-1]
        actual = tail_mean(panel, window)
        for col in panel.columns:
            if np.isnan(expected[col]):
                assert np.isnan(actual[col])
            else:
                assert actual[col] == approx(float(expected[col]))


def test_tail_max_matches_rolling_max_excluding_current():
    rng = np.random.default_rng(1)
    panel = pd.DataFrame(rng.normal(10.0, 1.0, size=(40, 3)), columns=list("ABC"))
    for window in (2, 5, 20):
        expected = rolling_max(panel, window, exclude_current=True).iloc[-1]
        actual = tail_max(panel, window, offset=1)
        for col in panel.columns:
            assert actual[col] == approx(float(expected[col]))


def test_tail_min_matches_rolling_min_excluding_current():
    rng = np.random.default_rng(2)
    panel = pd.DataFrame(rng.normal(10.0, 1.0, size=(40, 2)), columns=["A", "B"])
    expected = rolling_min(panel, 10, exclude_current=True).iloc[-1]
    actual = tail_min(panel, 10, offset=1)
    for col in panel.columns:
        assert actual[col] == approx(float(expected[col]))


def test_rolling_max_include_current_semantics():
    panel = _panel({"A": [1.0, 5.0, 3.0]})
    excl = rolling_max(panel, 2, exclude_current=True)
    incl = rolling_max(panel, 2, exclude_current=False)
    assert np.isnan(excl["A"].iloc[1])          # 只有 1 个历史值
    assert excl["A"].iloc[2] == approx(5.0)     # max(1,5)
    assert incl["A"].iloc[2] == approx(5.0)     # max(5,3)


def test_sma_requires_full_window_by_default():
    panel = _panel({"A": [1.0, 2.0, 3.0]})
    out = sma(panel, 3)
    assert np.isnan(out["A"].iloc[1])
    assert out["A"].iloc[2] == approx(2.0)


# --------------------------------------------------------------------------- #
# 交叉与量比
# --------------------------------------------------------------------------- #
def test_cross_over_only_on_cross_day():
    fast = pd.DataFrame({"A": [1.0, 2.0, 3.0, 4.0, 2.0]})
    slow = pd.DataFrame({"A": [2.5, 2.5, 2.5, 2.5, 2.5]})
    # t=2: fast 3 > slow 2.5 且前一日 fast 2 <= 2.5 → 金叉
    flags = cross_over(fast, slow)["A"].tolist()
    assert flags == [False, False, True, False, False]
    # t=4: fast 2 < slow 2.5 且前一日 fast 4 >= 2.5 → 死叉
    under = cross_under(fast, slow)["A"].tolist()
    assert under == [False, False, False, False, True]


def test_cross_over_requires_strict_direction():
    fast = pd.DataFrame({"A": [2.0, 2.0, 2.0]})
    slow = pd.DataFrame({"A": [2.0, 2.0, 2.0]})
    assert cross_over(fast, slow)["A"].tolist() == [False, False, False]
    assert cross_under(fast, slow)["A"].tolist() == [False, False, False]


def test_volume_ratio_excludes_current_by_default():
    volume = pd.DataFrame({"A": [100.0, 100.0, 100.0, 100.0, 300.0]})
    # 不含当日：基准 = mean(100,100,100) = 100 → 量比 3.0
    assert volume_ratio(volume, 3)["A"].iloc[-1] == approx(3.0)
    # 含当日：基准 = mean(100,100,300) = 166.67 → 量比 1.8（放量被自身抬高，信号被削弱）
    assert volume_ratio(volume, 3, exclude_current=False)["A"].iloc[-1] == approx(300.0 / (500.0 / 3.0))


def test_volume_ratio_zero_base_is_nan():
    volume = pd.DataFrame({"A": [0.0, 0.0, 0.0, 10.0]})
    assert np.isnan(volume_ratio(volume, 3)["A"].iloc[-1])


def test_latest_row_and_is_finite():
    panel = _panel({"A": [1.0, 2.0], "B": [np.nan, np.nan]})
    row = latest_row(panel)
    assert row["A"] == approx(2.0)
    assert is_finite(row["A"]) is True
    assert is_finite(row["B"]) is False
    assert is_finite(None) is False
    assert is_finite("x") is False
