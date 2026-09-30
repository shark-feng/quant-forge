"""日期与时间工具。

统一约定：
- **日频决策**使用 :class:`datetime.date`；
- **事件时间戳**使用 :class:`pandas.Timestamp`，并按交易时段（09:00/09:30/15:00/15:10）取值，
  使同一交易日内的先后顺序天然正确。
"""

from __future__ import annotations

from datetime import date as _date
from datetime import datetime as _datetime
from typing import Any

import pandas as pd

from .enums import SessionPhase

__all__ = [
    "DateLike",
    "to_date",
    "to_timestamp",
    "phase_timestamp",
    "date_range_contains",
]

DateLike = Any  # date | datetime | pd.Timestamp | str

_ONE_DAY = pd.Timedelta(days=1)


def to_date(value: DateLike) -> _date:
    """把任意日期表示归一到 :class:`datetime.date`。"""
    if isinstance(value, _date) and not isinstance(value, _datetime):
        return value
    if isinstance(value, _datetime):
        return value.date()
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, str):
        return pd.Timestamp(value).date()
    raise TypeError(f"无法转换为日期：{value!r}（类型 {type(value).__name__}）")


def to_timestamp(value: DateLike) -> pd.Timestamp:
    """把任意日期表示归一到归一化（00:00:00）的 :class:`pandas.Timestamp`。"""
    if isinstance(value, pd.Timestamp):
        return value.normalize()
    if isinstance(value, _datetime):
        return pd.Timestamp(value).normalize()
    if isinstance(value, _date):
        return pd.Timestamp(value)
    if isinstance(value, str):
        return pd.Timestamp(value).normalize()
    raise TypeError(f"无法转换为时间戳：{value!r}（类型 {type(value).__name__}）")


def phase_timestamp(day: DateLike, phase: SessionPhase) -> pd.Timestamp:
    """返回某交易日某阶段的精确时间戳（A 股时段）。"""
    hour, minute = phase.time_of_day
    return to_timestamp(day) + pd.Timedelta(hours=hour, minutes=minute)


def date_range_contains(start: DateLike | None, end: DateLike | None, value: DateLike) -> bool:
    """判断 ``value`` 是否落在 ``[start, end]`` 内；``None`` 表示该侧无界。"""
    v = to_timestamp(value)
    if start is not None and v < to_timestamp(start):
        return False
    if end is not None and v > to_timestamp(end):
        return False
    return True
