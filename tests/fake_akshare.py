"""``FakeAKShareClient``：离线替身，按 akshare 函数名返回 fixture。

用途：M4-8 的映射用例与 M4-9 的四 provider 契约测试（都不联网）。

设计要点：

1. **只实现 `ENDPOINTS` 里登记的函数名**：测试会断言「实现的方法集 == 端点表用到的函数名集」，
   于是端点名被改动/打错时立刻红灯（akshare 是模块级函数，拼错只有运行时才炸）；
2. **支持注入失败**：``fail_times``（前 N 次抛 ``ConnectionError``，用于重试用例）、
   ``fail_symbols``（指定标的永远失败，用于失败比例与降级用例）、``empty_calls``（返回空表）；
3. **记调用账**：``calls`` 记录 ``(函数名, 参数)``，用于断言缓存命中时「一次网络都没发」；
4. **不改 fixture 语义**：返回值形状尽量贴近真实 akshare（中文列名、成交量以「手」为单位）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import pandas as pd

__all__ = ["FakeAKShareClient", "FIXTURES"]

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "akshare"

#: 板块名 → fixture 文件名片段（ASCII，避免中文文件名的编码问题）
_BOARD_FILE = {"银行": "bank", "半导体": "semi"}


class FakeAKShareClient:
    """离线 akshare 替身。

    Args:
        fixture_root: fixture 目录（默认 `tests/fixtures/akshare`）。
        fail_times: 前 N 次**任意**调用抛 ``ConnectionError``（测试重试）。
        fail_symbols: 这些代码（不含后缀或含后缀均可）的调用永远抛 ``ConnectionError``。
        empty_calls: 这些函数名返回空表（测试「空结果不重试」与降级）。
        index_snapshots: ``代码 → 每次调用依次返回的 fixture 名``（模拟两次快照）。
    """

    __name__ = "FakeAKShareClient"

    def __init__(
        self,
        fixture_root: str | Path | None = None,
        *,
        fail_times: int = 0,
        fail_symbols: Sequence[str] = (),
        empty_calls: Sequence[str] = (),
        index_snapshots: dict[str, list[str]] | None = None,
    ) -> None:
        self.root = Path(fixture_root) if fixture_root is not None else FIXTURES
        self.fail_times = int(fail_times)
        self.fail_symbols = {str(s).split(".")[0] for s in fail_symbols}
        self.empty_calls = set(empty_calls)
        self.index_snapshots = {
            str(k).split(".")[0]: list(v) for k, v in (index_snapshots or {}).items()
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._attempts = 0
        self._snapshot_cursor: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    def _read(self, filename: str, *, dtype: dict[str, Any] | None = None) -> pd.DataFrame:
        path = self.root / filename
        if not path.exists():
            raise FileNotFoundError(f"缺少 fixture：{path}")
        return pd.read_csv(path, dtype=dtype or {"代码": str, "股票代码": str, "成分券代码": str})

    def _record(self, fn: str, kwargs: dict[str, Any]) -> None:
        self._attempts += 1
        self.calls.append((fn, dict(kwargs)))
        if self._attempts <= self.fail_times:
            raise ConnectionError(f"FakeAKShareClient 注入的瞬时故障（第 {self._attempts} 次）")
        code = str(kwargs.get("symbol", "")).split(".")[0]
        if code and code in self.fail_symbols:
            raise ConnectionError(f"FakeAKShareClient 注入的标的故障：{code}")

    def call_count(self, fn: str | None = None) -> int:
        """调用次数（可按函数名过滤）。"""
        if fn is None:
            return len(self.calls)
        return sum(1 for name, _ in self.calls if name == fn)

    # ------------------------------------------------------------------ #
    # akshare 函数替身（方法名 = 真实函数名）
    # ------------------------------------------------------------------ #
    def stock_zh_a_hist(
        self, *, symbol: str, period: str = "daily", start_date: str = "", end_date: str = "",
        adjust: str = "",
    ) -> pd.DataFrame:
        self._record("stock_zh_a_hist", {"symbol": symbol, "adjust": adjust,
                                        "start_date": start_date, "end_date": end_date})
        if "stock_zh_a_hist" in self.empty_calls:
            return pd.DataFrame()
        suffix = "hfq" if adjust == "hfq" else "raw"
        frame = self._read(f"stock_zh_a_hist_{symbol}_{suffix}.csv")
        dates = pd.to_datetime(frame["日期"], errors="coerce")
        mask = pd.Series(True, index=frame.index)
        if start_date:
            mask &= dates >= pd.Timestamp(start_date)
        if end_date:
            mask &= dates <= pd.Timestamp(end_date)
        return frame.loc[mask].reset_index(drop=True)

    def stock_individual_info_em(self, *, symbol: str) -> pd.DataFrame:
        self._record("stock_individual_info_em", {"symbol": symbol})
        if "stock_individual_info_em" in self.empty_calls:
            return pd.DataFrame()
        return self._read(f"stock_individual_info_em_{symbol}.csv")

    def tool_trade_date_hist_sina(self) -> pd.DataFrame:
        self._record("tool_trade_date_hist_sina", {})
        if "tool_trade_date_hist_sina" in self.empty_calls:
            return pd.DataFrame()
        return self._read("tool_trade_date_hist_sina.csv")

    def index_stock_cons_csindex(self, *, symbol: str) -> pd.DataFrame:
        self._record("index_stock_cons_csindex", {"symbol": symbol})
        if "index_stock_cons_csindex" in self.empty_calls:
            return pd.DataFrame()
        names = self.index_snapshots.get(symbol)
        if names:
            cursor = self._snapshot_cursor.get(symbol, 0)
            name = names[min(cursor, len(names) - 1)]
            self._snapshot_cursor[symbol] = cursor + 1
            return self._read(name)
        candidates = sorted(self.root.glob(f"index_stock_cons_csindex_{symbol}_*.csv"))
        if not candidates:
            return pd.DataFrame()
        return self._read(candidates[0].name)

    def stock_board_industry_name_em(self) -> pd.DataFrame:
        self._record("stock_board_industry_name_em", {})
        if "stock_board_industry_name_em" in self.empty_calls:
            return pd.DataFrame()
        return self._read("stock_board_industry_name_em.csv")

    def stock_board_industry_cons_em(self, *, symbol: str) -> pd.DataFrame:
        self._record("stock_board_industry_cons_em", {"symbol": symbol})
        if "stock_board_industry_cons_em" in self.empty_calls:
            return pd.DataFrame()
        tag = _BOARD_FILE.get(str(symbol), str(symbol))
        return self._read(f"stock_board_industry_cons_em_{tag}.csv")

    def stock_yjbb_em(self, *, date: str) -> pd.DataFrame:
        self._record("stock_yjbb_em", {"date": date})
        if "stock_yjbb_em" in self.empty_calls:
            return pd.DataFrame()
        path = self.root / f"stock_yjbb_em_{date}.csv"
        if not path.exists():
            return pd.DataFrame()
        return self._read(path)
