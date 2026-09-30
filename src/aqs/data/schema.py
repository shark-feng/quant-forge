"""数据规范、归一化与质量校验。

列规范（canonical schema）见 ``docs/01_data_layer.md`` §2。

本模块的三条铁律：
1. **不复权价用于成交/涨跌停/费用**，后复权价只用于信号与收益；
2. 财务数据一律以 ``announce_date``（公告日）为准，``report_period`` 仅作标签；
3. 校验失败必须显式暴露（:class:`DataQualityReport`），不允许静默填充。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from ..config.schema import PriceLimitConfig
from ..core.dates import to_date
from ..core.enums import Board
from ..core.exceptions import SchemaError
from ..core.logging import get_logger

logger = get_logger("data.schema")

__all__ = [
    "BARS_COLUMNS",
    "BARS_REQUIRED",
    "INDEX_MEMBER_COLUMNS",
    "FUNDAMENTAL_COLUMNS",
    "NUMERIC_BAR_COLUMNS",
    "DataQualityReport",
    "infer_board",
    "normalize_symbol",
    "normalize_bars",
    "add_adjusted_prices",
    "compute_limit_prices",
    "validate_bars",
    "normalize_index_members",
    "select_index_members",
    "normalize_fundamentals",
    "select_fundamentals_asof",
]

# ------------------------------- 列规范 ------------------------------------ #
BARS_REQUIRED: tuple[str, ...] = ("date", "symbol", "open", "high", "low", "close", "volume")
"""必需列。``amount`` 不在其中：缺失时会用 ``close × volume`` 估算并给出告警。"""

BARS_COLUMNS: tuple[str, ...] = (
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adj_factor",
    "prev_close",
    "limit_up",
    "limit_down",
    "is_suspended",
    "is_st",
    "list_date",
    "delist_date",
    "board",
    "bar_seq",
    # 派生列
    "open_adj",
    "high_adj",
    "low_adj",
    "close_adj",
    "prev_adj_factor",
    "base_adj_factor",
)

NUMERIC_BAR_COLUMNS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adj_factor",
    "prev_close",
    "limit_up",
    "limit_down",
)

INDEX_MEMBER_COLUMNS: tuple[str, ...] = ("index_code", "symbol", "effective_from", "effective_to")

FUNDAMENTAL_COLUMNS: tuple[str, ...] = ("symbol", "report_period", "announce_date")

_MAIN_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003", "900", "200")
_GEM_PREFIXES = ("300", "301")
_STAR_PREFIXES = ("688", "689")
_BSE_PREFIXES = ("43", "83", "87", "88", "920")


# --------------------------------------------------------------------------- #
# 质量报告
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class DataQualityReport:
    """数据质量报告。``errors`` 非空即表示数据不可用于回测。"""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def merge(self, other: "DataQualityReport") -> "DataQualityReport":
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        self.stats.update(other.stats)
        return self

    def summary(self, *, max_items: int = 8) -> str:
        parts = [
            f"数据质量报告：{len(self.errors)} 个错误 / {len(self.warnings)} 个告警",
            f"统计：{self.stats}",
        ]
        for msg in self.errors[:max_items]:
            parts.append(f"  [错误] {msg}")
        if len(self.errors) > max_items:
            parts.append(f"  ... 其余 {len(self.errors) - max_items} 条错误见 report.errors")
        for msg in self.warnings[:max_items]:
            parts.append(f"  [告警] {msg}")
        return "\n".join(parts)

    def __str__(self) -> str:  # pragma: no cover
        return self.summary()


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def normalize_symbol(symbol: str) -> str:
    """把 ``600000`` / ``sh600000`` / ``600000.sh`` 归一为 ``600000.SH``。"""
    s = str(symbol).strip().upper()
    if "." in s:
        code, _, suffix = s.partition(".")
        suffix = suffix.upper()
        if suffix in ("XSHG", "SS"):
            suffix = "SH"
        elif suffix in ("XSHE",):
            suffix = "SZ"
        return f"{code}.{suffix}"
    for prefix, suffix in (("SH", "SH"), ("SZ", "SZ"), ("BJ", "BJ")):
        if s.startswith(prefix) and len(s) > len(prefix):
            return f"{s[len(prefix):]}.{suffix}"
    # 无后缀：按代码前缀推断
    board = infer_board(s)
    if board in (Board.STAR,):  # 688/689 → 上交所
        return f"{s}.SH"
    if board is Board.GEM:
        return f"{s}.SZ"
    if board is Board.BSE:
        return f"{s}.BJ"
    return f"{s}.SH" if s.startswith("6") else f"{s}.SZ"


def infer_board(symbol: str) -> Board:
    """按证券代码前缀推断板块（决定涨跌停幅度）。"""
    code = str(symbol).split(".")[0].strip()
    if code.startswith(_STAR_PREFIXES):
        return Board.STAR
    if code.startswith(_GEM_PREFIXES):
        return Board.GEM
    if code.startswith(_BSE_PREFIXES):
        return Board.BSE
    if code.startswith(_MAIN_PREFIXES) or len(code) == 6:
        return Board.MAIN
    return Board.MAIN


def _round_cent(series: pd.Series) -> pd.Series:
    """四舍五入到分（A 股报价精度）。"""
    return np.floor(series * 100 + 0.5) / 100.0


# --------------------------------------------------------------------------- #
# 行情归一化
# --------------------------------------------------------------------------- #
def normalize_bars(
    df: pd.DataFrame,
    *,
    price_limit: PriceLimitConfig | None = None,
    compute_limits: bool = True,
) -> pd.DataFrame:
    """把原始行情表归一为 canonical schema，并补全派生列。

    步骤：
      1. 校验必需列 → 缺列抛 :class:`SchemaError`
      2. 类型归一（date/symbol/数值/布尔）
      3. 排序、按 symbol 前向填充静态字段（is_st / list_date / delist_date）
      4. 补 ``prev_close``（用同标的上一条 close）
      5. 补 ``limit_up/limit_down``（优先使用数据自带值）
      6. 计算后复权价与 ``base_adj_factor``
    """
    if df is None or len(df) == 0:
        raise SchemaError("行情表为空")
    missing = [c for c in BARS_REQUIRED if c not in df.columns]
    if missing:
        raise SchemaError(f"行情表缺少必需列：{missing}；必需列：{list(BARS_REQUIRED)}")

    out = df.copy()

    # 1) 类型归一
    out["date"] = pd.to_datetime(out["date"], errors="raise").dt.normalize()
    out["symbol"] = out["symbol"].map(normalize_symbol)

    if "amount" not in out.columns:
        out["amount"] = np.nan
    for col in NUMERIC_BAR_COLUMNS:
        if col not in out.columns:
            out[col] = np.nan
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")

    out["adj_factor"] = out["adj_factor"].fillna(1.0)
    if "is_suspended" not in out.columns:
        # 停牌判定：无成交（volume<=0）视为停牌
        out["is_suspended"] = out["volume"] <= 0
    out["is_suspended"] = out["is_suspended"].fillna(False).astype(bool)
    if "is_st" not in out.columns:
        out["is_st"] = False
    out["is_st"] = out["is_st"].fillna(False).astype(bool)

    for col in ("list_date", "delist_date"):
        if col not in out.columns:
            out[col] = pd.NaT
        out[col] = pd.to_datetime(out[col], errors="coerce").dt.normalize()

    # 2) 排序（保证 shift/cumcount 语义正确）
    out = out.sort_values(["symbol", "date"], kind="stable").reset_index(drop=True)

    # 3) 静态字段按标的补齐（ST 标记通常只在变更日出现；上市/退市日是静态属性）
    grouped = out.groupby("symbol", sort=False, observed=True)
    for col in ("is_st",):
        out[col] = grouped[col].transform(lambda s: s.ffill().bfill()).astype(bool)
    for col in ("list_date", "delist_date"):
        out[col] = grouped[col].transform(lambda s: s.ffill().bfill())

    # 4) 板块
    if "board" not in out.columns or out["board"].isna().any():
        board_map = {sym: infer_board(sym).value for sym in out["symbol"].unique()}
        out["board"] = out["symbol"].map(board_map)
    else:
        out["board"] = out["board"].astype(str)
    out["board"] = out["board"].fillna(out["symbol"].map({s: infer_board(s).value for s in out["symbol"].unique()}))

    # 5) bar_seq：该标的第几根 K 线（上市天数的兜底来源）
    out["bar_seq"] = grouped.cumcount() + 1

    # 6) prev_close / prev_adj_factor：用于涨跌停推算与现金分红入账
    derived_prev = grouped["close"].shift(1)
    out["prev_close"] = out["prev_close"].where(out["prev_close"].notna(), derived_prev)
    out["prev_adj_factor"] = grouped["adj_factor"].shift(1)

    # 7) 涨跌停价
    if compute_limits:
        out = compute_limit_prices(out, price_limit or PriceLimitConfig())

    # 8) 成交额兜底（缺失时用收盘价 × 成交量估算）
    if out["amount"].isna().any():
        derived = out["close"] * out["volume"]
        filled = out["amount"].isna()
        out["amount"] = out["amount"].where(out["amount"].notna(), derived)
        logger.warning("amount 存在 %d 个缺失值，已用 close × volume 估算", int(filled.sum()))

    # 9) 后复权价
    out = add_adjusted_prices(out)

    ordered = [c for c in BARS_COLUMNS if c in out.columns]
    rest = [c for c in out.columns if c not in ordered]
    return out[ordered + rest]


def add_adjusted_prices(df: pd.DataFrame, *, adjustment: str = "hfq") -> pd.DataFrame:
    """计算后复权价。

    后复权价 = 原始价 × adj_factor / adj_factor(该标的首个交易日)
    —— 使首个交易日的后复权价等于原始价，且历史价格序列不因后续分红送股而断裂。
    """
    out = df
    if "adj_factor" not in out.columns:
        out["adj_factor"] = 1.0
        out["adj_factor"] = out["adj_factor"].astype("float64")
    base = out.groupby("symbol", sort=False, observed=True)["adj_factor"].transform("first")
    ratio = (out["adj_factor"] / base).astype("float64")
    for col in ("open", "high", "low", "close"):
        if adjustment == "none":
            out[f"{col}_adj"] = out[col].astype("float64")
        else:
            out[f"{col}_adj"] = out[col].astype("float64") * ratio
    out["base_adj_factor"] = base.astype("float64")
    return out


def compute_limit_prices(df: pd.DataFrame, config: PriceLimitConfig) -> pd.DataFrame:
    """补全涨跌停价。

    优先使用数据自带值（``use_provided_limits``）；缺失时按 ``prev_close`` 与板块规则推算。
    上市首日（``bar_seq == 1`` 且 ``first_day_no_limit``）不设涨跌停（置 NaN = 无限制）。
    """
    out = df
    if not config.enabled:
        out["limit_up"] = np.nan
        out["limit_down"] = np.nan
        return out

    boards = out["board"].astype(str)
    is_st = out["is_st"].astype(bool)
    gem = Board.GEM.value
    star = Board.STAR.value
    bse = Board.BSE.value

    pct = pd.Series(config.main_board_pct, index=out.index, dtype="float64")
    pct = pct.mask(boards.isin([gem, star]), config.gem_pct)
    pct = pct.mask(boards.eq(bse), config.bse_pct)
    pct = pct.mask(is_st & ~boards.isin([gem, star]), config.st_pct)

    prev = out["prev_close"].astype("float64")
    calc_up = prev * (1.0 + pct)
    calc_down = prev * (1.0 - pct)
    if config.round_to_cent:
        calc_up = _round_cent(calc_up)
        calc_down = _round_cent(calc_down)

    if config.use_provided_limits:
        out["limit_up"] = out["limit_up"].where(out["limit_up"].notna(), calc_up)
        out["limit_down"] = out["limit_down"].where(out["limit_down"].notna(), calc_down)
    else:
        out["limit_up"] = calc_up
        out["limit_down"] = calc_down

    if config.first_day_no_limit:
        first_day = out["bar_seq"].eq(1)
        out.loc[first_day, ["limit_up", "limit_down"]] = np.nan
    # 首日无前收盘时同样无法推算
    no_prev = prev.isna()
    out.loc[no_prev, ["limit_up", "limit_down"]] = np.nan
    return out


# --------------------------------------------------------------------------- #
# 质量校验
# --------------------------------------------------------------------------- #
def validate_bars(
    df: pd.DataFrame,
    *,
    check_price_limits: bool = True,
    symbols: Sequence[str] | None = None,
) -> DataQualityReport:
    """对 canonical 行情表做结构与业务校验，返回质量报告（不抛异常）。"""
    report = DataQualityReport()
    if df is None or len(df) == 0:
        report.add_error("行情表为空")
        return report

    missing = [c for c in BARS_REQUIRED if c not in df.columns]
    if missing:
        report.add_error(f"缺少必需列：{missing}")
        return report

    report.stats["rows"] = int(len(df))
    report.stats["symbols"] = int(df["symbol"].nunique())
    report.stats["start"] = str(pd.to_datetime(df["date"]).min().date())
    report.stats["end"] = str(pd.to_datetime(df["date"]).max().date())

    # 主键
    dup = df.duplicated(subset=["date", "symbol"]).sum()
    if dup:
        report.add_error(f"主键 (date, symbol) 存在 {int(dup)} 条重复记录")

    # 价格合法性
    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            continue
        nan_cnt = int(df[col].isna().sum())
        if nan_cnt:
            report.add_error(f"{col} 存在 {nan_cnt} 个缺失值")
        bad = int((df[col] <= 0).sum())
        if bad:
            report.add_error(f"{col} 存在 {bad} 个非正值")

    if {"high", "low", "open", "close"}.issubset(df.columns):
        bad_hl = int((df["high"] < df["low"]).sum())
        if bad_hl:
            report.add_error(f"存在 {bad_hl} 条 high < low 的记录")
        upper = df[["open", "close"]].max(axis=1)
        lower = df[["open", "close"]].min(axis=1)
        bad_range = int(((df["high"] < upper) | (df["low"] > lower)).sum())
        if bad_range:
            report.add_error(f"存在 {bad_range} 条 OHLC 区间不一致（high/low 未覆盖 open/close）的记录")

    # 成交量与成交额
    if "volume" in df.columns:
        neg = int((df["volume"] < 0).sum())
        if neg:
            report.add_error(f"volume 存在 {neg} 个负值")
    if "amount" in df.columns:
        neg_amt = int((df["amount"] < 0).sum())
        if neg_amt:
            report.add_error(f"amount 存在 {neg_amt} 个负值")

    # 复权因子
    if "adj_factor" in df.columns:
        bad_factor = int((df["adj_factor"] <= 0).sum() + df["adj_factor"].isna().sum())
        if bad_factor:
            report.add_error(f"adj_factor 存在 {bad_factor} 个非正/缺失值")

    # 涨跌停
    if check_price_limits and {"limit_up", "limit_down"}.issubset(df.columns):
        both = df["limit_up"].notna() & df["limit_down"].notna()
        inverted = int((both & (df["limit_up"] < df["limit_down"])).sum())
        if inverted:
            report.add_error(f"存在 {inverted} 条 limit_up < limit_down 的记录")
        if "is_suspended" in df.columns:
            tradable = both & ~df["is_suspended"].astype(bool)
            out_of_band = int(
                (
                    tradable
                    & ((df["close"] > df["limit_up"] + 1e-6) | (df["close"] < df["limit_down"] - 1e-6))
                ).sum()
            )
            if out_of_band:
                report.add_warning(
                    f"存在 {out_of_band} 条收盘价越出涨跌停区间的记录（可能是复权/ST规则/新股差异，需人工核查）"
                )

    # 日期与上市/退市一致性
    if {"list_date", "delist_date"}.issubset(df.columns):
        ld = pd.to_datetime(df["list_date"], errors="coerce")
        dd = pd.to_datetime(df["delist_date"], errors="coerce")
        dts = pd.to_datetime(df["date"])
        before_list = int((ld.notna() & (dts < ld)).sum())
        if before_list:
            report.add_error(f"存在 {before_list} 条早于上市日期的记录")
        after_delist = int((dd.notna() & (dts > dd)).sum())
        if after_delist:
            report.add_error(f"存在 {after_delist} 条晚于退市日期的记录")

    # 停牌软告警
    if {"is_suspended", "volume"}.issubset(df.columns):
        susp = df["is_suspended"].astype(bool)
        mismatch = int((susp & (df["volume"] > 0)).sum())
        if mismatch:
            report.add_warning(f"存在 {mismatch} 条标记停牌但成交量大于 0 的记录")

    # 缺口告警（标的交易日覆盖率）
    if {"date", "symbol"}.issubset(df.columns):
        per_symbol = df.groupby("symbol", observed=True)["date"].nunique()
        full = int(pd.to_datetime(df["date"]).nunique())
        thin = per_symbol[per_symbol < full * 0.5]
        if len(thin):
            report.add_warning(
                f"{len(thin)} 个标的的交易日覆盖率不足 50%（可能是新上市/长期停牌/退市）"
            )
    return report


def raise_if_bad(report: DataQualityReport) -> None:
    """在 strict 模式下使用：报告有错误时抛出 :class:`DataQualityError`。"""
    from ..core.exceptions import DataQualityError

    if not report.ok:
        raise DataQualityError(report.summary(), report=report)


# --------------------------------------------------------------------------- #
# 指数成分
# --------------------------------------------------------------------------- #
def normalize_index_members(df: pd.DataFrame) -> pd.DataFrame:
    """归一化指数成分变更表。

    主键 ``(index_code, symbol, effective_from)``；``effective_to`` 为空表示至今有效。
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=list(INDEX_MEMBER_COLUMNS))
    missing = [c for c in ("index_code", "symbol", "effective_from") if c not in df.columns]
    if missing:
        raise SchemaError(f"指数成分表缺少列：{missing}")
    out = df.copy()
    out["index_code"] = out["index_code"].astype(str).str.strip().str.upper()
    out["symbol"] = out["symbol"].map(normalize_symbol)
    out["effective_from"] = pd.to_datetime(out["effective_from"], errors="raise").dt.normalize()
    if "effective_to" not in out.columns:
        out["effective_to"] = pd.NaT
    out["effective_to"] = pd.to_datetime(out["effective_to"], errors="coerce").dt.normalize()
    out = out.sort_values(["index_code", "symbol", "effective_from"], kind="stable").reset_index(drop=True)
    return out[list(INDEX_MEMBER_COLUMNS)]


def select_index_members(members: pd.DataFrame, index_code: str, day: Any) -> list[str]:
    """取某指数在某日**实际生效**的成分股（历史成分，不是当前成分）。"""
    if members is None or len(members) == 0:
        return []
    ts = pd.Timestamp(to_date(day))
    code = str(index_code).strip().upper()
    sub = members[members["index_code"] == code]
    if sub.empty:
        return []
    mask = (sub["effective_from"] <= ts) & (sub["effective_to"].isna() | (sub["effective_to"] >= ts))
    return sorted(sub.loc[mask, "symbol"].unique().tolist())


# --------------------------------------------------------------------------- #
# 财务数据（公告日口径）
# --------------------------------------------------------------------------- #
def normalize_fundamentals(df: pd.DataFrame) -> pd.DataFrame:
    """归一化财务数据表，必须包含 ``report_period``（报告期）与 ``announce_date``（公告日）。"""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=list(FUNDAMENTAL_COLUMNS))
    missing = [c for c in FUNDAMENTAL_COLUMNS if c not in df.columns]
    if missing:
        raise SchemaError(
            f"财务数据缺少列：{missing}。"
            "必须提供 announce_date（公告日），禁止仅凭 report_period（报告期）使用数据。"
        )
    out = df.copy()
    out["symbol"] = out["symbol"].map(normalize_symbol)
    out["report_period"] = pd.to_datetime(out["report_period"], errors="raise").dt.normalize()
    out["announce_date"] = pd.to_datetime(out["announce_date"], errors="raise").dt.normalize()
    future = int((out["announce_date"] < out["report_period"]).sum())
    if future:
        raise SchemaError(f"存在 {future} 条公告日早于报告期的记录，数据不可信")
    value_cols = [c for c in out.columns if c not in ("symbol", "report_period", "announce_date")]
    for col in value_cols:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.sort_values(["symbol", "report_period", "announce_date"], kind="stable").reset_index(drop=True)


def select_fundamentals_asof(
    fundamentals: pd.DataFrame,
    as_of: Any,
    *,
    symbols: Sequence[str] | None = None,
    fields: Sequence[str] | None = None,
) -> pd.DataFrame:
    """按公告日取 ``as_of`` 时点可见的最新财务数据。

    规则：
      1. 只保留 ``announce_date <= as_of`` 的记录（**这是防未来函数的关键**）；
      2. 同一 ``report_period`` 有多条公告时取公告日最大者（修正公告）；
      3. 每个 symbol 只保留最新报告期的一行。

    Returns:
        index 为 symbol 的 DataFrame（列：report_period, announce_date, 财务字段）。
    """
    empty = pd.DataFrame(columns=list(FUNDAMENTAL_COLUMNS) + list(fields or ()))
    if fundamentals is None or len(fundamentals) == 0:
        return empty

    ts = pd.Timestamp(to_date(as_of))
    visible = fundamentals[fundamentals["announce_date"] <= ts]
    if symbols is not None:
        visible = visible[visible["symbol"].isin(list(symbols))]
    if visible.empty:
        return empty

    visible = visible.sort_values(["symbol", "report_period", "announce_date"], kind="stable")
    latest_per_period = visible.drop_duplicates(subset=["symbol", "report_period"], keep="last")
    latest = latest_per_period.sort_values(["symbol", "report_period"], kind="stable").drop_duplicates(
        subset=["symbol"], keep="last"
    )
    id_cols = ("symbol", "report_period", "announce_date")
    if fields is None:
        extra = [c for c in latest.columns if c not in id_cols]
    else:
        extra = [c for c in fields if c in latest.columns and c not in id_cols]
    keep = list(id_cols) + extra
    return latest[keep].set_index("symbol")
