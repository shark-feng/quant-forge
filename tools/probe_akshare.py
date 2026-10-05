"""AKShare 端点探测（第三轮 M5-1）。**离线可跑**，全程不联网也能验收。

为什么需要它
------------
M4-8 的 `ENDPOINTS` / `MAPPERS` 里的函数名与中文列名都是**候选值**（未联网核实）。
本工具把候选变成**实测**：逐个候选端点发起一次最小调用，记录

1. **存在性**：客户端上有没有这个函数（akshare 是模块级函数，拼错只有运行时才炸）；
2. **调用成功与否**：一次最小参数调用，失败按类型归类；
3. **返回形状**：行数、**完整列名清单**、每列 dtype；
4. **耗时**：`--repeats` 次采样的**中位数**；
5. **分页**：签名里是否有分页参数，有则额外试一页并记录返回行数；
6. **失败模式**：`not_found / network / token / timeout / empty / error` 分类；
7. **单位推断**：`amount / (volume × close)` 的量级 → 成交量是「手」还是「股」、
   成交额是「元」还是「千元」（Q3 陷阱：把「手」当「股」会让成交额/参与率/冲击成本
   **整体错 100 倍且不报错**）；
8. **公告日**：财务接口是否有可用的公告日期列（缺了则 M6 的 PIT 因子**做不了**）。

产物是 `docs/data/akshare_capability_report.{json,md}`，供 M5-5 在 `ENDPOINTS`/`MAPPERS`
**单点修正**；报告页首强制带「生成命令 + 口径 + 来源」三件套。

三条必须写明的语义
------------------
1. **`--timeout` 是「判定超时」，不是「中断调用」**。Python 无法强杀线程，因此它的语义是：
   若某次调用耗时超过该秒数，则把该端点标为 `timeout` 并**如实记录耗时**；
   若底层函数签名接受 `timeout` 参数则**透传**（报告里 `timeout_passed=True`），
   否则**不做任何强制中断**。
2. **`mapper_check` 是只读调用**：它把探测到的原始表喂给 M4-8 的 `MAPPERS`，看映射器能否
   消费（缺列时映射器自己会列出候选列）。它**不写缓存、不发新请求、不改全局状态**；
   `MAPPERS` 必须保持纯函数 —— 未来若引入副作用，本条声明与
   `tests/test_probe_akshare.py::test_mapper_check_is_read_only` 必须同步更新。
3. **`dry-run` 的产物目录与真实报告分开**（`<out>/dry-run/`），且报告里明确写
   「fixture 是手工构造，非真实抓取」：dry-run 只证明**脚本自身能正确执行**
   （所以退出码恒为 0，可当 CI 的 smoke test），它**不能**证明接口可用。

退出码
------
- `--dry-run`：**恒 0**（脚本自身跑通即通过，用于 CI）；
- 真实探测：至少一个端点 `ok` → `0`；**全部失败** → `2`。

用法::

    # 离线自检（CI 用；不联网、不装 akshare）
    python tools\\probe_akshare.py --dry-run

    # 真实探测（联网环境；先装 pip install -e ".[data]"）
    python tools\\probe_akshare.py --out docs/data --symbols 600000,000001 --index 000300.SH
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(_ROOT / "src"), str(_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from aqs.config.schema import DataConfig  # noqa: E402
from aqs.core.provenance import build_invocation  # noqa: E402
from aqs.data.akshare_provider import (  # noqa: E402
    ENDPOINTS,
    MAPPERS,
    Endpoint,
    client_kwargs_for,
)
from aqs.data.provider import utc_now  # noqa: E402
from aqs.data.ratelimit import RateLimiter  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量（口径集中在这里；不得散落在逻辑里）
# --------------------------------------------------------------------------- #
SCHEMA_VERSION = 1
DEFAULT_OUT = "docs/data"
DRY_RUN_SUBDIR = "dry-run"
REPORT_STEM = "akshare_capability_report"
SCHEMA_FILE = "docs/data/probe_report.schema.json"

DEFAULT_SYMBOLS: tuple[str, ...] = ("600000", "000001")
DEFAULT_INDEX = "000300.SH"
DEFAULT_TIMEOUT = 30.0
DEFAULT_DAYS = 10
DEFAULT_REPEATS = 1

#: 探测顺序：**必须覆盖 `ENDPOINTS` 的每一个键**（有守护用例），且 `board_list`
#: 必须排在 `industry` 之前 —— industry 端点需要先拿到一个板块名才能调用。
PROBE_ORDER: tuple[str, ...] = (
    "bars_raw",
    "bars_hfq",
    "symbol_meta",
    "calendar",
    "index_members",
    "board_list",
    "industry",
    "fundamentals",
)

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_NOT_FOUND = "not_found"
STATUS_NETWORK = "network"
STATUS_TOKEN = "token"
STATUS_TIMEOUT = "timeout"
STATUS_ERROR = "error"
STATUS_DEPENDENCY_MISSING = "dependency_missing"

#: 状态词表（写进报告，避免读者自行猜测每个词的含义）
STATUS_VOCABULARY: dict[str, str] = {
    STATUS_OK: "调用成功且返回非空",
    STATUS_EMPTY: "接口存在、调用成功，但本次参数返回 0 行（非交易日区间/空板块/无该报告期）",
    STATUS_NOT_FOUND: "客户端上不存在该函数（akshare 未安装，或接口改名 → 需改 ENDPOINTS）",
    STATUS_NETWORK: "网络类失败（连不上/被拒），接口本身可能没问题",
    STATUS_TOKEN: "疑似缺少凭据/token（异常信息命中 token/认证/权限等关键词）",
    STATUS_TIMEOUT: "判定超时：调用耗时超过 --timeout（未强制中断，可能已返回数据）",
    STATUS_ERROR: "未分类异常，需人工判读",
    STATUS_DEPENDENCY_MISSING: (
        "依赖未满足，**本次未发请求**（例如板块列表为空导致 industry 端点无法确定参数）；"
        "刻意不叫 skipped：ingest 层的 skipped 语义是「用户明确未请求」，两者不可混用"
    ),
}

#: 判为「缺凭据」的关键词（小写匹配）
_TOKEN_KEYWORDS: tuple[str, ...] = (
    "token",
    "apikey",
    "api key",
    "unauthorized",
    "forbidden",
    "认证",
    "权限",
    "授权",
    "登录",
)

#: 分页参数的候选名（签名分析用）
_PAGE_PARAMS: tuple[str, ...] = (
    "page",
    "page_num",
    "page_no",
    "page_size",
    "pagesize",
    "per_page",
    "limit",
    "offset",
    "start_page",
)

#: 单位判定矩阵：``ratio = amount / (volume × close)`` 的量级 → (成交量单位, 成交额单位)。
#: 真实 AKShare 日线是「手 / 元」→ ratio = 100（1 手 = 100 股，成交额 = 股数 × 价）。
_UNIT_MATRIX: tuple[tuple[float, str, str], ...] = (
    (1.0, "股", "元"),
    (100.0, "手", "元"),
    (1e-3, "股", "千元"),
    (1e-1, "手", "千元"),
)

#: 量级判定的容差（十进制数量级）：各档之间相隔 1 个数量级，取 0.4 可留出明确间隔
_UNIT_TOLERANCE_DECADES = 0.4

_UNIT_METHOD = "ratio = Σamount / Σ(volume × close)，按数量级就近判定（容差 ±0.4 个数量级）"
_UNIT_NOTE_UNKNOWN = "无法判定单位，禁止上线：需人工核对样本行与 amount/(volume×close)"

#: 样例行数（Q4 修正：只取前 2 行）
SAMPLE_ROWS = 2


# --------------------------------------------------------------------------- #
# 状态分类（纯函数）
# --------------------------------------------------------------------------- #
def classify_status(exc: BaseException | None, *, rows: int | None = None) -> str:
    """把一次探测的结果归入状态词表（纯函数，不触碰客户端）。

    Args:
        exc: 调用抛出的异常；``None`` 表示调用正常返回。
        rows: 正常返回时的行数（``exc is None`` 时必须给出）。
    """
    if exc is None:
        if rows is None:
            raise ValueError("classify_status: exc 为 None 时必须给出 rows（编程错误）")
        return STATUS_EMPTY if int(rows) == 0 else STATUS_OK

    if isinstance(exc, (AttributeError, NotImplementedError)):
        return STATUS_NOT_FOUND

    message = f"{type(exc).__name__}: {exc}".lower()
    # 顺序有意义：TimeoutError 与 ConnectionError 都是 OSError 的子类，
    # 必须先判更具体的类型，否则会全部落进 network。
    if isinstance(exc, TimeoutError) or "timed out" in message or "timeout" in message:
        return STATUS_TIMEOUT
    if any(keyword in message for keyword in _TOKEN_KEYWORDS):
        return STATUS_TOKEN
    if isinstance(exc, (ConnectionError, OSError)):
        return STATUS_NETWORK
    return STATUS_ERROR


def _status_note(status: str) -> str:
    """给状态补一句「为什么会这样、下一步做什么」（不改变状态本身）。"""
    if status == STATUS_NOT_FOUND:
        return "客户端没有这个函数 → 接口改名或 akshare 未安装；需在 ENDPOINTS 单点修正"
    if status == STATUS_NETWORK:
        return "网络不通/连接被拒 → 探测环境无法访问数据源；接口可用性仍未知"
    if status == STATUS_TOKEN:
        return "疑似缺少凭据/token（AKShare 多数接口不需要，命中请核对异常原文）"
    if status == STATUS_TIMEOUT:
        return "判定超时（未强制中断）；耗时见 elapsed_ms，可加大 --timeout 复测"
    if status == STATUS_EMPTY:
        return "接口存在但本次参数返回 0 行 → 换区间/换标的复测，勿据此判定接口不可用"
    if status == STATUS_ERROR:
        return "未分类异常，需人工判读异常原文"
    return ""


# --------------------------------------------------------------------------- #
# 单位推断（纯函数）
# --------------------------------------------------------------------------- #
def classify_units(
    frame: pd.DataFrame | None,
    *,
    volume_candidates: Sequence[str] = ("成交量", "volume"),
    amount_candidates: Sequence[str] = ("成交额", "amount"),
    close_candidates: Sequence[str] = ("收盘", "close"),
) -> dict[str, Any]:
    """由 ``amount / (volume × close)`` 的量级推断成交量/成交额单位（纯函数）。

    返回 ``{"volume","amount","ratio","method","rows_used","note"}``；
    判定不了时 ``volume``/``amount`` 为「未知」并把原始 ``ratio`` 一并报出（供人工判读）。
    """
    out: dict[str, Any] = {
        "volume": "未知",
        "amount": "未知",
        "ratio": None,
        "method": _UNIT_METHOD,
        "rows_used": 0,
        "note": "",
    }
    if frame is None or len(frame) == 0:
        out["note"] = "无数据行，无法推断单位"
        return out

    columns = [str(c) for c in frame.columns]
    picked = {}
    for label, candidates in (
        ("volume", volume_candidates),
        ("amount", amount_candidates),
        ("close", close_candidates),
    ):
        hit = next((c for c in candidates if c in columns), None)
        if hit is None:
            out["note"] = f"缺少 {label} 列（候选 {list(candidates)} 都不在 {columns} 中），无法推断单位"
            return out
        picked[label] = hit

    volume = pd.to_numeric(frame[picked["volume"]], errors="coerce")
    amount = pd.to_numeric(frame[picked["amount"]], errors="coerce")
    close = pd.to_numeric(frame[picked["close"]], errors="coerce")
    valid = volume.notna() & amount.notna() & close.notna() & (volume > 0) & (close > 0)
    used = int(valid.sum())
    out["rows_used"] = used
    if used == 0:
        out["note"] = "无有效行（volume/close 非正或缺失），无法推断单位"
        return out

    denominator = float((volume[valid] * close[valid]).sum())
    if not math.isfinite(denominator) or denominator <= 0:
        out["note"] = "volume × close 之和非正，无法推断单位"
        return out
    ratio = float(amount[valid].sum()) / denominator
    if not math.isfinite(ratio) or ratio <= 0:
        out["note"] = f"ratio={ratio} 非正或非有限，无法推断单位"
        return out
    out["ratio"] = round(ratio, 6)

    log_ratio = math.log10(ratio)
    best = min(_UNIT_MATRIX, key=lambda entry: abs(log_ratio - math.log10(entry[0])))
    if abs(log_ratio - math.log10(best[0])) > _UNIT_TOLERANCE_DECADES:
        out["note"] = _UNIT_NOTE_UNKNOWN
        return out
    out["volume"], out["amount"] = best[1], best[2]
    out["note"] = f"按量级判定为 {best[1]}/{best[2]}（ratio={ratio:.4g}，用 {used} 行）"
    return out


# --------------------------------------------------------------------------- #
# 公告日 / 分页 / 映射器（纯函数，只读）
# --------------------------------------------------------------------------- #
def check_announce_date(
    frame: pd.DataFrame | None,
    *,
    candidates: Sequence[str] = ("最新公告日期", "公告日期", "announce_date"),
) -> dict[str, Any]:
    """财务接口是否有可用的公告日期列（PIT 口径的前提）。

    ``pit_ok`` 为假意味着**M6 的 PIT 财务因子无法做**：宁可明确说不能做，也不能用报告期顶替
    （那是典型的财务未来函数）。
    """
    out: dict[str, Any] = {
        "column": None,
        "present": False,
        "non_null_ratio": None,
        "pit_ok": False,
        "note": "",
    }
    if frame is None or len(frame) == 0:
        out["note"] = "无数据行，无法核验公告日期"
        return out
    columns = [str(c) for c in frame.columns]
    hit = next((c for c in candidates if c in columns), None)
    if hit is None:
        out["note"] = (
            f"缺少公告日期列（候选 {list(candidates)} 都不在 {columns} 中）"
            "→ M6 的 PIT 财务因子无法做，必须先补数据源"
        )
        return out
    values = pd.to_datetime(frame[hit], errors="coerce")
    ratio = float(values.notna().mean()) if len(values) else 0.0
    out.update(
        column=hit,
        present=True,
        non_null_ratio=ratio,
        pit_ok=bool(ratio > 0),
        note=f"公告日期列 {hit} 非空比例 {ratio:.1%}",
    )
    if not out["pit_ok"]:
        out["note"] += "→ 该列全空，等于没有公告日，M6 的 PIT 财务因子无法做"
    return out


def check_pagination(fn: Callable[..., Any]) -> dict[str, Any]:
    """分页能力：**签名分析**（纯函数，不发请求）。

    真正的「试一页」由 :func:`probe_endpoint` 完成（它才有客户端与参数）；
    本函数只回答「签名里有没有分页参数、默认值是多少」。
    """
    out: dict[str, Any] = {
        "supported": False,
        "page_param": None,
        "page_size": None,
        "attempted": False,
        "ok": None,
        "rows_in_page": None,
        "error": None,
        "note": "",
    }
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError) as exc:  # 非 Python 函数或无法反射
        out["note"] = f"无法读取签名（{type(exc).__name__}: {exc}），分页能力未知"
        return out

    params = list(signature.parameters)
    found = next((name for name in params if name.lower() in _PAGE_PARAMS), None)
    if found is None:
        out["note"] = f"签名无分页参数（参数：{params}）；本次不注入任何分页参数"
        return out

    default = signature.parameters[found].default
    out.update(
        supported=True,
        page_param=found,
        page_size=int(default) if isinstance(default, int) and not isinstance(default, bool) else None,
        note=f"签名含分页参数 {found}（默认 {default!r}）",
    )
    return out


def mapper_check(
    mapper_name: str,
    frame: pd.DataFrame | None,
    *,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把原始表喂给 M4-8 的映射器，看它能否消费（**只读调用**）。

    缺列时映射器自己会抛出「候选列都不在实测列中」的 `DataError` —— 这正是我们想要的
    结论文案（**单一实现**：候选清单只在 `MAPPERS` 里维护一次）。
    """
    mapper = MAPPERS.get(mapper_name)
    if mapper is None:
        return {"ok": False, "rows": None, "warnings": [], "error": f"未登记的映射器：{mapper_name}"}
    try:
        mapped = mapper(frame if frame is not None else pd.DataFrame(), **dict(context or {}))
    except Exception as exc:  # noqa: BLE001 - 探测必须吞掉一切异常并如实记录
        return {
            "ok": False,
            "rows": None,
            "warnings": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "ok": True,
        "rows": int(len(mapped)),
        "warnings": [str(w) for w in mapped.attrs.get("warnings", ())],
        "error": None,
    }


# --------------------------------------------------------------------------- #
# 探测上下文
# --------------------------------------------------------------------------- #
def latest_quarter_end(day: Any) -> pd.Timestamp:
    """``≤ day`` 的最近**已过**季末（财务报告期口径）。

    刻意取「已过」的那个季末，而不是「当季未到的季末」：

    - ``2022-03-15`` → ``2021-12-31``（**不是** ``2022-03-31``）；
    - ``2022-03-31``（当天就是季末）→ ``2022-03-31``；
    - ``2022-01-01`` → ``2021-12-31``；``2022-04-01`` → ``2022-03-31``。

    理由：未到季末 / 尚未公布的报告期会让 ``stock_yjbb_em`` 返回**空表**，
    使用者会把「参数不对」误读成「接口不可用」——探测工具最忌讳给出这种假结论。
    """
    ts = pd.Timestamp(day).normalize()
    if ts == ts + pd.offsets.MonthEnd(0) and ts.month in (3, 6, 9, 12):
        return ts
    quarter_end_month = ((ts.month - 1) // 3) * 3
    if quarter_end_month == 0:
        return pd.Timestamp(year=ts.year - 1, month=12, day=31)
    return pd.Timestamp(year=ts.year, month=quarter_end_month, day=1) + pd.offsets.MonthEnd(0)


#: ``report_period`` 缺省时的取值理由（写进报告，避免读者以为是「随便挑的日期」）
_REPORT_PERIOD_REASON = (
    "未显式指定 → 取 ≤ --end 的最近**已过**季末；避开未到季末/尚未公布的报告期，"
    "否则 fundamentals 接口会返回空表，被误判为「接口不可用」"
)


@dataclass(slots=True)
class ProbeContext:
    """一次探测的**固定口径**（窗口、标的、重试次数、超时阈值）。

    ``end_source`` / ``report_period_source`` 记录「这个日期是算出来的还是传进来的」：
    命令字符串里的 ``--end`` 可以省略（不可复现），但**报告里的日期必须可复现**，
    且读者要能一眼看出它是默认推导还是显式指定。
    """

    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    index_code: str = DEFAULT_INDEX
    start: pd.Timestamp = field(default_factory=lambda: pd.Timestamp("1970-01-01"))
    end: pd.Timestamp = field(default_factory=lambda: pd.Timestamp("1970-01-01"))
    report_period: pd.Timestamp = field(default_factory=lambda: pd.Timestamp("1970-01-01"))
    timeout: float = DEFAULT_TIMEOUT
    repeats: int = DEFAULT_REPEATS
    end_source: str = ""
    report_period_source: str = ""
    report_period_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "index_code": self.index_code,
            "window": [str(self.start.date()), str(self.end.date())],
            "end_source": self.end_source,
            "report_period": str(self.report_period.date()),
            "report_period_source": self.report_period_source,
            "report_period_reason": self.report_period_reason,
            "timeout_seconds": self.timeout,
            "repeats": self.repeats,
        }


def make_context(
    *,
    symbols: Sequence[str] = DEFAULT_SYMBOLS,
    index_code: str = DEFAULT_INDEX,
    end: Any = None,
    days: int = DEFAULT_DAYS,
    report_period: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
    repeats: int = DEFAULT_REPEATS,
) -> ProbeContext:
    """构造探测上下文。

    ``end`` 缺省为**今天（UTC）**；``start = end - days``；``report_period`` 缺省为
    ``≤ end`` 的最近**已过**季末。解析后的**绝对日期与来源**都写进报告，
    保证「报告里的日期」可复现（命令里省略了 ``--end`` 也不影响复查）。
    """
    if not symbols:
        raise ValueError("symbols 不能为空：探测需要至少一个标的")
    if days < 1:
        raise ValueError("days 至少为 1")
    if repeats < 1:
        raise ValueError("repeats 至少为 1")
    if timeout <= 0:
        raise ValueError("timeout 必须为正")

    explicit_end = end is not None
    last_day = pd.Timestamp(end).normalize() if explicit_end else pd.Timestamp(utc_now().date())
    explicit_period = report_period is not None
    period = (
        pd.Timestamp(report_period).normalize() if explicit_period else latest_quarter_end(last_day)
    )
    return ProbeContext(
        symbols=tuple(str(s) for s in symbols),
        index_code=str(index_code),
        start=last_day - pd.Timedelta(days=int(days)),
        end=last_day,
        report_period=period,
        timeout=float(timeout),
        repeats=int(repeats),
        end_source="cli(--end)" if explicit_end else "default(今天, UTC)",
        report_period_source=(
            "cli(--report-period)" if explicit_period else "derived(最近已过季末)"
        ),
        report_period_reason=(
            f"由 --report-period 显式指定（{period.date()}）"
            if explicit_period
            else f"{_REPORT_PERIOD_REASON}（实际取 {period.date()}）"
        ),
    )


# --------------------------------------------------------------------------- #
# 单端点探测
# --------------------------------------------------------------------------- #
def _cell(value: Any) -> str:
    """样例单元格一律字符串化（Q4 修正）：``None`` → 空串，其余 ``str()``。"""
    return "" if value is None else str(value)


def _sample(frame: pd.DataFrame | None) -> dict[str, Any]:
    if frame is None or len(frame) == 0:
        return {"columns": [], "rows": []}
    head = frame.head(SAMPLE_ROWS)
    return {
        "columns": [str(c) for c in frame.columns],
        "rows": [[_cell(v) for v in row] for row in head.itertuples(index=False, name=None)],
    }


def _client_label(client: Any) -> str:
    return str(getattr(client, "__name__", None) or type(client).__name__)


def _probe_kwargs(
    ep: Endpoint, ctx: ProbeContext, *, industry_name: str | None
) -> dict[str, Any]:
    """探测用的调用参数：**复用 provider 的单一实现**（不得自行拼一套）。

    注意「查谁」这件事按端点语义分派：指数成分查的是**指数代码**，不是股票代码
    （用 ``ctx.symbols[0]`` 去查指数会安静地返回空表 —— dry-run 实测踩到过）。
    """
    identifier = ctx.index_code if ep.mapper == "index_members" else (
        ctx.symbols[0] if ctx.symbols else None
    )
    return client_kwargs_for(
        ep,
        symbol=identifier,
        start=ctx.start,
        end=ctx.end,
        adjust="hfq" if ep.dataset == "bars_hfq" else "",
        report_period=ctx.report_period,
        industry_name=industry_name,
    )


def _mapper_context(
    ep: Endpoint, ctx: ProbeContext, *, industry_name: str | None
) -> dict[str, Any]:
    """映射器的上下文参数（与 provider 调用映射器时保持一致的口径）。"""
    symbol = ctx.symbols[0] if ctx.symbols else None
    snapshot_date = ctx.end.date()
    if ep.mapper in ("bars_raw", "bars_hfq", "symbol_meta"):
        return {"symbol": symbol}
    if ep.mapper == "calendar":
        return {}
    if ep.mapper == "index_members":
        return {"index_code": ctx.index_code, "snapshot_date": snapshot_date}
    if ep.mapper == "board_list":
        return {}
    if ep.mapper == "industry":
        return {"industry_name": industry_name, "snapshot_date": snapshot_date}
    if ep.mapper == "fundamentals":
        return {"report_period": ctx.report_period}
    return {}


def _blank_pagination() -> dict[str, Any]:
    """未做探测时的分页字段（保持与 :func:`check_pagination` 同构）。"""
    return {
        "supported": False,
        "page_param": None,
        "page_size": None,
        "attempted": False,
        "ok": None,
        "rows_in_page": None,
        "error": None,
        "note": "",
    }


def _blank_record(endpoint_id: str, ep: Endpoint) -> dict[str, Any]:
    return {
        "endpoint": endpoint_id,
        "fn": ep.fn,
        "dataset": ep.dataset,
        "mapper": ep.mapper,
        "capability": ep.capability,
        "strategy": ep.strategy,
        "status": STATUS_ERROR,
        "columns": [],
        "dtypes": {},
        "rows": None,
        "elapsed_ms": None,
        "elapsed_samples_ms": [],
        "attempts": [],
        "kwargs": {},
        "timeout_passed": False,
        "timeout_exceeded": False,
        "pagination": _blank_pagination(),
        "unit": classify_units(None),
        "mapper_check": {"ok": None, "rows": None, "warnings": [], "error": None},
        "sample": {"columns": [], "rows": []},
        "error": None,
        "note": "",
    }


def probe_endpoint(
    client: Any,
    endpoint_id: str,
    *,
    ctx: ProbeContext,
    industry_name: str | None = None,
    limiter: RateLimiter | None = None,
) -> dict[str, Any]:
    """探测单个端点：存在性 → 参数 → 限流 → 调用（repeats 次）→ 形状/单位/映射器。"""
    ep = ENDPOINTS[endpoint_id]
    record = _blank_record(endpoint_id, ep)

    fn = getattr(client, ep.fn, None)
    if fn is None or not callable(fn):
        record["status"] = STATUS_NOT_FOUND
        reason = getattr(client, "reason", None)
        message = f"客户端没有函数 {ep.fn}（{_client_label(client)}）"
        if reason:
            message += f"：{reason}"
        record["error"] = message
        record["note"] = _status_note(STATUS_NOT_FOUND)
        return record

    kwargs = _probe_kwargs(ep, ctx, industry_name=industry_name)
    signature: inspect.Signature | None
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        signature = None
    if signature is not None and "timeout" in signature.parameters:
        kwargs = {**kwargs, "timeout": ctx.timeout}
        record["timeout_passed"] = True
    record["kwargs"] = {key: _cell(value) for key, value in kwargs.items()}

    record["pagination"] = check_pagination(fn)
    if record["pagination"]["supported"]:
        page_kwargs = {**kwargs, record["pagination"]["page_param"]: record["pagination"]["page_size"] or 1}
        try:
            if limiter is not None:
                limiter.acquire()
            page_frame = fn(**page_kwargs)
            rows_in_page = int(len(page_frame))
            record["pagination"].update(attempted=True, ok=True, rows_in_page=rows_in_page)
            record["pagination"]["note"] += (
                f"；已额外试一页（{record['pagination']['page_param']}="
                f"{page_kwargs[record['pagination']['page_param']]}），返回 {rows_in_page} 行"
                "（单页上限需人工确认：一次调用无法证明上限）"
            )
        except Exception as exc:  # noqa: BLE001 - 分页试探失败不改变主调用结论
            record["pagination"].update(
                attempted=True, ok=False, error=f"{type(exc).__name__}: {exc}"
            )
            record["pagination"]["note"] += f"；试页失败：{type(exc).__name__}: {exc}"

    samples: list[float] = []
    frames: list[pd.DataFrame] = []
    errors: list[str] = []
    first_error: BaseException | None = None
    for attempt in range(1, ctx.repeats + 1):
        started = time.perf_counter()
        frame: pd.DataFrame | None = None
        exc: BaseException | None = None
        try:
            if limiter is not None:
                limiter.acquire()
            frame = fn(**kwargs)
        except Exception as caught:  # noqa: BLE001 - 探测必须吞掉一切异常并归类
            exc = caught
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        samples.append(round(elapsed_ms, 3))
        record["attempts"].append(
            {
                "attempt": attempt,
                "ok": exc is None,
                "rows": None if frame is None else int(len(frame)),
                "elapsed_ms": round(elapsed_ms, 3),
                "error": None if exc is None else f"{type(exc).__name__}: {exc}",
            }
        )
        if exc is not None:
            errors.append(f"{type(exc).__name__}: {exc}")
            if first_error is None:
                first_error = exc
        else:
            frames.append(frame)

    record["elapsed_samples_ms"] = samples
    record["elapsed_ms"] = round(statistics.median(samples), 3) if samples else None

    if not frames:
        record["status"] = classify_status(first_error) if first_error is not None else STATUS_ERROR
        record["error"] = "; ".join(errors) if errors else "调用未返回任何结果"
        record["note"] = _status_note(record["status"])
        return record

    frame = frames[0]
    rows = int(len(frame))
    record["columns"] = [str(c) for c in frame.columns]
    record["dtypes"] = {str(c): str(dtype) for c, dtype in frame.dtypes.items()}
    record["rows"] = rows
    record["sample"] = _sample(frame)
    record["unit"] = classify_units(frame)
    if ep.dataset == "bars_hfq":
        # 实测（fixture 与真实 akshare 同构）：后复权表的 close 被复权因子放大，而成交额仍是
        # 原始金额 → ratio 被缩小 ≈ 复权倍数。单位判定必须以不复权的 bars_raw 为准。
        record["unit"]["note"] += (
            "；注意该表 close 是**后复权价**，ratio 会被复权因子缩放 → 单位判定以 bars_raw 为准"
        )
    record["mapper_check"] = mapper_check(
        ep.mapper, frame, context=_mapper_context(ep, ctx, industry_name=industry_name)
    )
    if ep.dataset == "fundamentals":
        record["announce_date_check"] = check_announce_date(frame)

    status = classify_status(None, rows=rows)
    record["status"] = status
    record["note"] = f"返回 {rows} 行 / {len(frame.columns)} 列"
    if status == STATUS_OK and record["elapsed_ms"] is not None:
        threshold_ms = ctx.timeout * 1000.0
        if record["elapsed_ms"] > threshold_ms:
            record["status"] = STATUS_TIMEOUT
            record["timeout_exceeded"] = True
            record["note"] += (
                f"；耗时 {record['elapsed_ms']}ms 超过判定阈值 {threshold_ms:.0f}ms，"
                "判定为超时（数据已返回，未强制中断）"
            )
    if len(errors):  # repeats > 1 时部分次失败：不改变结论，但要披露
        record["note"] += f"；{len(errors)}/{ctx.repeats} 次调用失败（见 attempts）"
    return record


def _dependency_missing_record(
    endpoint_id: str, reason: str, *, upstream: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """依赖未满足（例如拿不到板块名）时的记录：**明确说未发请求**，不算失败也不算成功。

    会把**上游端点的错误原文**一并带上：否则用户只看到「board_list 状态=not_found」，
    却看不到「akshare 没装」这样的根因（探测工具的价值就在于说清为什么失败）。
    """
    ep = ENDPOINTS[endpoint_id]
    record = _blank_record(endpoint_id, ep)
    record["status"] = STATUS_DEPENDENCY_MISSING
    record["note"] = f"依赖未满足，本次未发请求：{reason}"
    upstream_error = (upstream or {}).get("error")
    if upstream_error:
        record["note"] += f"；上游错误：{upstream_error}"
    return record


def _industry_name_from_board(record: Mapping[str, Any]) -> tuple[str | None, str]:
    """从 `board_list` 的探测结果里取一个板块名（不给 industry 端点凭空编参数）。"""
    status = record.get("status")
    if status not in (STATUS_OK, STATUS_TIMEOUT):
        return None, f"board_list 状态={status}"
    rows = record.get("sample", {}).get("rows") or []
    if not rows:
        return None, "board_list 未返回可读样例行"
    columns = [str(c) for c in record.get("sample", {}).get("columns", [])]
    index = next((i for i, c in enumerate(columns) if "名称" in c or "板块" in c), 0)
    values = rows[0]
    if index >= len(values) or not values[index]:
        return None, "board_list 样例行里取不到板块名"
    return str(values[index]), f"取自 board_list 首行（{values[index]}）"


# --------------------------------------------------------------------------- #
# 全量探测
# --------------------------------------------------------------------------- #
def _observed(record: Mapping[str, Any] | None) -> bool:
    """该端点是否**观测到数据**（`ok` 或「判定超时但已返回行」）。"""
    if not record:
        return False
    if record.get("status") not in (STATUS_OK, STATUS_TIMEOUT):
        return False
    return int(record.get("rows") or 0) > 0


def derive_capabilities(endpoints: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """把逐端点的观测结果**机械地**映射成能力清单（每一步都写清依据）。"""
    def basis(name: str) -> str:
        record = endpoints.get(name) or {}
        return f"端点 {name}: status={record.get('status')}, rows={record.get('rows')}"

    bars_ok = _observed(endpoints.get("bars_raw"))
    hfq_ok = _observed(endpoints.get("bars_hfq"))
    meta = endpoints.get("symbol_meta") or {}
    meta_ok = _observed(meta) and bool((meta.get("mapper_check") or {}).get("ok"))
    fundamentals_ok = _observed(endpoints.get("fundamentals"))
    announce = (endpoints.get("fundamentals") or {}).get("announce_date_check") or {}
    board_ok = _observed(endpoints.get("board_list"))
    industry_ok = _observed(endpoints.get("industry"))

    observed: dict[str, Any] = {
        "daily_bars": bars_ok,
        "adjustment_factors": hfq_ok,
        "listing_dates": meta_ok,
        "fundamentals": fundamentals_ok,
        "fundamentals_announce_date": bool(fundamentals_ok and announce.get("pit_ok")),
        "industry": bool(board_ok and industry_ok),
        "index_members_current": _observed(endpoints.get("index_members")),
        "calendar": _observed(endpoints.get("calendar")),
        # 恒 False：单次调用只能观测「当前」成分，历史成分要靠快照累积（M4-8 已如此声明）
        "index_members_historical": False,
        "suspensions": False,
        "price_limits": False,
        "st_flags": False,
        "delistings": False,
        "market_cap": False,
        "intraday": False,
    }
    derivation: dict[str, str] = {
        "daily_bars": basis("bars_raw"),
        "adjustment_factors": basis("bars_hfq"),
        "listing_dates": basis("symbol_meta") + f"，mapper_check.ok={(meta.get('mapper_check') or {}).get('ok')}",
        "fundamentals": basis("fundamentals"),
        "fundamentals_announce_date": basis("fundamentals")
        + f"，pit_ok={announce.get('pit_ok')}（缺公告日则 M6 的 PIT 财务因子无法做）",
        "industry": basis("board_list") + "；" + basis("industry") + "（两者都要有数据）",
        "index_members_current": basis("index_members"),
        "calendar": basis("calendar"),
        "index_members_historical": (
            "单次调用只能观测当前成分；历史成分只能靠逐日快照累积，首次抓取日之前不可得"
        ),
        "suspensions": "本次未登记该能力的候选端点（M4-8 能力表声明未实现）",
        "price_limits": "本次未登记该能力的候选端点（M4-8 能力表声明未实现）",
        "st_flags": "本次未登记该能力的候选端点（M4-8 能力表声明未实现）",
        "delistings": "本次未登记该能力的候选端点（M4-8 能力表声明未实现）",
        "market_cap": "本次未登记该能力的候选端点（M4-8 能力表声明未实现）",
        "intraday": "未探测分钟级接口（本轮不涉及）",
    }
    observed["_derivation"] = derivation
    return observed


def derive_recommendations(
    endpoints: Mapping[str, Mapping[str, Any]], capabilities: Mapping[str, Any]
) -> list[str]:
    """由**观测证据**推导下一步动作（不引入任何独立于探测结果的期望值）。"""
    out: list[str] = []

    for endpoint_id, record in endpoints.items():
        status = record.get("status")
        if status == STATUS_OK:
            continue
        if status == STATUS_DEPENDENCY_MISSING:
            out.append(f"{endpoint_id}: {record.get('note')}")
        else:
            out.append(
                f"{endpoint_id}: 状态={status}（{record.get('error') or record.get('note')}）"
                f"→ 核对 ENDPOINTS['{endpoint_id}'].fn"
            )

    # 单位结论只从**不复权**的 bars_raw 推出：后复权表的 close 被复权因子放大，
    # ratio 会被同比例缩小（实测 fixture ratio≈50 vs raw≈100），据它下结论会误导。
    for endpoint_id in ("bars_raw",):
        record = endpoints.get(endpoint_id) or {}
        if not _observed(record):
            continue
        unit = record.get("unit") or {}
        volume, amount, ratio = unit.get("volume"), unit.get("amount"), unit.get("ratio")
        if volume == "手":
            out.append(
                f"{endpoint_id}: 成交量单位实测为「手」（ratio={ratio}）"
                "→ `_DEFAULT_VOLUME_TO_SHARES=100` 正确，无需改动"
            )
        elif volume == "股":
            out.append(
                f"{endpoint_id}: 实测成交量为「股」（ratio={ratio}）→ **必须**把 "
                "`_DEFAULT_VOLUME_TO_SHARES` 改为 1.0；否则成交量/成交额/参与率/冲击成本"
                "会整体错 100 倍且不报错"
            )
        else:
            out.append(
                f"{endpoint_id}: 成交量单位**无法判定**（ratio={ratio}，{unit.get('note')}）"
                "→ 禁止上线，需人工核对样例行"
            )
        if amount == "千元":
            out.append(
                f"{endpoint_id}: 成交额单位为「千元」→ 映射里必须 ×1000（当前映射**未处理**）"
            )
        elif amount not in (None, "元") and amount != "未知":
            out.append(f"{endpoint_id}: 成交额单位判定为「{amount}」→ 需在映射里换算")
    if not _observed(endpoints.get("bars_raw")) and _observed(endpoints.get("bars_hfq")):
        out.append(
            "bars_raw 未观测到数据，只有 bars_hfq：后复权表的 ratio 受复权因子影响，"
            "**不能**据它判定成交量单位，需先让 bars_raw 可用"
        )

    for endpoint_id, record in endpoints.items():
        check = record.get("mapper_check") or {}
        if check.get("ok") is False:
            out.append(
                f"{endpoint_id}: 映射器 `MAPPERS['{record.get('mapper')}']` 无法消费实测返回"
                f"（{check.get('error')}）→ 在 MAPPERS 单点修正候选列名"
            )

    fundamentals = endpoints.get("fundamentals") or {}
    announce = fundamentals.get("announce_date_check") or {}
    if fundamentals.get("status") in (STATUS_OK, STATUS_TIMEOUT) and not announce.get("pit_ok"):
        out.append(
            "fundamentals: 公告日期不可用 → **M6 的 PIT 财务因子无法做**，必须先补数据源"
            "（绝不能用报告期顶替公告日）"
        )

    if capabilities.get("index_members_current"):
        out.append(
            "index_members: 接口只返回**当前**成分 → capabilities.index_members=False 正确；"
            "历史成分只能靠快照逐日累积（首次抓取日之前不可得）"
        )
    if all((record.get("status") == STATUS_NOT_FOUND) for record in endpoints.values()):
        out.append(
            "全部端点 not_found → akshare 未安装或全部接口改名：先 pip install -e \".[data]\"，"
            "再核对 ENDPOINTS 的函数名"
        )
    if not out:
        out.append("所有端点均探测成功，且单位/公告日/mapper 均无异常：无需修正")
    return out


def probe_all(
    client: Any,
    *,
    ctx: ProbeContext,
    limiter: RateLimiter | None = None,
    mode: str = "live",
    akshare_version: str | None = None,
    out_dir: Path | str = DEFAULT_OUT,
    argv: Sequence[str] | None = None,
    load_error: str | None = None,
) -> dict[str, Any]:
    """按 :data:`PROBE_ORDER` 探测全部端点，返回报告字典（**单个端点失败不中止**）。"""
    endpoints: dict[str, Any] = {}
    industry_name: str | None = None
    industry_source = ""
    for endpoint_id in PROBE_ORDER:
        ep = ENDPOINTS[endpoint_id]
        if ep.key_kind == "industry":
            if industry_name is None:
                industry_name, industry_source = _industry_name_from_board(
                    endpoints.get("board_list") or {}
                )
            if industry_name is None:
                endpoints[endpoint_id] = _dependency_missing_record(
                    endpoint_id, industry_source, upstream=endpoints.get("board_list")
                )
                continue
        record = probe_endpoint(
            client, endpoint_id, ctx=ctx, industry_name=industry_name, limiter=limiter
        )
        if ep.key_kind == "industry":
            record["note"] += f"；板块名来源：{industry_source}"
        endpoints[endpoint_id] = record

    capabilities = derive_capabilities(endpoints)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "probed_at": utc_now().isoformat(),
        "mode": mode,
        "akshare_version": akshare_version,
        "command": " ".join(argv) if argv else "",
        "invocation": build_invocation(list(argv) if argv else None, project_root=_ROOT, extras={"mode": mode}),
        "out": str(out_dir),
        "context": ctx.as_dict(),
        "client": _client_label(client),
        "rate_limit": _rate_limit_summary(limiter),
        "status_vocabulary": dict(STATUS_VOCABULARY),
        "endpoints": endpoints,
        "capabilities_observed": capabilities,
        "recommendations": derive_recommendations(endpoints, capabilities),
        "limitations": _limitations(mode, load_error),
    }
    return report


def _rate_limit_summary(limiter: RateLimiter | None) -> dict[str, Any]:
    """把限流配置与**实际等待**写进报告（耗时数字的解释前提）。"""
    if limiter is None:
        return {"enabled": False, "source": "未构造限流器（探测未限流）"}
    summary: dict[str, Any] = {
        "enabled": bool(limiter.enabled),
        "requests_per_minute": int(limiter.requests_per_minute),
        "burst": int(limiter.burst),
        "min_interval_ms": float(limiter.min_interval_ms),
        "source": "configs/base.yaml → data.rate_limit（经 DataConfig 默认值构造）",
    }
    stats = getattr(limiter, "stats", None)
    if callable(stats):
        summary["stats"] = stats()
    return summary


def _limitations(mode: str, load_error: str | None) -> list[str]:
    common = [
        "`ENDPOINTS` 与 `MAPPERS` 里的函数名/列名在本次探测前都是**候选值**；本报告只证明"
        "「本次调用看到了什么」，不证明接口在未来版本仍如此",
        "耗时是采样中位数，受本机与网络状况影响，只能作量级参考",
        "单次调用无法观测历史成分/历史行业：只返回**当前值**的接口不得声称历史能力",
    ]
    if mode == "dry-run":
        return [
            "**本报告由 `--dry-run` 生成：数据来自 `tests/fixtures/akshare/` 的手工构造样例，"
            "不是真实抓取**；它只证明探测脚本自身能正确执行，不能用于判断接口是否真实可用",
            "dry-run 的退出码恒为 0（脚本自身跑通即通过），因此**不能**用它判断数据源可用性",
            *common,
        ]
    out = ["本报告由真实联网探测生成（akshare 版本见上）", *common]
    if load_error:
        out.append(f"akshare 导入失败：{load_error}")
    return out


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def _yes_no(value: Any) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    return "未知"


def render_markdown(report: Mapping[str, Any]) -> str:
    """把报告渲染成人类可读的 Markdown（数字与 JSON 同源，不另算）。"""
    mode = report.get("mode")
    lines: list[str] = []
    lines.append("# AKShare 端点探测报告")
    lines.append("")
    if mode == "dry-run":
        lines.append(
            "> **模式：dry-run** —— 数据来自 `tests/fixtures/akshare/` 的**手工构造样例**，"
            "**不是真实抓取**。本报告只证明探测脚本自身能正确执行，"
            "不足以判断任何接口是否真实可用。"
        )
    else:
        lines.append(
            f"> **模式：live（真实联网探测）** —— akshare 版本 `{report.get('akshare_version')}`。"
        )
    lines.append("")
    lines.append("## 生成信息（口径 / 来源 / 命令 三件套）")
    lines.append("")
    lines.append(f"- **生成命令**：`{report.get('command')}`")
    lines.append(f"- **生成时间（UTC）**：`{report.get('probed_at')}`")
    lines.append(f"- **来源**：`tools/probe_akshare.py`（schema v{report.get('schema_version')}）")
    lines.append(f"- **格式定义**：`{SCHEMA_FILE}`（JSON Schema；本报告的机器可读格式以它为准）")
    lines.append(f"- **模式**：`{mode}` ｜ **客户端**：`{report.get('client')}`")
    lines.append(f"- **akshare 版本**：`{report.get('akshare_version')}`")
    context = report.get("context") or {}
    lines.append(
        f"- **探测窗口（已解析的绝对日期）**：{context.get('window')}"
        f" ｜ end 来源：{context.get('end_source')}"
    )
    lines.append(
        f"- **财务报告期**：`{context.get('report_period')}`"
        f"（来源：{context.get('report_period_source')}）"
    )
    lines.append(f"- **报告期取值理由**：{context.get('report_period_reason')}")
    lines.append(
        f"- **标的**：{context.get('symbols')} ｜ **指数**：`{context.get('index_code')}`"
    )
    lines.append(
        f"- **超时判定阈值**：{context.get('timeout_seconds')}s（判定超时，**不中断调用**）"
        f" ｜ **耗时采样次数**：{context.get('repeats')}（取中位数）"
    )
    limits = report.get("rate_limit") or {}
    lines.append(f"- **限流**：{limits}")
    lines.append("")
    lines.append("### 口径说明")
    lines.append("")
    lines.append(f"- **单位推断**：{_UNIT_METHOD}（判不出即报「未知」，不猜）")
    lines.append(
        "- **`mapper_check`**：只读调用 —— 把原始表喂给 `MAPPERS` 看能否消费；"
        "不写缓存、不发新请求、不改全局状态"
    )
    lines.append("- **状态词表**：")
    for status, meaning in (report.get("status_vocabulary") or {}).items():
        lines.append(f"  - `{status}`：{meaning}")
    lines.append("")
    lines.append("## 端点总览")
    lines.append("")
    lines.append("| 端点 | 状态 | 行数 | 耗时中位(ms) | 单位(量/额) | mapper | 函数 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for endpoint_id, record in (report.get("endpoints") or {}).items():
        unit = record.get("unit") or {}
        check = record.get("mapper_check") or {}
        lines.append(
            f"| `{endpoint_id}` | `{record.get('status')}` | {record.get('rows')} | "
            f"{record.get('elapsed_ms')} | {unit.get('volume')}/{unit.get('amount')} | "
            f"{_yes_no(check.get('ok'))} | `{record.get('fn')}` |"
        )
    lines.append("")
    lines.append("## 逐端点明细")
    for endpoint_id, record in (report.get("endpoints") or {}).items():
        lines.append("")
        lines.append(f"### {endpoint_id} — status={record.get('status')}")
        lines.append("")
        lines.append(
            f"- **函数**：`{record.get('fn')}` ｜ **数据集**：`{record.get('dataset')}`"
            f" ｜ **映射器**：`{record.get('mapper')}` ｜ **能力**：`{record.get('capability')}`"
        )
        lines.append(f"- **调用参数**：`{record.get('kwargs')}`（timeout 透传={record.get('timeout_passed')}）")
        lines.append(
            f"- **行数**：{record.get('rows')} ｜ **耗时(中位/样本)**：{record.get('elapsed_ms')} ms / "
            f"{record.get('elapsed_samples_ms')} ms ｜ **判定超时**：{_yes_no(record.get('timeout_exceeded'))}"
        )
        lines.append(f"- **列名（{len(record.get('columns') or [])}）**：{record.get('columns')}")
        lines.append(f"- **dtype**：`{record.get('dtypes')}`")
        pagination = record.get("pagination") or {}
        lines.append(
            f"- **分页**：支持={_yes_no(pagination.get('supported'))}"
            f" ｜ 参数=`{pagination.get('page_param')}` ｜ 试页返回行数={pagination.get('rows_in_page')}"
            f" ｜ 说明：{pagination.get('note')}"
        )
        unit = record.get("unit") or {}
        lines.append(
            f"- **单位推断**：成交量=`{unit.get('volume')}` 成交额=`{unit.get('amount')}`"
            f"（ratio={unit.get('ratio')}，用 {unit.get('rows_used')} 行）｜ {unit.get('note')}"
        )
        check = record.get("mapper_check") or {}
        lines.append(
            f"- **映射器可消费**：{_yes_no(check.get('ok'))} ｜ 映射后行数={check.get('rows')}"
            f" ｜ 错误={check.get('error')} ｜ 映射警告={check.get('warnings')}"
        )
        announce = record.get("announce_date_check")
        if announce is not None:
            lines.append(
                f"- **公告日期**：列=`{announce.get('column')}` 存在={_yes_no(announce.get('present'))}"
                f" 非空比例={announce.get('non_null_ratio')} PIT可用={_yes_no(announce.get('pit_ok'))}"
                f" ｜ {announce.get('note')}"
            )
        sample = record.get("sample") or {}
        if sample.get("rows"):
            lines.append("- **样例（前 2 行，字符串化）**：")
            lines.append("")
            lines.append("  | " + " | ".join(str(c) for c in sample.get("columns", [])) + " |")
            lines.append("  | " + " | ".join("---" for _ in sample.get("columns", [])) + " |")
            for row in sample.get("rows", []):
                lines.append("  | " + " | ".join(str(v) for v in row) + " |")
            lines.append("")
        lines.append(f"- **错误**：{record.get('error')}")
        lines.append(f"- **备注**：{record.get('note')}")
    lines.append("")
    lines.append("## 观测到的能力")
    lines.append("")
    lines.append("| 能力 | 观测到 | 依据 |")
    lines.append("| --- | --- | --- |")
    capabilities = report.get("capabilities_observed") or {}
    derivation = capabilities.get("_derivation") or {}
    for name, value in capabilities.items():
        if name == "_derivation":
            continue
        lines.append(f"| `{name}` | {_yes_no(value)} | {derivation.get(name, '')} |")
    lines.append("")
    lines.append("## 建议动作")
    lines.append("")
    for item in report.get("recommendations") or []:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## 本报告的局限")
    lines.append("")
    for item in report.get("limitations") or []:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def write_reports(report: Mapping[str, Any], out_dir: Path | str) -> tuple[Path, Path]:
    """写出 JSON（机器读）与 Markdown（人读）两份报告，返回两者路径。"""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / f"{REPORT_STEM}.json"
    md_path = target / f"{REPORT_STEM}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    md_path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    return json_path, md_path


# --------------------------------------------------------------------------- #
# 客户端构造
# --------------------------------------------------------------------------- #
class _MissingClient:
    """akshare 未安装/导入失败时的占位客户端。

    它**任何函数名都不存在** → 每个端点都会得到 ``status=not_found`` 与一句
    「为什么失败」的原因，而不是让脚本崩掉（用户能看到结论，而不是 traceback）。
    """

    __name__ = "_MissingClient"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"{name}: {self.reason}")


def load_akshare(
    *, importer: Callable[[str], Any] | None = None
) -> tuple[Any, str | None, str | None]:
    """惰性 import akshare。

    Returns:
        ``(client, version, error)``：导入失败时 client 为 :class:`_MissingClient`、
        version 为 ``None``、error 为可读原因（**不抛异常**）。
    """
    load = importer or importlib.import_module
    try:
        module = load("akshare")
    except Exception as exc:  # noqa: BLE001 - 导入期任何失败都退化为占位客户端
        reason = (
            f"import akshare 失败：{type(exc).__name__}: {exc}"
            "（联网环境请 pip install -e \".[data]\"，离线环境请改用 provider=synthetic/csv/parquet）"
        )
        return _MissingClient(reason), None, reason
    version = getattr(module, "__version__", None)
    return module, (None if version is None else str(version)), None


def build_limiter(config: DataConfig | None = None) -> RateLimiter:
    """按配置构造限流器（**不写魔法数字**：取值来自 `DataConfig.rate_limit`）。"""
    rate_limit = (config or DataConfig()).rate_limit
    return RateLimiter(
        requests_per_minute=rate_limit.requests_per_minute if rate_limit.enabled else 0,
        burst=rate_limit.burst,
        min_interval_ms=rate_limit.min_interval_ms,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _symbols_arg(value: str) -> tuple[str, ...]:
    items = tuple(item.strip() for item in str(value).split(",") if item.strip())
    if not items:
        raise argparse.ArgumentTypeError("--symbols 不能为空（示例：--symbols 600000,000001）")
    return items


def _day_arg(value: str) -> pd.Timestamp:
    try:
        parsed = pd.Timestamp(str(value).strip()).normalize()
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError(f"无法解析日期 {value!r}（示例：2022-03-07 或 20211231）") from exc
    if pd.isna(parsed):
        raise argparse.ArgumentTypeError(f"无法解析日期 {value!r}（示例：2022-03-07 或 20211231）")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """CLI 定义（README 的命令必须与本解析器一致，由 M5-4 的守护用例保证）。"""
    parser = argparse.ArgumentParser(
        prog="probe_akshare.py",
        description="探测 AKShare 候选端点的存在性/列名/耗时/分页/单位，产出能力报告",
        epilog=(
            "示例：python tools/probe_akshare.py --dry-run "
            "| python tools/probe_akshare.py --out docs/data --days 10"
        ),
    )
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"报告输出目录（默认 {DEFAULT_OUT}）")
    parser.add_argument(
        "--symbols", type=_symbols_arg, default=DEFAULT_SYMBOLS,
        help=f"探测用标的，逗号分隔（默认 {','.join(DEFAULT_SYMBOLS)}）",
    )
    parser.add_argument("--index", default=DEFAULT_INDEX, help=f"探测用指数代码（默认 {DEFAULT_INDEX}）")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT,
        help=f"**判定超时**阈值（秒，默认 {DEFAULT_TIMEOUT:g}）；不中断调用，只改状态标记",
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS,
        help=f"行情探测窗口长度（自然日，默认 {DEFAULT_DAYS}）；窗口 = [--end - days, --end]",
    )
    parser.add_argument(
        "--repeats", type=int, default=DEFAULT_REPEATS,
        help=f"每个端点调用次数（默认 {DEFAULT_REPEATS}），耗时取中位数；想量耗时用 --repeats 3",
    )
    parser.add_argument(
        "--end", type=_day_arg, default=None,
        help="探测窗口结束日（默认今天，UTC）；固化它才能得到可复现的探测命令",
    )
    parser.add_argument(
        "--report-period", type=_day_arg, default=None, dest="report_period",
        help="财务探测用的报告期（默认 --end 之前最近季末）",
    )
    parser.add_argument("--dry-run", action="store_true", help="用 FakeAKShareClient + fixture 离线走全流程")
    parser.add_argument(
        "--fixture-root", default=None, dest="fixture_root",
        help="dry-run 的 fixture 目录（默认 tests/fixtures/akshare）；只能与 --dry-run 同用",
    )
    return parser


def run_probe(
    args: argparse.Namespace,
    *,
    client: Any,
    mode: str,
    out_dir: Path,
    akshare_version: str | None = None,
    argv: Sequence[str] | None = None,
    limiter: RateLimiter | None = None,
    load_error: str | None = None,
) -> dict[str, Any]:
    """按 CLI 参数执行探测（不写文件；写文件由 :func:`write_reports` 负责）。"""
    ctx = make_context(
        symbols=tuple(args.symbols),
        index_code=args.index,
        end=args.end,
        days=args.days,
        report_period=args.report_period,
        timeout=args.timeout,
        repeats=args.repeats,
    )
    return probe_all(
        client,
        ctx=ctx,
        limiter=limiter if limiter is not None else build_limiter(),
        mode=mode,
        akshare_version=akshare_version,
        out_dir=out_dir,
        argv=argv,
        load_error=load_error,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    client: Any | None = None,
    importer: Callable[[str], Any] | None = None,
) -> int:
    """CLI 入口。

    Args:
        argv: 命令行参数（``None`` 时取 ``sys.argv[1:]``）。
        client: 注入的客户端（测试用；``None`` 时按模式自行构造）。
        importer: 注入的模块导入器（测试「akshare 未安装」路径用）。

    Returns:
        退出码：dry-run 恒 0；真实探测「≥1 个端点 ok」→ 0，全失败 → 2。
    """
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.days < 1:
        parser.error("--days 至少为 1")
    if args.repeats < 1:
        parser.error("--repeats 至少为 1")
    if args.timeout <= 0:
        parser.error("--timeout 必须为正")
    if args.fixture_root and not args.dry_run:
        parser.error("--fixture-root 只能与 --dry-run 一起使用")

    command = [f"python tools/{Path(__file__).name}", *(list(argv) if argv is not None else sys.argv[1:])]
    out_dir = Path(args.out)
    mode = "dry-run" if args.dry_run else "live"
    version: str | None = None
    load_error: str | None = None
    client_obj = client
    if args.dry_run:
        out_dir = out_dir / DRY_RUN_SUBDIR
        if client_obj is None:
            from tests.fake_akshare import FakeAKShareClient  # noqa: PLC0415 - 仅 dry-run 需要

            client_obj = FakeAKShareClient(args.fixture_root)
        version = f"dry-run({_client_label(client_obj)})"
    elif client_obj is None:
        client_obj, version, load_error = load_akshare(importer=importer)

    report = run_probe(
        args,
        client=client_obj,
        mode=mode,
        out_dir=out_dir,
        akshare_version=version,
        argv=command,
        load_error=load_error,
    )
    json_path, md_path = write_reports(report, out_dir)
    ok_count = sum(1 for rec in report["endpoints"].values() if rec["status"] == STATUS_OK)
    print(f"模式={mode} 端点={len(report['endpoints'])} 可用={ok_count}")
    print(f"报告：{json_path} | {md_path}")
    if mode == "dry-run":
        return 0
    return 0 if ok_count > 0 else 2


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
