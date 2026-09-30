"""交易日历。

要点：
- **停牌日仍是交易日**（停牌 ≠ 休市），因此日历只由「有行情的日期并集」派生；
- 提供顺延查询（下一个可交易日），这是「涨停/停牌无法成交则顺延」规则的基础。
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import date as _date
from typing import Callable, Iterable, Iterator, Sequence

import pandas as pd

from .dates import DateLike, phase_timestamp, to_date, to_timestamp
from .enums import SessionPhase
from .exceptions import DataError

__all__ = ["TradingCalendar", "TradableFn"]

TradableFn = Callable[[str, _date], bool]


class TradingCalendar:
    """有序交易日集合及其查询工具。

    Args:
        days: 交易日序列（可含重复，内部会去重排序）。
        name: 日历名称（便于日志定位）。
    """

    __slots__ = ("_days", "_index", "name", "_tradable_fn")

    def __init__(self, days: Iterable[DateLike], *, name: str = "default") -> None:
        unique = sorted({to_date(d) for d in days})
        self._days: list[_date] = unique
        self._index: dict[_date, int] = {d: i for i, d in enumerate(unique)}
        self.name = name
        self._tradable_fn: TradableFn | None = None

    # ------------------------------ 构造 ------------------------------ #
    @classmethod
    def from_bars(cls, bars: pd.DataFrame, *, name: str = "from_bars") -> "TradingCalendar":
        """从行情表（含 ``date`` 列）派生日历。停牌日同样计入。"""
        if "date" not in bars.columns:
            raise DataError("行情表缺少 date 列，无法派生交易日历")
        return cls(pd.to_datetime(bars["date"]).unique().tolist(), name=name)

    @classmethod
    def from_file(cls, path: str, *, name: str | None = None, column: str = "date") -> "TradingCalendar":
        """从 CSV/Parquet 读取交易日。"""
        from pathlib import Path

        p = Path(path)
        if not p.exists():
            raise DataError(f"交易日历文件不存在：{p}")
        if p.suffix.lower() in (".parquet", ".pq"):
            df = pd.read_parquet(p)
        else:
            df = pd.read_csv(p)
        if column not in df.columns:
            raise DataError(f"交易日历文件缺少 {column} 列：{p}")
        return cls(df[column].tolist(), name=name or p.stem)

    def bind_tradable(self, fn: TradableFn | None) -> None:
        """绑定「某标的某日是否可交易」的查询函数（由 DataStore 提供）。"""
        self._tradable_fn = fn

    # ------------------------------ 基础属性 ------------------------------ #
    @property
    def days(self) -> list[_date]:
        return list(self._days)

    @property
    def first_day(self) -> _date | None:
        return self._days[0] if self._days else None

    @property
    def last_day(self) -> _date | None:
        return self._days[-1] if self._days else None

    def __len__(self) -> int:
        return len(self._days)

    def __contains__(self, day: object) -> bool:
        try:
            return to_date(day) in self._index  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    def __iter__(self) -> Iterator[_date]:
        return iter(self._days)

    def __repr__(self) -> str:  # pragma: no cover
        return f"TradingCalendar(name={self.name!r}, n={len(self._days)}, {self.first_day}~{self.last_day})"

    # ------------------------------ 查询 ------------------------------ #
    def is_trading_day(self, day: DateLike) -> bool:
        return to_date(day) in self._index

    def index_of(self, day: DateLike, *, strict: bool = True) -> int | None:
        idx = self._index.get(to_date(day))
        if idx is None and strict:
            raise DataError(f"{to_date(day)} 不是 {self.name} 日历中的交易日")
        return idx

    def next_trading_day(self, day: DateLike, n: int = 1, *, include_self: bool = False) -> _date | None:
        """第 n 个交易日（``n >= 1``）；越界返回 ``None``。"""
        if n < 1:
            raise ValueError("n 必须 >= 1，向后请使用 prev_trading_day")
        d = to_date(day)
        pos = bisect_left(self._days, d) if include_self else bisect_right(self._days, d)
        target = pos + n - 1
        if target >= len(self._days):
            return None
        return self._days[target]

    def prev_trading_day(self, day: DateLike, n: int = 1, *, include_self: bool = False) -> _date | None:
        if n < 1:
            raise ValueError("n 必须 >= 1，向前请使用 next_trading_day")
        d = to_date(day)
        pos = bisect_right(self._days, d) - 1 if include_self else bisect_left(self._days, d) - 1
        target = pos - (n - 1)
        if target < 0:
            return None
        return self._days[target]

    def shift(self, day: DateLike, n: int) -> _date | None:
        """n>0 向后，n<0 向前，n=0 返回规范化后的当日（若不是交易日则返回下一个交易日）。"""
        if n > 0:
            return self.next_trading_day(day, n)
        if n < 0:
            return self.prev_trading_day(day, -n)
        d = to_date(day)
        if d in self._index:
            return d
        return self.next_trading_day(d, 1)

    def sessions(self, start: DateLike | None = None, end: DateLike | None = None) -> list[_date]:
        """返回 ``[start, end]`` 区间内的交易日（含边界）。"""
        lo = 0 if start is None else bisect_left(self._days, to_date(start))
        hi = len(self._days) if end is None else bisect_right(self._days, to_date(end))
        return self._days[lo:hi]

    def count_between(self, start: DateLike, end: DateLike) -> int:
        return len(self.sessions(start, end))

    # ------------------------------ 顺延 ------------------------------ #
    def next_tradable_day(
        self,
        symbol: str,
        day: DateLike,
        *,
        include_self: bool = False,
        is_tradable: TradableFn | None = None,
        max_lookahead: int = 60,
    ) -> _date | None:
        """从 ``day`` 起找到该标的**第一个可交易日**（跳过停牌/无数据/退市）。

        用于实现「T+1 无法成交则顺延到下一个可成交日」。
        """
        fn = is_tradable or self._tradable_fn
        if fn is None:
            raise DataError("未绑定可交易性查询函数（calendar.bind_tradable 或显式传入 is_tradable）")
        d = to_date(day)
        if include_self and fn(symbol, d):
            return d
        cursor = d
        for _ in range(max_lookahead):
            nxt = self.next_trading_day(cursor, 1)
            if nxt is None:
                return None
            if fn(symbol, nxt):
                return nxt
            cursor = nxt
        return None

    def tradable_sessions(
        self,
        symbol: str,
        start: DateLike,
        end: DateLike,
        *,
        is_tradable: TradableFn | None = None,
    ) -> list[_date]:
        """区间内该标的所有可交易日。"""
        fn = is_tradable or self._tradable_fn
        if fn is None:
            raise DataError("未绑定可交易性查询函数")
        return [d for d in self.sessions(start, end) if fn(symbol, d)]

    # ------------------------------ 时间戳 ------------------------------ #
    def timestamp(self, day: DateLike, phase: SessionPhase = SessionPhase.CLOSE) -> pd.Timestamp:
        """某交易日某阶段的时间戳。"""
        if not self.is_trading_day(day):
            raise DataError(f"{to_date(day)} 不是交易日，无法构造时间戳")
        return phase_timestamp(day, phase)

    def normalize(self, day: DateLike) -> pd.Timestamp:
        return to_timestamp(day)

    # ------------------------------ 校验 ------------------------------ #
    def validate_span(self, start: DateLike, end: DateLike, *, min_days: int = 1) -> None:
        """校验区间内交易日数量是否足够（用于快速失败）。"""
        n = self.count_between(start, end)
        if n < min_days:
            raise DataError(
                f"区间 {to_date(start)} ~ {to_date(end)} 内交易日仅 {n} 天，少于要求的 {min_days} 天"
            )

    def missing_days(self, expected: Sequence[_date]) -> list[_date]:
        """返回期望交易日中不在日历里的日期（数据完整性校验用）。"""
        return [d for d in expected if to_date(d) not in self._index]
