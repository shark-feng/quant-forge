"""指标层：纯函数、显式的 shift/offset 语义、无未来数据。

两类接口：
- **尾部窗口版**（``tail_mean``/``tail_max``/``tail_min``）：策略逐日使用，只取最后若干行，
  复杂度 O(window × symbols)；
- **整段序列版**（``sma``/``rolling_max``/``rolling_min``/``volume_ratio``）：研究与作图使用。

两者的一致性由测试锁定：
``tail_mean(frame, w)`` == ``sma(frame, w).iloc[-1]``、
``tail_max(frame, w, offset=1)`` == ``rolling_max(frame, w, exclude_current=True).iloc[-1]``。

NaN 语义：窗口内出现 NaN（标的缺失行情）时结果为 NaN —— **宁可不出信号，也不要用残缺数据出信号**。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

__all__ = [
    "tail_window",
    "tail_mean",
    "tail_max",
    "tail_min",
    "sma",
    "rolling_max",
    "rolling_min",
    "volume_ratio",
    "cross_over",
    "cross_under",
    "latest_row",
]


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _as_frame(data: pd.DataFrame | pd.Series) -> pd.DataFrame:
    return data.to_frame() if isinstance(data, pd.Series) else data


def _nan_series(frame: pd.DataFrame) -> pd.Series:
    return pd.Series(np.nan, index=frame.columns, dtype="float64")


def _check_window(window: int) -> None:
    if not isinstance(window, int) or window <= 0:
        raise ValueError(f"窗口长度必须为正整数，收到 {window!r}")


# --------------------------------------------------------------------------- #
# 尾部窗口版
# --------------------------------------------------------------------------- #
def tail_window(frame: pd.DataFrame | pd.Series, window: int, *, offset: int = 0) -> pd.DataFrame:
    """取「最后一行往前 offset 行的连续 window 行」。

    ``offset=0``：截至最后一行（含）；``offset=1``：截至倒数第二行（即不含最后一行）。

    行数不足时返回空 DataFrame（调用方据此判断历史是否足够）。
    """
    _check_window(window)
    if offset < 0:
        raise ValueError(f"offset 不能为负，收到 {offset}")
    df = _as_frame(frame)
    end = len(df) - offset
    start = end - window
    if start < 0 or end <= 0:
        return df.iloc[0:0]
    return df.iloc[start:end]


def _tail_reduce(
    frame: pd.DataFrame | pd.Series,
    window: int,
    func: str,
    *,
    offset: int = 0,
) -> pd.Series:
    df = _as_frame(frame)
    sub = tail_window(df, window, offset=offset)
    if len(sub) < window:
        return _nan_series(df)
    result = getattr(sub, func)(axis=0, skipna=False)
    return result.astype("float64")


def tail_mean(frame: pd.DataFrame | pd.Series, window: int, *, offset: int = 0) -> pd.Series:
    """尾部窗口均值（``offset=0`` 含最后一行，``offset=1`` 不含）。"""
    return _tail_reduce(frame, window, "mean", offset=offset)


def tail_max(frame: pd.DataFrame | pd.Series, window: int, *, offset: int = 0) -> pd.Series:
    """尾部窗口最大值。"""
    return _tail_reduce(frame, window, "max", offset=offset)


def tail_min(frame: pd.DataFrame | pd.Series, window: int, *, offset: int = 0) -> pd.Series:
    """尾部窗口最小值。"""
    return _tail_reduce(frame, window, "min", offset=offset)


def latest_row(frame: pd.DataFrame | pd.Series) -> pd.Series:
    """最后一行（跨列取当日值）。"""
    df = _as_frame(frame)
    return df.iloc[-1].astype("float64") if len(df) else _nan_series(df)


# --------------------------------------------------------------------------- #
# 整段序列版
# --------------------------------------------------------------------------- #
def sma(frame: pd.DataFrame | pd.Series, window: int, *, min_periods: int | None = None) -> pd.DataFrame:
    """简单移动平均（整段序列）。"""
    _check_window(window)
    df = _as_frame(frame)
    return df.astype("float64").rolling(window, min_periods=min_periods or window).mean()


def rolling_max(
    frame: pd.DataFrame | pd.Series, window: int, *, exclude_current: bool = True
) -> pd.DataFrame:
    """滚动最大值；``exclude_current=True`` 时不含当前行（用于「过去 N 日最高价」）。"""
    _check_window(window)
    df = _as_frame(frame).astype("float64")
    if exclude_current:
        return df.shift(1).rolling(window, min_periods=window).max()
    return df.rolling(window, min_periods=window).max()


def rolling_min(
    frame: pd.DataFrame | pd.Series, window: int, *, exclude_current: bool = True
) -> pd.DataFrame:
    """滚动最小值；``exclude_current=True`` 时不含当前行。"""
    _check_window(window)
    df = _as_frame(frame).astype("float64")
    if exclude_current:
        return df.shift(1).rolling(window, min_periods=window).min()
    return df.rolling(window, min_periods=window).min()


def volume_ratio(
    volume: pd.DataFrame | pd.Series,
    window: int,
    *,
    exclude_current: bool = True,
) -> pd.DataFrame:
    """量比 = 当日成交量 / 过去 window 日均量。

    ``exclude_current=True``（默认）：基准**不含当日**，与合同「过去 20 日平均成交量」一致。
    """
    _check_window(window)
    df = _as_frame(volume).astype("float64")
    base = df.shift(1).rolling(window, min_periods=window).mean() if exclude_current else sma(df, window)
    return df / base.replace(0.0, np.nan)


def cross_over(fast: pd.DataFrame | pd.Series, slow: pd.DataFrame | pd.Series) -> pd.DataFrame:
    """上穿：当日 ``fast > slow`` 且前一日 ``fast <= slow``。"""
    f, s = _as_frame(fast).astype("float64"), _as_frame(slow).astype("float64")
    return (f > s) & (f.shift(1) <= s.shift(1))


def cross_under(fast: pd.DataFrame | pd.Series, slow: pd.DataFrame | pd.Series) -> pd.DataFrame:
    """下穿：当日 ``fast < slow`` 且前一日 ``fast >= slow``。"""
    f, s = _as_frame(fast).astype("float64"), _as_frame(slow).astype("float64")
    return (f < s) & (f.shift(1) >= s.shift(1))


def is_finite(value: Any) -> bool:
    """判断标量是否为有限数值（用于逐标的过滤 NaN）。"""
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


__all__.append("is_finite")
