"""AKShare provider（M4-8）：**字段映射骨架** + 快照累积状态。

本步**不做任何联网操作**：真实接口的存在性、列名、dtype、耗时、分页与限额
由 M5 的 `tools/probe_akshare.py` 探测后固化（`docs/12_appendix_verification.md`）。
因此这里的一切接口名与中文列名都是**候选值**，集中在两张表里，探测报告出来后**单点修改**：

- :data:`ENDPOINTS`：逻辑端点 → akshare 函数名 / 数据集 / 映射器 / 支撑能力 / 缓存策略；
- :data:`MAPPERS`：映射器名 → 纯函数（AKShare 中文列 → canonical 列）。

三条不可让步的约束：

1. **能力如实声明**：只给「当前值」的接口不得声称具备历史能力。
   最有代表性的是指数成分 —— 候选接口只返回**当前**成分，而
   ``ProviderCapabilities.index_members`` 的定义是**历史**成分，
   故声明为 ``False``；调用时仍返回快照数据，但附带**具体**警告（写明 index_code、
   请求区间、快照日、后果），而不是假装没有数据，也不是无声地把它当历史用。
   （``industry`` 声明为 ``True`` 的理由：它以 ``effective_from/effective_to`` 区间形式
   累积，且 note 明确「首次抓取日之前不可得」；而指数成分直接决定股票池，
   误用的后果是**幸存者偏差**，严重度最高，故按最严标准声明。）
2. **单位换算在映射里，且不能靠配置默认值**：AKShare 日线成交量单位是**手**，
   固定 ×100 转股（:data:`_DEFAULT_VOLUME_TO_SHARES`）。若改用
   ``config.unit_conversion.volume_to_shares``（默认 1.0），一旦使用者没改配置，
   成交量/成交额/参与率/冲击成本会**整体错 100 倍且不报错**。故此处用模块常量，
   并把「已应用手→股换算」写进 ``capabilities().notes``；配置里若被显式改成别的值，
   会在 warnings 里**披露该覆盖无效**（不静默忽略）。
3. **快照累积是状态，不是缓存**：指数成分/行业的接口只给「今天是谁」，
   历史只能靠我们自己逐日累积。累积表跨调用维护（新进/退出/留存），
   用固定缓存 key（不带日期），元信息的 ``start/end`` 表示**已累积区间**，
   同一天重复抓取**幂等**。累积逻辑写成纯函数 :func:`_accumulate_snapshot` 单独测试。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from ..config.loader import resolve_path
from ..config.schema import DataConfig, as_config
from ..core.dates import DateLike
from ..core.exceptions import ConfigError, DataError
from ..core.logging import get_logger
from .cache import CacheLookup, CacheMeta, DataCache
from .provider import (
    ProviderCapabilities,
    ProviderHealth,
    Provenance,
    filter_effective_window,
    utc_now,
)
from .ratelimit import RateLimiter, resolve_exceptions, retry_call
from .schema import INDEX_MEMBER_COLUMNS, normalize_symbol

__all__ = [
    "ENDPOINTS",
    "MAPPERS",
    "Endpoint",
    "AKShareProvider",
    "client_kwargs_for",
    "normalize_akshare_symbol",
    "derive_adj_factor",
    "report_periods_for_range",
]

logger = get_logger("data.akshare")

#: AKShare 日线成交量单位是「手」→ 股（Q3 单位陷阱）。
#: **刻意不读配置**：`data.unit_conversion.volume_to_shares` 默认 1.0，
#: 使用者一旦忘了改，全部量纲会错 100 倍且不报错（不报错的错最危险）。
_DEFAULT_VOLUME_TO_SHARES = 100.0

#: 合并/排序后的固定主键
_BARS_KEYS = ("date", "symbol")
_MEMBER_KEYS = list(INDEX_MEMBER_COLUMNS)

#: 多标的循环的审计节流：标的数超过该值时每 N 个写一条审计记录
_AUDIT_THRESHOLD = 50
_AUDIT_EVERY = 10

#: 财务：抓取报告期时的「公告滞后」上界（自然日）。宁可多抓（按公告日过滤），不可漏抓：
#: 年报最晚 4/30 披露、且存在更正公告，故取 365 天这一**保守上界**。
_ANNOUNCE_LOOKBACK_DAYS = 365

#: akshare 代码列常因 pandas 读成 float 而带 ``.0`` 尾巴
_FLOAT_TAIL = re.compile(r"^(\d{6})\.0$")
_BARE_CODE = re.compile(r"^\d{6}$")


# --------------------------------------------------------------------------- #
# 端点表（候选；M5 探测后单点修改）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Endpoint:
    """一个候选数据端点。

    Args:
        fn: akshare 函数名（**候选值**，未联网核实）。
        dataset: 缓存数据集名。
        mapper: :data:`MAPPERS` 的键。
        capability: 该端点支撑的能力名（``None`` 表示不属于任何能力）；
            用于「能力声明 ↔ 实现」一致性自检。
        strategy: 缓存策略 —— ``incremental``（可增量追加，主键去重）/
            ``fixed``（整表替换，按 TTL 命中）/ ``snapshot``（累积状态，不用 TTL）。
        per_symbol: 是否**一次调用只能查一个标的**（决定调用与缓存粒度）。
        key_kind: 缓存 key 的构造方式。
    """

    fn: str
    dataset: str
    mapper: str
    capability: str | None = None
    strategy: str = "fixed"
    per_symbol: bool = False
    key_kind: str = "fixed"


ENDPOINTS: dict[str, Endpoint] = {
    # 日线：同一函数名靠 adjust 参数区分口径，故按逻辑端点分开登记
    "bars_raw": Endpoint(
        fn="stock_zh_a_hist",
        dataset="bars",
        mapper="bars_raw",
        capability="daily_bars",
        strategy="incremental",
        per_symbol=True,
        key_kind="symbol_adjust",
    ),
    "bars_hfq": Endpoint(
        fn="stock_zh_a_hist",
        dataset="bars_hfq",
        mapper="bars_hfq",
        capability="adjustment_factors",
        strategy="incremental",
        per_symbol=True,
        key_kind="symbol_adjust",
    ),
    "symbol_meta": Endpoint(
        fn="stock_individual_info_em",
        dataset="symbol_meta",
        mapper="symbol_meta",
        capability="listing_dates",
        per_symbol=True,
        key_kind="symbol",
    ),
    "calendar": Endpoint(
        fn="tool_trade_date_hist_sina",
        dataset="calendar",
        mapper="calendar",
    ),
    "index_members": Endpoint(
        fn="index_stock_cons_csindex",
        dataset="index_members",
        mapper="index_members",
        capability="index_members",
        strategy="snapshot",
        key_kind="symbol",
    ),
    "board_list": Endpoint(
        fn="stock_board_industry_name_em",
        dataset="board_list",
        mapper="board_list",
        capability="industry",
    ),
    "industry": Endpoint(
        fn="stock_board_industry_cons_em",
        dataset="industry",
        mapper="industry",
        capability="industry",
        strategy="snapshot",
        key_kind="industry",
    ),
    "fundamentals": Endpoint(
        fn="stock_yjbb_em",
        dataset="fundamentals",
        mapper="fundamentals",
        capability="fundamentals",
        per_symbol=False,
        key_kind="report_period",
    ),
}

#: 支持的能力（供一致性自检：端点声明的能力名必须在这里）
_SUPPORTED_CAPABILITY_NAMES = frozenset(
    {
        "daily_bars",
        "adjustment_factors",
        "suspensions",
        "price_limits",
        "st_flags",
        "listing_dates",
        "delistings",
        "index_members",
        "fundamentals",
        "industry",
        "market_cap",
        "intraday",
    }
)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def normalize_akshare_symbol(code: Any) -> str:
    """把 AKShare 返回的代码归一为 canonical（``600000.SH``）并**严格校验**。

    复用 `schema.normalize_symbol` 的推断规则（不重复实现），但补上两件 AKShare 特有的事：

    1. pandas 把代码列读成 float 时会带 ``.0``（``600000.0``）→ 先剥掉；
    2. `schema.normalize_symbol` 对无法识别的输入会**兜底**成 ``.SH/.SZ``
       （那是给内部数据的宽松口径）；这里对**外部返回值**必须严格：
       非「6 位数字（可带后缀）」直接报错，否则 ``abc`` 会被静默拼成 ``abc.SZ``。
    """
    raw = str(code).strip()
    tail = _FLOAT_TAIL.match(raw)
    if tail:
        raw = tail.group(1)
    if not raw:
        raise DataError("AKShare 返回了空的证券代码")
    body = raw.split(".")[0]
    if not _BARE_CODE.match(body):
        raise DataError(f"AKShare 返回了非法证券代码：{code!r}")
    return normalize_symbol(raw)


def _pick_column(frame: pd.DataFrame, *candidates: str, required: bool = True) -> str | None:
    """按候选名找列（**候选名是探测前的不确定项，故允许多个候选**）。"""
    for name in candidates:
        if name in frame.columns:
            return name
    if required:
        raise DataError(
            f"AKShare 返回缺少列：候选 {list(candidates)} 都不在 {sorted(map(str, frame.columns))} 中"
            "（接口列名是候选值，请用 tools/probe_akshare.py 探测后在 MAPPERS 单点修正）"
        )
    return None


def _num(frame: pd.DataFrame, *candidates: str, required: bool = True) -> pd.Series:
    """取数值列（缺失时按 ``required`` 报错或返回 NaN 序列）。"""
    name = _pick_column(frame, *candidates, required=required)
    if name is None:
        return pd.Series([float("nan")] * len(frame), index=frame.index, dtype="float64")
    return pd.to_numeric(frame[name], errors="coerce")


def _dates(frame: pd.DataFrame, *candidates: str) -> pd.Series:
    name = _pick_column(frame, *candidates)
    assert name is not None
    return pd.to_datetime(frame[name], errors="coerce").dt.normalize()


def _parse_compact_date(value: Any) -> pd.Timestamp:
    """解析 ``1999-11-10`` / ``19991110`` / ``1999/11/10`` 三种写法。"""
    text = str(value).strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        return pd.Timestamp(digits[:8])
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        raise DataError(f"无法解析日期：{value!r}")
    return pd.Timestamp(parsed).normalize()


# --------------------------------------------------------------------------- #
# 映射器（纯函数：AKShare 中文列 → canonical 列）
# --------------------------------------------------------------------------- #
def map_bars_raw(frame: pd.DataFrame, *, symbol: str, **_: Any) -> pd.DataFrame:
    """日线（不复权）→ canonical 行情。**成交量 手 → 股**。"""
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=["date", "symbol", "open", "high", "low", "close",
                                     "volume", "amount", "prev_close", "adj_factor"])
    out = pd.DataFrame(
        {
            "date": _dates(frame, "日期", "date"),
            "symbol": symbol,
            "open": _num(frame, "开盘", "open"),
            "high": _num(frame, "最高", "high"),
            "low": _num(frame, "最低", "low"),
            "close": _num(frame, "收盘", "close"),
            "volume": _num(frame, "成交量", "volume") * _DEFAULT_VOLUME_TO_SHARES,
            "amount": _num(frame, "成交额", "amount"),
            "prev_close": _num(frame, "昨收", "前收盘", "prev_close", required=False),
        }
    )
    # 原始价口径：adj_factor 由后复权序列派生（见 derive_adj_factor）
    out["adj_factor"] = 1.0
    bad = out["close"].isna() | (out["close"] <= 0)
    if bool(bad.any()):
        out.attrs["warnings"] = (f"{int(bad.sum())} 行收盘价缺失或非正，已丢弃",)
    out = out.loc[~bad].reset_index(drop=True)
    return out.sort_values(list(_BARS_KEYS)).reset_index(drop=True)


def map_bars_hfq(frame: pd.DataFrame, *, symbol: str, **_: Any) -> pd.DataFrame:
    """日线（后复权）→ 仅 ``date`` + ``close_hfq``（供算复权因子，不参与 canonical 行情）。"""
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=["date", "close_hfq"])
    out = pd.DataFrame({"date": _dates(frame, "日期", "date"), "close_hfq": _num(frame, "收盘", "close")})
    return out.dropna(subset=["date", "close_hfq"]).sort_values("date").reset_index(drop=True)


def derive_adj_factor(raw: pd.DataFrame, hfq: pd.DataFrame) -> pd.DataFrame:
    """由「后复权收盘 / 原始收盘」派生 ``adj_factor``，按 symbol **首日归一为 1.0**。

    归一的意义：``adj_factor`` 只表示**相对变化**，绝对水平无意义；归一后与
    ``normalize_bars`` 的口径一致，也便于跨源比对。

    边界：原始收盘 ≤ 0 或 hfq 缺失的行**不猜测**（``adj_factor`` 置 NaN 并进 warning），
    而不是填 1.0 —— 填 1.0 等于伪造「无复权事件」。
    """
    out = raw.copy()
    if hfq is None or len(hfq) == 0:
        out["adj_factor"] = float("nan")
        out.attrs["warnings"] = ("无后复权序列，adj_factor 置为缺失（不做假设）",)
        return out

    merged = out.merge(hfq[["date", "close_hfq"]], on="date", how="left")
    ratio = pd.Series(float("nan"), index=merged.index, dtype="float64")
    valid = merged["close"].notna() & (merged["close"] > 0) & merged["close_hfq"].notna()
    ratio.loc[valid] = merged.loc[valid, "close_hfq"] / merged.loc[valid, "close"]

    first = ratio.dropna()
    warnings: tuple[str, ...] = ()
    if len(first) == 0:
        out["adj_factor"] = float("nan")
        warnings += ("后复权序列与原始序列无交集日期，adj_factor 无法派生",)
    else:
        base = float(first.iloc[0])
        if base <= 0:
            out["adj_factor"] = float("nan")
            warnings += (f"复权基准非正（{base}），adj_factor 置为缺失",)
        else:
            out["adj_factor"] = (ratio / base).to_numpy()
            missing = int(out["adj_factor"].isna().sum())
            if missing:
                warnings += (f"{missing} 行缺少后复权价，adj_factor 为缺失",)
    if warnings:
        out.attrs["warnings"] = warnings
    return out


def _meta_from_long(frame: pd.DataFrame) -> dict[str, Any]:
    """``stock_individual_info_em`` 的 item/value 长表 → 字典。"""
    items = frame["item"] if "item" in frame.columns else frame.iloc[:, 0]
    values = frame["value"] if "value" in frame.columns else frame.iloc[:, 1]
    return {str(k).strip(): v for k, v in zip(items, values, strict=False)}


def map_symbol_meta(frame: pd.DataFrame, *, symbol: str, **_: Any) -> pd.DataFrame:
    """股票基本信息 → ``symbol/name/list_date/delist_date/industry/board``。

    兼容两种返回形态（探测前无法确定是哪种）：
    长表 ``item/value``（``stock_individual_info_em`` 的真实形态）与宽表（含 ``上市时间`` 列）。
    ``list_date`` **缺失即报错**（缺陷 #8 口径：不得静默用其它字段兜底）。
    """
    if frame is None or len(frame) == 0:
        raise DataError(f"{symbol} 的基本信息为空，无法取得 list_date（不得静默兜底）")

    if "item" in frame.columns and "value" in frame.columns:
        info = _meta_from_long(frame)
        name = info.get("股票简称")
        industry = info.get("行业")
        raw_list = info.get("上市时间")
        raw_delist = info.get("退市时间")
    else:
        name = frame.iloc[0].get("股票简称", frame.iloc[0].get("名称"))
        industry = frame.iloc[0].get("行业", frame.iloc[0].get("所属行业"))
        raw_list = frame.iloc[0].get("上市时间")
        raw_delist = frame.iloc[0].get("退市时间")

    if raw_list is None or (isinstance(raw_list, float) and pd.isna(raw_list)) or str(raw_list).strip() in ("", "-", "nan", "NaT"):
        raise DataError(
            f"{symbol} 缺少上市时间（list_date 是回测必需字段，缺失即错误；见缺陷 #8 的 strict 口径）"
        )

    row = {
        "symbol": symbol,
        "name": None if name is None else str(name),
        "list_date": _parse_compact_date(raw_list),
        "delist_date": (
            None
            if raw_delist is None or str(raw_delist).strip() in ("", "-", "nan", "NaT")
            else _parse_compact_date(raw_delist)
        ),
        "industry": None if industry is None else str(industry),
    }
    return pd.DataFrame([row])


def map_calendar(frame: pd.DataFrame, **_: Any) -> pd.DataFrame:
    """交易日历 → 单列 ``date`` 的 DataFrame（缓存存表、对外转 list）。"""
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=["date"])
    out = pd.DataFrame({"date": _dates(frame, "trade_date", "日期", "date")})
    return out.dropna(subset=["date"]).drop_duplicates().sort_values("date").reset_index(drop=True)


def map_index_members(
    frame: pd.DataFrame, *, index_code: str, snapshot_date: DateLike, **_: Any
) -> pd.DataFrame:
    """指数成分（**当前**成分）→ 一条快照：``effective_from = 抓取日``、``effective_to`` 空。"""
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=list(INDEX_MEMBER_COLUMNS))
    name = _pick_column(frame, "成分券代码", "品种代码", "代码", "symbol", "con_code")
    assert name is not None
    symbols = [normalize_akshare_symbol(v) for v in frame[name].tolist()]
    day = pd.Timestamp(snapshot_date).normalize()
    out = pd.DataFrame(
        {
            "index_code": str(index_code).upper(),
            "symbol": symbols,
            "effective_from": day,
            "effective_to": pd.NaT,
        }
    )
    return out.drop_duplicates(subset=["index_code", "symbol"]).reset_index(drop=True)


def map_board_list(frame: pd.DataFrame, **_: Any) -> pd.DataFrame:
    """行业板块列表 → 单列 ``industry``。"""
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=["industry"])
    name = _pick_column(frame, "板块名称", "行业名称", "名称", "name")
    assert name is not None
    out = pd.DataFrame({"industry": frame[name].astype(str).str.strip()})
    return out.loc[out["industry"] != ""].drop_duplicates().reset_index(drop=True)


def map_industry(
    frame: pd.DataFrame, *, industry_name: str, snapshot_date: DateLike, **_: Any
) -> pd.DataFrame:
    """某板块的成分股 → 行业快照（``symbol/industry/effective_from/effective_to``）。"""
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=["symbol", "industry", "effective_from", "effective_to"])
    name = _pick_column(frame, "代码", "证券代码", "symbol")
    assert name is not None
    symbols = [normalize_akshare_symbol(v) for v in frame[name].tolist()]
    day = pd.Timestamp(snapshot_date).normalize()
    out = pd.DataFrame(
        {
            "symbol": symbols,
            "industry": str(industry_name),
            "effective_from": day,
            "effective_to": pd.NaT,
        }
    )
    return out.drop_duplicates(subset=["symbol"]).reset_index(drop=True)


def map_fundamentals(
    frame: pd.DataFrame, *, report_period: DateLike, **_: Any
) -> pd.DataFrame:
    """业绩报表 → canonical 财务。

    ``announce_date`` **必填**：缺失的行**丢弃并计数**（放进 ``frame.attrs["dropped_rows"]``），
    绝不用 ``report_period`` 顶替 —— 那是典型的财务未来函数。
    """
    columns = ["symbol", "report_period", "announce_date", "eps", "revenue", "net_profit", "roe"]
    if frame is None or len(frame) == 0:
        out = pd.DataFrame(columns=columns)
        out.attrs["dropped_rows"] = 0
        return out

    code_col = _pick_column(frame, "股票代码", "代码", "symbol")
    announce_col = _pick_column(frame, "最新公告日期", "公告日期", "announce_date", required=False)
    assert code_col is not None

    period = pd.Timestamp(report_period).normalize()
    out = pd.DataFrame(
        {
            "symbol": [normalize_akshare_symbol(v) for v in frame[code_col].tolist()],
            "report_period": period,
            "announce_date": (
                pd.to_datetime(frame[announce_col], errors="coerce").dt.normalize()
                if announce_col
                else pd.NaT
            ),
            "eps": _optional_series(frame, "每股收益", "eps"),
            "revenue": _optional_series(frame, "营业收入-营业收入", "营业总收入", "revenue"),
            "net_profit": _optional_series(frame, "净利润-净利润", "净利润", "net_profit"),
            "roe": _optional_series(frame, "净资产收益率", "roe"),
        }
    )
    missing = out["announce_date"].isna()
    dropped = int(missing.sum())
    out = out.loc[~missing].reset_index(drop=True)
    out.attrs["dropped_rows"] = dropped
    if dropped:
        out.attrs["warnings"] = (
            f"{dropped} 行缺少公告日期，已丢弃（不得用报告期顶替，否则是财务未来函数）",
        )
    return out


def _optional_series(frame: pd.DataFrame, *candidates: str) -> pd.Series:
    return _num(frame, *candidates, required=False)


MAPPERS: dict[str, Callable[..., pd.DataFrame]] = {
    "bars_raw": map_bars_raw,
    "bars_hfq": map_bars_hfq,
    "symbol_meta": map_symbol_meta,
    "calendar": map_calendar,
    "index_members": map_index_members,
    "board_list": map_board_list,
    "industry": map_industry,
    "fundamentals": map_fundamentals,
}


# --------------------------------------------------------------------------- #
# 快照累积（纯函数，单独测试）
# --------------------------------------------------------------------------- #
def _accumulate_snapshot(
    cached: pd.DataFrame | None,
    current: pd.DataFrame,
    snapshot_date: DateLike,
) -> tuple[pd.DataFrame, str]:
    """把「今天的快照」并入累积表，返回 ``(新累积表, 动作)``。

    动作取值：``appended``（新增了一天）、``same_day_ignored``（同日重复抓取，幂等）、
    ``no_change``（同日且完全一致）。

    规则：

    - 上一次快照中有、今天没有的标的 → **退出**：闭合其 ``effective_to = 快照日 − 1 天``
      （用**自然日**，不假装知道交易日历）；
    - 今天新出现的标的 → 追加一行 ``effective_from = 快照日``、``effective_to = 空``；
    - 两天都在 → **留存**，不改动（区间继续延伸）；
    - **同一天重复抓取幂等**：不重复追加。若同日快照内容不同（数据被修订），
      保留首次快照并把 ``same_day_ignored`` 报给调用方（由调用方写 warning），
      **不覆盖**已观测到的历史。

    纯函数：不修改 ``cached``/``current``，返回新对象。
    """
    day = pd.Timestamp(snapshot_date).normalize()
    cur = current.copy() if current is not None else pd.DataFrame()
    if len(cur):
        cur["effective_from"] = day
        if "effective_to" not in cur.columns:
            cur["effective_to"] = pd.NaT
        cur = cur[list(_MEMBER_KEYS)] if set(_MEMBER_KEYS).issubset(cur.columns) else cur

    if cached is None or len(cached) == 0:
        out = cur.drop_duplicates(subset=_group_keys(cur)).reset_index(drop=True)
        return out, ("appended" if len(out) else "no_change")

    hist = cached.copy()
    hist["effective_from"] = pd.to_datetime(hist["effective_from"], errors="coerce").dt.normalize()
    if "effective_to" not in hist.columns:
        hist["effective_to"] = pd.NaT
    hist["effective_to"] = pd.to_datetime(hist["effective_to"], errors="coerce")

    keys = _group_keys(hist)
    last_day = hist["effective_from"].max()
    if last_day is not None and not pd.isna(last_day) and last_day == day:
        same = _same_snapshot(hist, cur, day, keys)
        return hist.reset_index(drop=True), ("no_change" if same else "same_day_ignored")

    # 1) 闭合已退出标的的区间（只动「仍然生效」的行）
    open_rows = hist["effective_to"].isna()
    today_pairs: set[tuple[str, ...]] = (
        set(map(tuple, cur[keys].astype(str).to_numpy())) if len(cur) else set()
    )
    keep = pd.Series(
        [tuple(row) in today_pairs for row in hist[keys].astype(str).to_numpy()], index=hist.index
    )
    close_at = day - timedelta(days=1)
    hist.loc[open_rows & ~keep, "effective_to"] = close_at

    # 2) 追加今天新出现的标的（已在历史里「仍然生效」的不重复追加）
    if len(cur):
        still_open = set(
            map(tuple, hist.loc[hist["effective_to"].isna(), keys].astype(str).to_numpy())
        )
        new_mask = [
            tuple(str(v) for v in row) not in still_open
            for row in cur[keys].astype(str).to_numpy()
        ]
        fresh = cur.loc[pd.Series(new_mask, index=cur.index)]
    else:
        fresh = cur

    out = (
        pd.concat([hist, fresh], ignore_index=True)
        .drop_duplicates(subset=keys, keep="last")
        .sort_values(keys)
        .reset_index(drop=True)
    )
    return out, ("appended" if len(fresh) else "no_change")


def _merge_keys(frame: pd.DataFrame) -> tuple[str, ...]:
    """推导增量合并的主键：``date``（若有 ``symbol`` 则一起用）。

    主键必须从**实际列**推导，不能假设所有数据集都有 ``symbol``。
    """
    keys = tuple(c for c in _BARS_KEYS if c in frame.columns)
    if not keys:
        raise DataError(f"增量合并无法确定主键（列：{sorted(map(str, frame.columns))}）")
    return keys


def _group_keys(frame: pd.DataFrame) -> list[str]:
    """累积表的主键：指数成分按 ``index_code+symbol``，行业按 ``symbol``。"""
    return ["index_code", "symbol"] if "index_code" in frame.columns else ["symbol"]


def _same_snapshot(hist: pd.DataFrame, cur: pd.DataFrame, day: pd.Timestamp, keys: list[str]) -> bool:
    """同日快照是否与已存的那一天完全一致。"""
    today = hist[pd.to_datetime(hist["effective_from"], errors="coerce").dt.normalize() == day]
    if len(today) == 0:
        return False
    a = set(map(tuple, today[keys].astype(str).to_numpy()))
    b = set(map(tuple, cur[keys].astype(str).to_numpy())) if len(cur) else set()
    return a == b


# --------------------------------------------------------------------------- #
# 财务报告期
# --------------------------------------------------------------------------- #
def report_periods_for_range(start: DateLike, end: DateLike, *, lookback_days: int = _ANNOUNCE_LOOKBACK_DAYS) -> list[pd.Timestamp]:
    """区间 ``[start, end]`` 内**可能被公告**的报告期（季末）列表。

    公告日总是晚于报告期，故起点要向前回溯 ``lookback_days``（保守上界）；
    多抓的行会被 ``announce_date ∈ [start, end]`` 过滤掉，**漏抓则直接少数据**，
    因此这里宁可多抓。

    边界：区间倒置（``end < start``）返回空列表 —— 否则回溯会把「倒置」悄悄变成
    「抓一整年」，那是悄无声息的行为差异。
    """
    lo_start = pd.Timestamp(start).normalize()
    hi = pd.Timestamp(end).normalize()
    if hi < lo_start:
        return []
    lo = lo_start - pd.Timedelta(days=int(lookback_days))
    periods = pd.date_range(lo, hi, freq="QE")
    return [p.normalize() for p in periods]


# --------------------------------------------------------------------------- #
# 端点 → 客户端调用参数（**公开**：provider 与探测工具共用同一实现）
# --------------------------------------------------------------------------- #
def client_kwargs_for(
    ep: Endpoint,
    *,
    symbol: str | None = None,
    start: DateLike | None = None,
    end: DateLike | None = None,
    adjust: str = "",
    report_period: DateLike | None = None,
    industry_name: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """逻辑端点 → akshare 调用参数（**候选映射**，集中在 ENDPOINTS + 此处）。

    **为什么是公开函数**：`tools/probe_akshare.py` 必须用与 provider **完全相同**的
    参数去探测，否则探测结论（列名、单位、分页）对 provider 无效。若探测工具自己
    再写一份参数构造，两边就会漂移 —— 而漂移的表现是「探测报告说通了、留痕也齐全，
    实际取数却用错参数」，属于本项目反复出现的「两份实现」缺陷。

    因此这里是**单一实现**：

    - provider 在 :meth:`AKShareProvider._fetch_remote` 里调用它；
    - 探测工具直接 import 它（不得访问私有名）；
    - `tests/test_probe_akshare.py::test_probe_uses_same_kwargs_as_provider`
      会用同一次假客户端调用账，交叉比对「provider 实际发出的参数」与
      「探测构造的参数」，签名一改就红灯。

    改动本函数会**同时影响取数与探测**，所以任何调整都必须让上面那条用例通过。
    """
    if ep.fn == "stock_zh_a_hist":
        return {
            "symbol": str(symbol).split(".")[0],
            "period": "daily",
            "start_date": pd.Timestamp(start).strftime("%Y%m%d"),
            "end_date": pd.Timestamp(end).strftime("%Y%m%d"),
            "adjust": "hfq" if ep.dataset == "bars_hfq" else "",
        }
    if ep.fn == "stock_individual_info_em":
        return {"symbol": str(symbol).split(".")[0]}
    if ep.fn == "tool_trade_date_hist_sina":
        return {}
    if ep.fn == "index_stock_cons_csindex":
        return {"symbol": str(symbol).split(".")[0]}
    if ep.fn == "stock_board_industry_name_em":
        return {}
    if ep.fn == "stock_board_industry_cons_em":
        return {"symbol": industry_name}
    if ep.fn == "stock_yjbb_em":
        return {"date": pd.Timestamp(report_period).strftime("%Y%m%d")}
    return dict(kwargs)


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #
class AKShareProvider:
    """AKShare 数据源（M4-8：映射骨架；联网抓取由 M5 固化端点与列名）。

    Args:
        config: 数据层配置（缓存/限流/重试/失败策略/上市日口径都取自这里）。
        client: akshare 客户端（生产传真模块；测试传 ``FakeAKShareClient``）。
            ``None`` 时**惰性** ``import akshare``，缺失即抛 ``DataError``。
        cache: 本地缓存；``None`` 且 ``config.cache.enabled`` 时按配置自建。
        limiter: 限流器；``None`` 时按 ``config.rate_limit`` 自建。
        sleeper / random: 注入给 `retry_call`（测试不真睡、抖动可复现）。
        now: 注入「当前时间」的函数（测试固定快照日）。
    """

    name = "akshare"

    def __init__(
        self,
        config: DataConfig | Mapping[str, Any] | None = None,
        *,
        client: Any | None = None,
        cache: DataCache | None = None,
        limiter: RateLimiter | None = None,
        sleeper: Callable[[float], None] | None = None,
        random: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = as_config(config, DataConfig) if config is not None else DataConfig()
        self._client = client
        self._now = now or utc_now
        self._sleeper = sleeper
        self._random = random

        if cache is not None:
            self._cache: DataCache | None = cache
        elif self.config.cache.enabled:
            root = resolve_path(self.config.cache.root)
            assert root is not None
            self._cache = DataCache(
                root,
                version=self.config.cache.version,
                fmt=self.config.cache.fmt,
                ttl_hours=self.config.cache.ttl_hours,
                provider=self.name,
            )
        else:
            self._cache = None

        if limiter is not None:
            self._limiter = limiter
        else:
            rl = self.config.rate_limit
            self._limiter = RateLimiter(
                requests_per_minute=rl.requests_per_minute if rl.enabled else 0,
                burst=rl.burst,
                min_interval_ms=rl.min_interval_ms,
            )

        self._retry_on = resolve_exceptions(self.config.retry.retry_on)
        #: 每次公开调用重置的统计（供 Provenance 与测试断言）
        self._stats: dict[str, Any] = {}
        self._reset_stats()
        #: 抓取进度审计（内存保留；缓存启用时同时落盘 audit.log）
        self._audit: list[dict[str, Any]] = []
        self._audit_path = self._resolve_audit_path()

    @property
    def audit_records(self) -> list[dict[str, Any]]:
        """本次进程内写过的抓取审计记录（测试与诊断用）。"""
        return list(self._audit)

    def cache_manifest(self) -> list[CacheMeta]:
        """缓存清单（供 `ingest_from_provider` 写进 `IngestReport.manifest`）。

        没有缓存能力的 provider **不提供**本方法 —— 适配层据此区分
        「未启用缓存」与「缓存清单为空」，而不是让调用方猜。
        """
        return self._cache.manifest() if self._cache is not None else []

    # ------------------------------------------------------------------ #
    # 客户端
    # ------------------------------------------------------------------ #
    def _resolve_audit_path(self) -> Path | None:
        """抓取审计落盘路径（`data/cache/akshare/audit.log`）；缓存关闭则仅内存。"""
        if self._cache is None:
            return None
        return self._cache.root / self.name / "audit.log"

    def _client_or_raise(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import akshare  # noqa: PLC0415 - 惰性导入：未安装时不影响其它 provider
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise DataError(
                "使用 provider=akshare 需要安装 akshare：pip install akshare"
                "（离线环境请改用 provider=synthetic/csv/parquet）"
            ) from exc
        self._client = akshare
        return akshare

    # ------------------------------------------------------------------ #
    # 能力 / 健康
    # ------------------------------------------------------------------ #
    def capabilities(self) -> ProviderCapabilities:
        """能力**如实**声明：只给当前值的接口不得声称历史能力（见模块文档）。"""
        notes = [
            "字段映射基于候选接口名，未经联网探测（M5 用 tools/probe_akshare.py 固化）",
            f"日线成交量已应用 手→股 换算（volume × {_DEFAULT_VOLUME_TO_SHARES:g}）",
            "index_members 声明为 False：候选接口只返回**当前**成分；调用时返回快照并附警告",
            "industry 为快照累积（effective_from/effective_to）：首次抓取日之前的行业不可得",
            "停牌/涨跌停/ST 历史/退市/市值 未实现（M5 补充），对应能力为 False",
        ]
        if float(self.config.unit_conversion.volume_to_shares) != 1.0 and float(
            self.config.unit_conversion.volume_to_shares
        ) != _DEFAULT_VOLUME_TO_SHARES:
            notes.append(
                f"data.unit_conversion.volume_to_shares={self.config.unit_conversion.volume_to_shares:g} "
                f"对 akshare 无效：该源成交量固定为手，映射按 ×{_DEFAULT_VOLUME_TO_SHARES:g} 处理"
            )
        return ProviderCapabilities(
            daily_bars=True,
            adjustment_factors=True,
            listing_dates=True,
            fundamentals=True,
            industry=True,
            suspensions=False,
            price_limits=False,
            st_flags=False,
            delistings=False,
            index_members=False,
            market_cap=False,
            intraday=False,
            default_adjust="hfq",
            notes=tuple(notes),
        )

    def health_check(self) -> ProviderHealth:
        """**不联网**的健康检查：只验证「客户端可用 + 缓存目录可写位置已知」。

        联网探测属于 M5（`tools/probe_akshare.py`）。``details["network_probe"]=False``
        是刻意写明的：避免把「本地能构造」误读成「接口通」。
        """
        started = self._now()
        errors: list[str] = []
        details: dict[str, Any] = {
            "network_probe": False,
            "endpoints": len(ENDPOINTS),
            "cache_root": str(self._cache.root) if self._cache is not None else None,
            "cache_enabled": self._cache is not None,
            "limiter_enabled": bool(self._limiter.enabled),
        }
        try:
            client = self._client_or_raise()
            details["client"] = getattr(client, "__name__", type(client).__name__)
            ok = True
        except DataError as exc:
            ok = False
            details["client"] = None
            errors.append(str(exc))
        latency = (self._now() - started).total_seconds() * 1000.0
        return ProviderHealth(
            ok=ok, checked_at=self._now(), latency_ms=latency, details=details, errors=tuple(errors)
        )

    # ------------------------------------------------------------------ #
    # 统计与溯源
    # ------------------------------------------------------------------ #
    def _reset_stats(self) -> None:
        self._stats = {
            "network_calls": 0,
            "network_successes": 0,
            "cache_hits": 0,
            "stale_fallbacks": 0,
            "incremental_baselines": 0,
            "dropped_rows": 0,
            "retries": 0,
        }

    def _provenance(
        self,
        frame: pd.DataFrame | None = None,
        *,
        rows: int | None = None,
        warnings: Sequence[str] = (),
        source: str | None = None,
    ) -> Provenance:
        data = frame if frame is not None else pd.DataFrame()
        n_rows = int(len(data)) if rows is None else int(rows)
        symbols = int(data["symbol"].nunique()) if len(data) and "symbol" in data.columns else 0
        # cache_hit 的语义是「**最终供给数据的是缓存**」，不是「有没有发起过网络请求」：
        # 降级回退（stale fallback）时网络被尝试过但失败了，数据仍来自缓存，故为 True。
        served_from_cache = self._stats["cache_hits"] + self._stats["stale_fallbacks"]
        cache_hit = bool(served_from_cache > 0 and self._stats["network_successes"] == 0)
        return Provenance(
            source=source or self.name,
            fetched_at=self._now(),
            cache_hit=cache_hit,
            rows=n_rows,
            symbols=symbols,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _audit_progress_needed(index: int, total: int) -> bool:
        """是否需要写进度审计：标的数超过阈值时，每 ``_AUDIT_EVERY`` 个写一条。"""
        return total > _AUDIT_THRESHOLD and index % _AUDIT_EVERY == 0

    def _audit_progress(self, endpoint: str, done: int, total: int, failures: int) -> None:
        """抓取进度审计（标的数超过阈值时按 ``_AUDIT_EVERY`` 节流）。"""
        record = {
            "ts": self._now().isoformat(),
            "category": "data.akshare",
            "action": "fetch_progress",
            "endpoint": endpoint,
            "done": int(done),
            "total": int(total),
            "failures": int(failures),
        }
        logger.info("akshare 抓取进度 %s：%s/%s（失败 %s）", endpoint, done, total, failures)
        if self._audit_path is None:
            self._audit.append(record)
            return
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # 审计落盘失败不应中断取数
            logger.warning("抓取审计写入失败（不影响取数）：%s", exc)
        self._audit.append(record)

    # ------------------------------------------------------------------ #
    # 缓存 key / 参数哈希
    # ------------------------------------------------------------------ #
    def _cache_key(self, endpoint: str, *, symbol: str | None = None, adjust: str = "",
                   report_period: DateLike | None = None, industry_name: str | None = None) -> str:
        ep = ENDPOINTS[endpoint]
        if ep.key_kind == "symbol_adjust":
            return f"{symbol}_{adjust or 'raw'}"
        if ep.key_kind == "symbol":
            return str(symbol)
        if ep.key_kind == "report_period":
            return f"{pd.Timestamp(report_period).date()}" if report_period is not None else "all"
        if ep.key_kind == "industry":
            return str(industry_name)
        return "all"

    def _cache_params(self, endpoint: str, *, adjust: str = "") -> dict[str, Any]:
        """口径类参数（**不含日期区间**，否则增量永远失效）。"""
        ep = ENDPOINTS[endpoint]
        return {
            "endpoint": endpoint,
            "fn": ep.fn,
            "mapper": ep.mapper,
            "adjust": adjust,
            "volume_to_shares": _DEFAULT_VOLUME_TO_SHARES,
            "member_columns": list(INDEX_MEMBER_COLUMNS) if ep.dataset == "index_members" else None,
        }

    # ------------------------------------------------------------------ #
    # 核心：限流 → 重试 → 缓存 → 映射
    # ------------------------------------------------------------------ #
    def _call(
        self,
        endpoint: str,
        *,
        symbol: str | None = None,
        start: DateLike | None = None,
        end: DateLike | None = None,
        adjust: str = "",
        report_period: DateLike | None = None,
        industry_name: str | None = None,
        snapshot_date: DateLike | None = None,
        warnings: list[str] | None = None,
        mapper_kwargs: Mapping[str, Any] | None = None,
        **client_kwargs: Any,
    ) -> pd.DataFrame:
        """一次逻辑取数（顺序：**缓存 → 限流 → 重试 → 映射 → 回写**）。

        与 `docs/11` §5 的「限流→重试→缓存」顺序不同，理由：缓存命中还先限流等于白排队，
        且「完全不用触碰网络」才是缓存存在的意义（该顺序已在 `docs/14` 记录）。

        增量收窄：``incremental`` 策略下若存在陈旧缓存，则只请求
        ``(缓存最后日期 + 1 天) ~ end``，并与缓存合并 —— 这样既省配额又不丢历史。
        若缓存已覆盖到 ``end``，**一个请求都不发**并如实警告「未刷新」。
        """
        ep = ENDPOINTS[endpoint]
        warnings = warnings if warnings is not None else []
        key = self._cache_key(
            endpoint, symbol=symbol, adjust=adjust, report_period=report_period,
            industry_name=industry_name,
        )
        params = self._cache_params(endpoint, adjust=adjust)

        stale: pd.DataFrame | None = None
        last_fetched: str | None = None
        if self._cache is not None and ep.strategy != "snapshot":
            # 时间戳一律取自 provider 的可注入时钟：这样「新鲜/陈旧」在测试里完全可控，
            # 不依赖真实时间流逝（也避免 ttl<=0 被误读为「永不过期」这类陷阱）。
            lookup: CacheLookup = self._cache.lookup(
                ep.dataset, key, params=params, now=self._now()
            )
            if lookup.hit and not self.config.cache.refresh:
                self._stats["cache_hits"] += 1
                return lookup.frame if lookup.frame is not None else pd.DataFrame()
            stale = lookup.frame if not self.config.cache.refresh else None
            if lookup.meta is not None:
                last_fetched = lookup.meta.fetched_at
            if stale is not None and len(stale):
                self._stats["incremental_baselines"] += 1

        fetch_start: DateLike | None = start
        if ep.strategy == "incremental" and stale is not None and len(stale):
            covered_until = _frame_max(stale, _DATE_HINT.get(ep.dataset))
            if covered_until is not None:
                if end is not None and covered_until >= pd.Timestamp(end).date():
                    # 缓存已覆盖请求区间：不发请求，但必须披露「这是旧数据」
                    self._stats["cache_hits"] += 1
                    warnings.append(
                        f"{key} 缓存已覆盖请求区间（截至 {covered_until}，抓取于 "
                        f"{last_fetched or '未知'}），本次未刷新缓存"
                    )
                    return stale
                fetch_start = covered_until + timedelta(days=1)

        try:
            fresh = self._fetch_remote(ep, symbol=symbol, start=fetch_start, end=end, adjust=adjust,
                                       report_period=report_period, industry_name=industry_name,
                                       **client_kwargs)
        except DataError:
            raise
        except Exception as exc:  # noqa: BLE001 - 失败策略在此统一处理
            return self._on_fetch_failure(
                ep, key, params, stale, last_fetched, exc, warnings, symbol=symbol
            )
        self._stats["network_successes"] += 1

        mapped = MAPPERS[ep.mapper](
            fresh,
            symbol=symbol,
            report_period=report_period or end,
            industry_name=industry_name,
            snapshot_date=snapshot_date or self._now().date(),
            **dict(mapper_kwargs or {}),
        )
        for note in mapped.attrs.get("warnings", ()):  # mapper 通过 attrs 回报诊断
            warnings.append(str(note))
        if mapped.attrs.get("dropped_rows"):
            self._stats["dropped_rows"] += int(mapped.attrs["dropped_rows"])

        if ep.strategy == "snapshot":
            return self._merge_snapshot(ep, key, params, mapped, snapshot_date, warnings)

        merged = self._merge_incremental(ep, stale, mapped, warnings)
        if self._cache is not None:
            self._cache.write(
                ep.dataset, key, merged, params=params, now=self._now(),
                start=_frame_min(merged, _DATE_HINT.get(ep.dataset)),
                end=_frame_max(merged, _DATE_HINT.get(ep.dataset)),
            )
        return merged

    def _fetch_remote(
        self, ep: Endpoint, *, symbol: str | None, start: DateLike | None, end: DateLike | None,
        adjust: str, report_period: DateLike | None, industry_name: str | None, **kwargs: Any,
    ) -> pd.DataFrame:
        """限流 + 重试的真实调用（**唯一触碰客户端的地方**）。"""
        client = self._client_or_raise()
        fn = getattr(client, ep.fn, None)
        if fn is None:
            raise DataError(
                f"akshare 客户端没有函数 {ep.fn}（接口名是候选值，请探测后在 ENDPOINTS 单点修正）"
            )
        call_kwargs = client_kwargs_for(
            ep, symbol=symbol, start=start, end=end, adjust=adjust,
            report_period=report_period, industry_name=industry_name, **kwargs
        )

        def _do() -> pd.DataFrame:
            self._limiter.acquire()
            self._stats["network_calls"] += 1
            return fn(**call_kwargs)

        retry_kwargs: dict[str, Any] = {
            "max_attempts": self.config.retry.max_attempts,
            "backoff": self.config.retry.backoff,
            "jitter": self.config.retry.jitter,
            "retry_on": self._retry_on,
            "on_retry": self._on_retry,
        }
        if self._sleeper is not None:
            retry_kwargs["sleeper"] = self._sleeper
        if self._random is not None:
            retry_kwargs["random"] = self._random
        return retry_call(_do, **retry_kwargs)

    def _on_retry(self, attempt: int, exc: BaseException, delay: float) -> None:
        self._stats["retries"] += 1
        logger.warning("akshare 第 %s 次重试（等待 %.2fs）：%s", attempt, delay, exc)

    def _on_fetch_failure(
        self,
        ep: Endpoint,
        key: str,
        params: Mapping[str, Any],
        stale: pd.DataFrame | None,
        last_fetched: str | None,
        exc: BaseException,
        warnings: list[str],
        *,
        symbol: str | None,
    ) -> pd.DataFrame:
        """抓取失败后的处置：``fallback`` 退回本地快照并**显式披露**；``fail`` 直接抛。"""
        policy = self.config.failure_policy
        if policy == "fail":
            raise DataError(
                f"akshare 端点 {ep.fn}（{key}）抓取失败且 failure_policy=fail：{exc}"
            ) from exc
        if stale is None or len(stale) == 0:
            raise DataError(
                f"akshare 端点 {ep.fn}（{key}）抓取失败，且本地无可用快照可回退：{exc}"
            ) from exc
        self._stats["stale_fallbacks"] += 1
        covered = _frame_max(stale, _DATE_HINT.get(ep.dataset))
        warnings.append(
            f"{ep.fn}（{key}）抓取失败，已退回本地快照"
            f"（数据截至 {covered or '未知'}，抓取于 {last_fetched or '未知'}，共 {len(stale)} 行）：{exc}"
        )
        return stale

    @staticmethod
    def _merge_incremental(
        ep: Endpoint, stale: pd.DataFrame | None, mapped: pd.DataFrame, warnings: list[str]
    ) -> pd.DataFrame:
        """增量合并：按主键去重（后取到的覆盖先前的），并断言**不丢数据**。

        主键从**实际列**推导（``date`` + 有则 ``symbol``）：``bars_hfq`` 只保留
        ``date/close_hfq``（它是复权因子的中间产物，不是 canonical 行情），
        若硬编码 ``(date, symbol)`` 会在增量路径上直接 KeyError。
        """
        if stale is None or len(stale) == 0:
            return mapped.reset_index(drop=True)
        keys = _merge_keys(mapped)
        if set(map(str, stale.columns)) != set(map(str, mapped.columns)):
            warnings.append(
                f"缓存列集与当前映射不一致（缓存 {sorted(map(str, stale.columns))} / "
                f"当前 {sorted(map(str, mapped.columns))}）；按当前列集重建，旧列缺失处为 NaN"
            )
        past = stale.reindex(columns=mapped.columns)
        merged = (
            pd.concat([past, mapped], ignore_index=True)
            .drop_duplicates(subset=list(keys), keep="last")
            .sort_values(list(keys))
            .reset_index(drop=True)
        )
        if len(merged) < len(past):  # pragma: no cover - 防御性断言
            raise DataError(
                f"增量合并后行数减少（{len(past)} → {len(merged)}），缓存合并逻辑有误"
            )
        return merged

    def _merge_snapshot(
        self,
        ep: Endpoint,
        key: str,
        params: Mapping[str, Any],
        mapped: pd.DataFrame,
        snapshot_date: DateLike | None,
        warnings: list[str],
    ) -> pd.DataFrame:
        """快照累积：读状态 → 纯函数累积 → 有新快照才回写。"""
        day = pd.Timestamp(snapshot_date or self._now().date()).normalize()
        cached: pd.DataFrame | None = None
        if self._cache is not None:
            cached, meta = self._cache.read(ep.dataset, key)
            if cached is not None and meta is not None and meta.schema_version != self._cache.version:
                warnings.append(
                    f"快照状态由 schema v{meta.schema_version} 累积（当前 v{self._cache.version}）；"
                    "已保留历史并继续累积（不可再生的观测数据优先保全）"
                )
        merged, action = _accumulate_snapshot(cached, mapped, day)
        if action == "same_day_ignored":
            warnings.append(
                f"同日（{day.date()}）已有快照且内容不同：保留首次观测，不覆盖（本次快照被忽略）"
            )
        if action == "appended" and self._cache is not None:
            self._cache.write(
                ep.dataset, key, merged, params=params, now=self._now(),
                start=_frame_min(merged, "effective_from"),
                end=_frame_max(merged, "effective_from"),
            )
        return merged

    # ------------------------------------------------------------------ #
    # 协议实现
    # ------------------------------------------------------------------ #
    def fetch_bars(
        self, symbols: Sequence[str], start: DateLike, end: DateLike, *, adjust: str = "none"
    ) -> tuple[pd.DataFrame, Provenance]:
        """日线。**逐标的调用**（akshare 一次只查一个代码），部分失败按阈值升级。"""
        self._reset_stats()
        wanted = [normalize_akshare_symbol(s) for s in symbols] if symbols else []
        if not wanted:
            empty = pd.DataFrame(columns=["date", "symbol", "open", "high", "low", "close",
                                          "volume", "amount", "adj_factor"])
            return empty, self._provenance(empty, warnings=("未指定标的：请显式给出 symbols",))

        mode = adjust if adjust != "none" else self.config.adjustment
        frames: list[pd.DataFrame] = []
        warnings: list[str] = []
        failures: list[str] = []
        reasons: dict[str, str] = {}
        total = len(wanted)
        for index, symbol in enumerate(wanted, start=1):
            try:
                frame = self._call(
                    "bars_raw", symbol=symbol, start=start, end=end, adjust=mode, warnings=warnings
                )
                if mode == "hfq":
                    hfq = self._call(
                        "bars_hfq", symbol=symbol, start=start, end=end, adjust=mode, warnings=warnings
                    )
                    frame = derive_adj_factor(frame, hfq)
                    for note in frame.attrs.get("warnings", ()):
                        warnings.append(f"{symbol}: {note}")
                if len(frame) == 0:
                    failures.append(symbol)
                    reasons[symbol] = "无行情（停牌/退市/未上市）"
                    warnings.append(f"{symbol} 在 [{start}, {end}] 无行情（停牌/退市/未上市）")
                    continue
                frames.append(frame)
            except DataError as exc:
                failures.append(symbol)
                reasons[symbol] = str(exc)
                warnings.append(f"{symbol} 取数失败：{exc}")
            if self._audit_progress_needed(index, total):
                self._audit_progress("bars", index, total, len(failures))

        ratio = len(failures) / total if total else 0.0
        if ratio > float(self.config.max_missing_ratio):
            detail = [f"{s}: {reasons.get(s, '')[:120]}" for s in failures[:3]]
            raise DataError(
                f"{len(failures)}/{total} 个标的取数失败（{ratio:.1%}），"
                f"超过 data.max_missing_ratio={self.config.max_missing_ratio:.1%}；"
                f"失败标的示例：{failures[:5]}；原因示例：{detail}"
            )
        if not frames:
            empty = pd.DataFrame(columns=["date", "symbol", "open", "high", "low", "close",
                                          "volume", "amount", "adj_factor"])
            return empty, self._provenance(empty, warnings=tuple(warnings))

        merged = (
            pd.concat(frames, ignore_index=True)
            .sort_values(list(_BARS_KEYS))
            .reset_index(drop=True)
        )
        return merged, self._provenance(merged, warnings=tuple(warnings))

    def fetch_symbol_meta(self, symbols: Sequence[str]) -> tuple[pd.DataFrame, Provenance]:
        """股票基本信息（含 ``list_date``，缺失即报错）。"""
        self._reset_stats()
        wanted = [normalize_akshare_symbol(s) for s in symbols] if symbols else []
        if not wanted:
            empty = pd.DataFrame(columns=["symbol", "name", "list_date", "delist_date", "industry"])
            return empty, self._provenance(empty, warnings=("未指定标的：请显式给出 symbols",))
        frames: list[pd.DataFrame] = []
        warnings: list[str] = []
        for symbol in wanted:
            try:
                frames.append(self._call("symbol_meta", symbol=symbol, warnings=warnings))
            except DataError as exc:
                # list_date 缺失是**硬错误**（缺陷 #8）：不静默跳过，直接向上抛
                raise DataError(f"{symbol} 基本信息取数失败：{exc}") from exc
        merged = pd.concat(frames, ignore_index=True).sort_values("symbol").reset_index(drop=True)
        return merged, self._provenance(merged, warnings=tuple(warnings))

    def fetch_index_members(
        self, index_code: str, start: DateLike, end: DateLike
    ) -> tuple[pd.DataFrame, Provenance]:
        """指数成分：返回**当前快照累积**并按区间相交过滤，同时**明确披露它不满足历史语义**。"""
        self._reset_stats()
        code = str(index_code).strip().upper()
        if "." not in code:
            raise DataError(
                f"指数代码必须带交易所后缀（收到 {index_code!r}）：裸代码无法区分 000300.SH 与 399300.SZ"
            )
        warnings: list[str] = []
        day = pd.Timestamp(self._now().date()).normalize()
        state = self._call(
            "index_members",
            symbol=code,
            snapshot_date=day,
            warnings=warnings,
            mapper_kwargs={"index_code": code},
        )
        frame = filter_effective_window(state, start, end)
        detail = (
            f"指数 {code} 的成分来自**当前**快照（抓取日 {day.date()}），"
            f"请求区间 [{pd.Timestamp(start).date()}, {pd.Timestamp(end).date()}]；"
            "累积表只覆盖我们实际观测过的日期，首次抓取日之前的历史成分不可得 —— "
            "把它当历史成分用会造成幸存者偏差，故 capabilities().index_members=False"
        )
        warnings.append(detail)
        if len(frame) == 0:
            warnings.append(f"指数 {code} 在 [{start}, {end}] 无观测到的成分记录")
        return frame.copy(), self._provenance(frame, warnings=tuple(warnings))

    def fetch_fundamentals(
        self, symbols: Sequence[str], start: DateLike, end: DateLike
    ) -> tuple[pd.DataFrame, Provenance]:
        """财务：按报告期抓取 → 只保留 ``announce_date ∈ [start, end]`` 的行（PIT 口径）。"""
        self._reset_stats()
        columns = ["symbol", "report_period", "announce_date", "eps", "revenue", "net_profit", "roe"]
        wanted = {normalize_akshare_symbol(s) for s in symbols} if symbols else None
        warnings: list[str] = []
        frames: list[pd.DataFrame] = []
        for period in report_periods_for_range(start, end):
            frame = self._call(
                "fundamentals", report_period=period, start=start, end=end, warnings=warnings
            )
            if len(frame):
                frames.append(frame)
        if not frames:
            empty = pd.DataFrame(columns=columns)
            warnings.append(f"区间 [{start}, {end}] 内没有可用的财务报告期数据")
            return empty, self._provenance(empty, warnings=tuple(warnings))

        merged = pd.concat(frames, ignore_index=True)
        announce = pd.to_datetime(merged["announce_date"], errors="coerce")
        mask = (announce >= pd.Timestamp(start)) & (announce <= pd.Timestamp(end))
        dropped = int((~mask).sum())
        merged = merged.loc[mask]
        if wanted is not None:
            merged = merged.loc[merged["symbol"].isin(wanted)]
        merged = (
            merged.drop_duplicates(subset=["symbol", "report_period", "announce_date"], keep="last")
            .sort_values(["announce_date", "symbol"])
            .reset_index(drop=True)
        )
        if dropped:
            warnings.append(f"{dropped} 行公告日不在请求区间内，已按 PIT 口径排除")
        return merged, self._provenance(merged, warnings=tuple(warnings))

    def fetch_trading_calendar(
        self, start: DateLike, end: DateLike
    ) -> tuple[list[Any], Provenance]:
        """交易日历：缓存存表（单列 ``date``），对外转升序去重的 ``list[date]``。"""
        self._reset_stats()
        frame = self._call("calendar", start=start, end=end)
        columns = [c for c in frame.columns if str(c) == "date"]
        if not columns:
            return [], self._provenance(None, rows=0, warnings=("交易日历缺少 date 列",))
        ts = pd.to_datetime(frame["date"], errors="coerce").dropna()
        mask = (ts >= pd.Timestamp(start)) & (ts <= pd.Timestamp(end))
        days = sorted({t.date() for t in ts[mask]})
        empty = pd.DataFrame({"date": pd.to_datetime(days)})
        return days, self._provenance(empty, rows=len(days))

    def fetch_industry(self, symbols: Sequence[str]) -> tuple[pd.DataFrame, Provenance]:
        """行业：板块列表 → 各板块成分 → 快照累积 → 过滤到请求标的。"""
        self._reset_stats()
        columns = ["symbol", "industry", "effective_from", "effective_to"]
        boards = self._call("board_list")
        warnings: list[str] = []
        if len(boards) == 0:
            empty = pd.DataFrame(columns=columns)
            warnings.append("行业板块列表为空，无法取得行业快照（接口不可用或返回空）")
            return empty, self._provenance(empty, warnings=tuple(warnings))

        day = pd.Timestamp(self._now().date()).normalize()
        frames: list[pd.DataFrame] = []
        for board in boards["industry"].astype(str).tolist():
            frame = self._call(
                "industry", industry_name=board, snapshot_date=day, warnings=warnings
            )
            if len(frame):
                frames.append(frame)
        state = (
            pd.concat(frames, ignore_index=True)
            if frames
            else pd.DataFrame(columns=columns)
        )
        wanted = {normalize_akshare_symbol(s) for s in symbols} if symbols else None
        if wanted is not None:
            state = state.loc[state["symbol"].isin(wanted)]
        warnings.append(
            f"行业为快照累积（最近观测日 {day.date()}）：首次抓取日之前的行业不可得，"
            "区间以 effective_from/effective_to 表示"
        )
        return state.reset_index(drop=True), self._provenance(state, warnings=tuple(warnings))


#: 缓存元信息里的区间列（按数据集选择语义最合适的日期列；缺省表示不推断）
_DATE_HINT: dict[str, str] = {
    "bars": "date",
    "bars_hfq": "date",
    "calendar": "date",
    "symbol_meta": "list_date",
    "fundamentals": "announce_date",
    "index_members": "effective_from",
    "industry": "effective_from",
}


def _frame_min(frame: pd.DataFrame, column: str | None) -> Any:
    if frame is None or len(frame) == 0 or not column or column not in frame.columns:
        return None
    values = pd.to_datetime(frame[column], errors="coerce").dropna()
    return None if len(values) == 0 else values.min().date()


def _frame_max(frame: pd.DataFrame, column: str | None) -> Any:
    if frame is None or len(frame) == 0 or not column or column not in frame.columns:
        return None
    values = pd.to_datetime(frame[column], errors="coerce").dropna()
    return None if len(values) == 0 else values.max().date()
