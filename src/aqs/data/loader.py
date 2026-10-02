"""行情装载：CSV / Parquet / 合成数据。

约定（``docs/01_data_layer.md`` §6）：loader 只做字段映射与类型归一，
**禁止**在 loader 内做复权、填充、过滤等“智能”操作 —— 那些属于数据层/store 的职责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

import pandas as pd

from ..config.schema import DataConfig, PriceLimitConfig, UniverseConfig, as_config
from ..core.calendar import TradingCalendar
from ..core.exceptions import DataError, DataQualityError, SchemaError
from ..core.logging import get_logger
from .cache import CacheMeta
from .provider import (
    ProviderCapabilities,
    Provenance,
    degradation_notes,
)
from .quality import ProviderQualityReport, QualityChecker, QualityFinding
from .schema import (
    DataQualityReport,
    normalize_bars,
    normalize_fundamentals,
    normalize_index_members,
    validate_bars,
)
from .store import DataStore

__all__ = [
    "MarketDataBundle",
    "BarDataLoader",
    "CsvBarLoader",
    "ParquetBarLoader",
    "load_market_data",
    "read_table",
    "describe_data_scope",
    "IngestReport",
    "ingest_from_provider",
]

logger = get_logger("data.loader")


@dataclass(slots=True)
class MarketDataBundle:
    """一组原始市场数据（已归一为 canonical schema）。"""

    bars: pd.DataFrame
    index_members: pd.DataFrame | None = None
    fundamentals: pd.DataFrame | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def symbols(self) -> list[str]:
        return sorted(self.bars["symbol"].unique().tolist())

    def date_range(self) -> tuple[Any, Any]:
        d = pd.to_datetime(self.bars["date"])
        return d.min(), d.max()


def describe_data_scope(
    bundle: MarketDataBundle,
    store: Any,
    *,
    sample_days: int = 5,
) -> dict[str, Any]:
    """说明「数据口径」的四个数量（缺陷修复 #12）。

    这四个数经常被混为一谈，导致「我明明生成了 30 只，怎么只有 12 只在跑」的困惑：

    - ``generated``：**生成/请求**的标的数（合成数据）；
    - ``with_bars``：**实际有行情**的标的数；
    - ``index_members``：**进入指数成分**的标的数（股票池候选上限）；
    - ``universe_avg``：**每日入池**的平均标的数（再经 ST/停牌/上市天数/流动性过滤）。
    """
    generated = int(bundle.meta.get("n_symbols", len(bundle.symbols())))
    with_bars = len(bundle.symbols())
    union: set[str] = set()
    index_code: str | None = None
    if bundle.index_members is not None and len(bundle.index_members):
        members_frame = bundle.index_members
        union = set(members_frame["symbol"].astype(str))
        if "index_code" in members_frame.columns:
            index_code = str(members_frame["index_code"].iloc[0])
    days = store.trading_days()
    step = max(len(days) // max(int(sample_days), 1), 1)
    sample = list(days[::step])[: max(int(sample_days), 1)]
    sizes = [len(store.universe(d)) for d in sample]
    member_counts = (
        [len(store.index_members(index_code, d)) for d in sample] if index_code is not None else []
    )
    return {
        "generated": generated,
        "with_bars": with_bars,
        "index_code": index_code,
        "index_members_union": len(union),
        "index_members_avg": (sum(member_counts) / len(member_counts)) if member_counts else 0.0,
        "index_members_sizes": member_counts,
        "universe_avg": (sum(sizes) / len(sizes)) if sizes else 0.0,
        "universe_min": min(sizes) if sizes else 0,
        "universe_max": max(sizes) if sizes else 0,
        "universe_sample_days": [str(d) for d in sample],
        "universe_sizes": sizes,
    }


def read_table(path: str | Path, *, columns: Sequence[str] | None = None) -> pd.DataFrame:
    """按扩展名读取 CSV / Parquet。"""
    p = Path(path)
    if not p.exists():
        raise DataError(f"数据文件不存在：{p}")
    suffix = p.suffix.lower()
    if suffix in (".parquet", ".pq"):
        try:
            return pd.read_parquet(p, columns=list(columns) if columns else None)
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise DataError("读取 Parquet 需要安装 pyarrow：pip install pyarrow") from exc
    if suffix in (".csv", ".txt"):
        return pd.read_csv(p, dtype={"symbol": str, "index_code": str})
    raise DataError(f"不支持的文件类型：{p.suffix}（支持 .csv/.parquet）")


@runtime_checkable
class BarDataLoader(Protocol):
    """数据源适配协议：接入真实数据源（Tushare/Wind/本地库）只需实现本协议。"""

    def load_bars(
        self, *, symbols: Sequence[str] | None = None, start: Any = None, end: Any = None
    ) -> pd.DataFrame: ...

    def load_index_members(self, index_code: str | None = None) -> pd.DataFrame: ...

    def load_fundamentals(self, *, symbols: Sequence[str] | None = None) -> pd.DataFrame: ...


@dataclass(slots=True)
class _FileLoaderBase:
    """文件型数据源基类：支持「单文件含 symbol 列」与「一标的一文件」两种布局。"""

    bars_path: str | Path
    index_members_path: str | Path | None = None
    fundamentals_path: str | Path | None = None
    glob: str = "*"
    symbol_from_filename: bool = True
    name: str = "file"

    # ------------------------------ 内部 ------------------------------ #
    def _iter_files(self) -> list[Path]:
        root = Path(self.bars_path)
        if root.is_file():
            return [root]
        if not root.exists():
            raise DataError(f"行情目录不存在：{root}")
        files = sorted(p for p in root.glob(self.glob) if p.is_file() and p.suffix.lower() in (".csv", ".parquet", ".txt", ".pq"))
        if not files:
            raise DataError(f"目录 {root} 下未找到匹配 {self.glob} 的数据文件")
        return files

    def _symbol_from_path(self, path: Path) -> str:
        return path.stem

    # ------------------------------ 协议实现 ------------------------------ #
    def load_bars(
        self, *, symbols: Sequence[str] | None = None, start: Any = None, end: Any = None
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        wanted = {str(s).upper() for s in symbols} if symbols else None
        for path in self._iter_files():
            df = read_table(path)
            if self.symbol_from_filename and "symbol" not in df.columns:
                df = df.assign(symbol=self._symbol_from_path(path))
            if "symbol" not in df.columns:
                raise SchemaError(f"数据文件缺少 symbol 列且无法从文件名推断：{path}")
            if wanted is not None:
                df = df[df["symbol"].astype(str).str.upper().isin(wanted)]
            if len(df):
                frames.append(df)
        if not frames:
            raise DataError("未读取到任何行情数据（请检查 symbols / 目录 / 文件内容）")
        bars = pd.concat(frames, ignore_index=True)
        if start is not None:
            bars = bars[pd.to_datetime(bars["date"]) >= pd.Timestamp(start)]
        if end is not None:
            bars = bars[pd.to_datetime(bars["date"]) <= pd.Timestamp(end)]
        logger.info("已载入 %s 条行情（%s 个标的）", len(bars), bars["symbol"].nunique())
        return bars

    def load_index_members(self, index_code: str | None = None) -> pd.DataFrame:
        if self.index_members_path is None:
            return pd.DataFrame()
        df = read_table(self.index_members_path)
        if index_code is not None and "index_code" in df.columns:
            df = df[df["index_code"].astype(str).str.upper() == str(index_code).upper()]
        return df

    def load_fundamentals(self, *, symbols: Sequence[str] | None = None) -> pd.DataFrame:
        if self.fundamentals_path is None:
            return pd.DataFrame()
        df = read_table(self.fundamentals_path)
        if symbols is not None and "symbol" in df.columns:
            wanted = {str(s).upper() for s in symbols}
            df = df[df["symbol"].astype(str).str.upper().isin(wanted)]
        return df


class CsvBarLoader(_FileLoaderBase):
    """CSV 数据源。"""

    def __init__(
        self,
        bars_path: str | Path,
        index_members_path: str | Path | None = None,
        fundamentals_path: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("glob", "*.csv")
        kwargs.setdefault("name", "csv")
        super().__init__(bars_path, index_members_path, fundamentals_path, **kwargs)


class ParquetBarLoader(_FileLoaderBase):
    """Parquet 数据源（需 pyarrow）。"""

    def __init__(
        self,
        bars_path: str | Path,
        index_members_path: str | Path | None = None,
        fundamentals_path: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("glob", "*.parquet")
        kwargs.setdefault("name", "parquet")
        super().__init__(bars_path, index_members_path, fundamentals_path, **kwargs)


def load_market_data(
    config: DataConfig,
    *,
    symbols: Sequence[str] | None = None,
    start: Any = None,
    end: Any = None,
    loader: BarDataLoader | None = None,
    **synthetic_kwargs: Any,
) -> MarketDataBundle:
    """按配置装载市场数据，返回归一化后的 :class:`MarketDataBundle`。

    ``provider: synthetic`` 用于测试与演示（确定性生成，可复现）。

    .. deprecated:: M4-7
       这是「进程内直接生成/读盘」的旧入口，只覆盖 synthetic/csv/parquet，
       且没有能力声明、溯源、缓存与降级披露。**新代码请用**
       :func:`aqs.data.registry.build_provider` +
       :func:`aqs.data.loader.ingest_from_provider`（M4-10），
       以便报告层拿到 `ProviderCapabilities` 与 `IngestReport`。
       本函数暂时保留（既有 demo/测试仍在使用），长期计划是内部改为委托
       ``build_provider``，届时两条路径必须逐字段一致（有一致性用例守护）。
    """
    if loader is None and config.provider == "synthetic":
        from .synthetic import generate_market_data

        return generate_market_data(start=start, end=end, symbols=symbols, **synthetic_kwargs)

    if loader is None:
        root = Path(config.root)
        indexes = root.parent / "index" / "index_members.csv"
        fundamentals = root.parent / "fundamental" / "fundamentals.csv"
        cls = CsvBarLoader if config.provider == "csv" else ParquetBarLoader
        loader = cls(
            root,
            index_members_path=indexes if indexes.exists() else None,
            fundamentals_path=fundamentals if fundamentals.exists() else None,
        )

    raw_bars = loader.load_bars(symbols=symbols, start=start, end=end)
    bars = normalize_bars(raw_bars)

    members = loader.load_index_members() if hasattr(loader, "load_index_members") else pd.DataFrame()
    members = normalize_index_members(members) if members is not None and len(members) else None

    fundamentals = loader.load_fundamentals(symbols=symbols) if hasattr(loader, "load_fundamentals") else pd.DataFrame()
    fundamentals = normalize_fundamentals(fundamentals) if fundamentals is not None and len(fundamentals) else None

    return MarketDataBundle(bars=bars, index_members=members, fundamentals=fundamentals, meta={"provider": config.provider})


# --------------------------------------------------------------------------- #
# provider → DataStore 适配层（M4-10）
# --------------------------------------------------------------------------- #
#: 步骤名固定（报告层与测试据此断言；顺序即执行顺序）
INGEST_STEPS: tuple[str, ...] = (
    "calendar",
    "index_members",
    "symbol_meta",
    "bars",
    "fundamentals",
    "industry",
    "normalize",
    "validate",
    "store",
)

#: `step_status` 的取值域
STEP_STATUSES: tuple[str, ...] = ("ok", "degraded", "failed", "skipped")

#: 取数层降级披露用的 finding 编码前缀（与 Q1~Q12 区分：这些不是数据质量缺陷，
#: 而是「取数时发生的降级/近似」）
INGEST_FINDING_CODE = "I1"


@dataclass(slots=True)
class IngestReport:
    """一次「从 provider 取数并构建 :class:`DataStore`」的完整结果（M4-10）。

    字段语义：

    - ``store``：可直接回测的 :class:`DataStore`。本适配层**不改**它的对外语义
      （PIT 访问、股票池、上市日策略都仍由 store 自己负责）；
    - ``quality``：provider 级质量报告（Q1~Q12）+ 取数层降级披露（编码 ``I1``）；
    - ``capabilities``：provider 声明它**理论上**具备的能力。**本次实际达成情况见
      `step_status`** —— ``capabilities.daily_bars is True`` 只表示「该源声称支持日线」，
      不表示这一次一定拿到了数据（可能抓取失败并走了降级路径）；
    - ``step_status``：9 个步骤各自的取值，见 :data:`STEP_STATUSES`。
      分界：``skipped`` **只表示用户显式没请求**（如 ``fundamentals=False``）；
      「源不支持该能力」属于 ``degraded``，不是 ``skipped``；
    - ``degradation_notes``：逐条写明**被触发的既有口径**（去重保序），
      而不是只写一句「降级了」；
    - ``manifest``：provider 的缓存清单。无缓存能力时为 ``[]``，并在
      ``degradation_notes`` 里说明 —— 避免调用方把空清单误读成「抓取失败」；
    - ``diagnostics``：``validate``（`validate_bars` 的原始报告）、``store_quality``、
      ``index_members_approximated`` 等供报告层直接消费的结构。

    **失败原子性**：失败**不影响已写入的缓存**；重跑时前面的步骤会走缓存命中，
    不会重复抓取（缓存由 provider 自己管理，本适配层不清理也不回滚缓存）。
    """

    store: DataStore
    quality: ProviderQualityReport
    provenance: dict[str, Provenance]
    capabilities: ProviderCapabilities
    step_status: dict[str, str]
    degradation_notes: tuple[str, ...]
    manifest: list[CacheMeta]
    diagnostics: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    @property
    def has_errors(self) -> bool:
        """是否存在**阻断级**问题：质量报告有 error，或校验步骤被判 failed。"""
        return (not self.quality.ok) or self.step_status.get("validate") == "failed"

    @property
    def overall_status(self) -> str:
        """整体状态：``failed`` > ``degraded`` > ``ok``。"""
        values = set(self.step_status.values())
        if "failed" in values:
            return "failed"
        if "degraded" in values:
            return "degraded"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        """供报告层/诊断落盘的摘要（不含 store 本体）。"""
        return {
            "provider": self.diagnostics.get("provider", ""),
            "overall_status": self.overall_status,
            "has_errors": self.has_errors,
            "step_status": dict(self.step_status),
            "degradation_notes": list(self.degradation_notes),
            "quality": self.quality.to_dict(),
            "manifest_rows": len(self.manifest),
            "provenance": {k: v.as_dict() for k, v in self.provenance.items()},
        }


def _fallback_notes(caps: ProviderCapabilities, config: DataConfig) -> list[str]:
    """D4 要求：逐条写明**被触发的既有口径**（不是笼统的「降级」）。"""
    limits = PriceLimitConfig()
    notes: list[str] = []
    if not caps.suspensions:
        notes.append("is_suspended 缺失 → 由 volume<=0 推断停牌（normalize_bars 的既有口径）")
    if not caps.price_limits:
        notes.append(
            "limit_up/limit_down 缺失 → 按板块规则推算"
            f"（主板 {limits.main_board_pct:.0%} / 创业板·科创板 {limits.gem_pct:.0%} / "
            f"北交所 {limits.bse_pct:.0%} / 主板 ST {limits.st_pct:.0%}）"
        )
    if not caps.st_flags:
        notes.append("is_st 缺失 → 默认 False（不假设任何标的为 ST）")
    if not caps.adjustment_factors:
        notes.append(
            f"复权因子缺失 → adj_factor 按 1.0 处理，{config.adjustment} 口径不可用（收益口径会失真）"
        )
    return notes


def _attach_static_columns(bars: pd.DataFrame, extra: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """把元信息/行业等**静态列**补进行情表（**不覆盖数据源自带值**，只补缺失）。

    「优先使用数据源取值，仅对缺失行补」是缺陷 #2 的口径；行业等列同样遵守，
    否则一个更权威的数据源自带值会被侧表覆盖。
    """
    attached: list[str] = []
    if extra is None or len(extra) == 0 or "symbol" not in extra.columns:
        return bars, attached
    out = bars
    side = extra.drop_duplicates(subset=["symbol"]).set_index("symbol")
    for column in (c for c in side.columns if c != "symbol"):
        values = out["symbol"].map(side[column])
        if column not in out.columns:
            out = out.assign(**{column: values})
            attached.append(column)
        else:
            current = out[column]
            missing = current.isna()
            if bool(missing.any()):
                out[column] = current.where(~missing, values)
                attached.append(column)
    return out, attached


def _ingest_optional(
    name: str,
    fetch: Any,
    *,
    declared: bool,
    failure_policy: str,
    steps: dict[str, str],
    notes: list[str],
    provenance: dict[str, Provenance],
) -> pd.DataFrame | None:
    """跑一个**可选**数据集（财务 / 行业）：失败一律**不中止**，只如实记录。

    四种状态的分界（用户 D3 决定）：

    - 源声明不支持 → ``degraded``；
    - 抓取失败且 ``failure_policy=fallback``（有降级路径）→ ``degraded``；
    - 抓取失败且 ``failure_policy=fail``（该数据集本次确实缺失）→ ``failed``；
    - 成功但为空 → ``degraded``（区间内确实没有该数据，不是错误）。

    ``skipped`` 不由本函数产生：那只表示**用户显式没请求**。
    """
    try:
        frame, prov = fetch()
    except DataError as exc:
        if not declared:
            steps[name] = "degraded"
            notes.append(f"{name}：数据源不支持该能力（capabilities 已声明为 False）：{exc}")
        elif failure_policy == "fail":
            steps[name] = "failed"
            notes.append(f"{name}：抓取失败且 failure_policy=fail → 本次无该数据：{exc}")
        else:
            steps[name] = "degraded"
            notes.append(f"{name}：抓取失败，已跳过（可选数据，不中止）：{exc}")
        return None
    provenance[name] = prov
    if not declared:
        steps[name] = "degraded"
        notes.append(f"{name}：数据源不支持该能力（capabilities 已声明为 False）")
        return None
    if len(frame) == 0:
        steps[name] = "degraded"
        notes.append(f"{name}：区间内无数据（不是错误，已如实记录）")
        return None
    steps[name] = "ok"
    for warning in prov.warnings:
        notes.append(f"{name}：{warning}")
    return frame


def ingest_from_provider(
    provider: Any,
    *,
    config: DataConfig,
    universe_config: UniverseConfig,
    index_codes: Sequence[str] = ("000300.SH",),
    start: Any,
    end: Any,
    symbols: Sequence[str] | None = None,
    fundamentals: bool = True,
    industry: bool = True,
) -> IngestReport:
    """从 :class:`~aqs.data.provider.DataProvider` 取数并构建 :class:`DataStore`。

    九步流程（`docs/11` §5.1）：日历 → 指数成分（并集→候选池）→ 元信息 →
    行情 → 财务 → 行业 → 归一 → 校验 → 构建 store（+ 读缓存清单）。

    失败路径（已与使用者确认）：

    - **第 3 步元信息**：源**不支持**上市日 → 不中止，进入「无 meta 模式」并由
      ``data.listing_date.policy`` 决定（``strict`` 报错 / ``proxy`` 代理口径）；
      源**声称支持但抓取失败** → 按 ``data.failure_policy``（``fallback`` 降级 / ``fail`` 中止）；
    - **第 5/6 步财务与行业**：可选数据，失败**一律不中止**，按 D3 记
      ``degraded``/``failed`` 并写进 ``degradation_notes``；
    - **第 7 步校验**：``quality.strict=True`` → 抛 :class:`DataQualityError`（附报告）；
      ``False`` → 记 ``degraded`` 并继续；
    - **第 2 步指数成分**：源不支持历史成分时 ``diagnostics["index_members_approximated"]=True``，
      并把披露写入 ``quality``；股票池按 ``universe.fallback_to_all`` 决定退化或报错。

    Args:
        symbols: 候选股票池。``None`` 或空序列表示「由指数成分推导；若无成分则不过滤
            （返回该源能提供的全部）」。注意 AKShare 逐标的抓取，必须有明确标的。

    **失败原子性**：失败不影响已写入的缓存；重跑时前面的步骤会走缓存命中，不会重复抓取。
    """
    cfg = as_config(config, DataConfig)
    uni = as_config(universe_config, UniverseConfig)
    caps = provider.capabilities()
    steps: dict[str, str] = {}
    notes: list[str] = []
    provenance: dict[str, Provenance] = {}
    diagnostics: dict[str, Any] = {"provider": provider.name}

    # ---------------- ① 交易日历 ----------------
    days, cal_prov = provider.fetch_trading_calendar(start, end)
    provenance["calendar"] = cal_prov
    if days:
        steps["calendar"] = "ok"
        calendar = TradingCalendar(days, name=f"{provider.name}_calendar")
    else:
        steps["calendar"] = "degraded"
        calendar = None
        notes.append("交易日历为空（provider 未提供）→ 由行情日期派生（calendar.source=derived）")

    # ---------------- ② 指数历史成分（并集 → 候选池） ----------------
    member_frames: list[pd.DataFrame] = []
    member_failed: list[str] = []
    for code in index_codes:
        try:
            frame, prov = provider.fetch_index_members(code, start, end)
        except DataError as exc:
            member_failed.append(f"{code}: {exc}")
            notes.append(f"指数 {code} 成分抓取失败：{exc}")
            continue
        provenance[f"index_members:{code}"] = prov
        if len(frame):
            member_frames.append(frame)
    members = (
        normalize_index_members(pd.concat(member_frames, ignore_index=True))
        if member_frames
        else None
    )
    if not index_codes:
        steps["index_members"] = "skipped"
    elif members is None:
        steps["index_members"] = "degraded"
    elif member_failed:
        steps["index_members"] = "degraded"
    else:
        steps["index_members"] = "ok"

    if index_codes and not caps.index_members:
        diagnostics["index_members_approximated"] = True
        notes.append(
            f"指数成分不具备历史语义（capabilities.index_members=False）：请求了 {list(index_codes)}，"
            "实际使用当前快照/累积快照 → 这些日期之前的成分不可得，存在幸存者偏差风险"
        )
    else:
        diagnostics["index_members_approximated"] = False

    if members is None and uni.mode == "index":
        if uni.fallback_to_all:
            notes.append("无可用指数成分 → 股票池退化为全市场（universe.fallback_to_all=True）")
        else:
            raise DataError(
                "universe.mode=index 需要历史指数成分，但本次未取到任何成分；"
                "universe.fallback_to_all=False 时不允许退化（避免幸存者偏差被静默掩盖）"
            )

    # ---------------- 候选标的 ----------------
    candidate: list[str] | None = list(symbols) if symbols else None
    if candidate is None:
        if members is not None and len(members):
            candidate = sorted(set(members["symbol"].astype(str)))
        else:
            notes.append("无候选标的可推导 → 不过滤，由 provider 返回其全部标的")

    # ---------------- ③ 元信息 ----------------
    meta: pd.DataFrame | None = None
    try:
        meta, meta_prov = provider.fetch_symbol_meta(candidate or [])
        provenance["symbol_meta"] = meta_prov
    except DataError as exc:
        if cfg.failure_policy == "fail":
            raise DataError(
                f"symbol_meta 抓取失败且 data.failure_policy=fail：{exc}；step_status={steps}"
            ) from exc
        steps["symbol_meta"] = "degraded"
        if not caps.listing_dates:
            notes.append(
                "数据源不支持上市日（capabilities.listing_dates=False）→ 进入无 meta 模式："
                "上市天数过滤将由 data.listing_date.policy 决定（strict 报错 / proxy 代理口径）"
            )
        else:
            notes.append(f"symbol_meta 抓取失败 → 进入无 meta 模式：{exc}")
    else:
        if not caps.listing_dates:
            steps["symbol_meta"] = "degraded"
            notes.append(
                "数据源不支持上市日（capabilities.listing_dates=False）→ 上市天数过滤由 "
                "data.listing_date.policy 决定（strict 报错 / proxy 代理口径）"
            )
        elif len(meta) == 0:
            steps["symbol_meta"] = "degraded"
            notes.append("元信息为空 → 进入无 meta 模式（上市日策略照常生效）")
        else:
            steps["symbol_meta"] = "ok"
            if meta_prov.warnings:
                steps["symbol_meta"] = "degraded"
                for warning in meta_prov.warnings:
                    notes.append(f"symbol_meta：{warning}")

    # ---------------- ④ 行情 ----------------
    try:
        bars, bars_prov = provider.fetch_bars(candidate or [], start, end, adjust=cfg.adjustment)
    except DataError as exc:
        steps["bars"] = "failed"
        raise DataError(f"行情取数失败（必需步骤，直接中止）：{exc}；step_status={steps}") from exc
    provenance["bars"] = bars_prov
    if len(bars) == 0:
        steps["bars"] = "failed"
        detail = "；".join(dict.fromkeys([*notes[-3:], *bars_prov.warnings])) or "provider 未给出原因"
        raise DataError(
            f"行情为空，无法构建 DataStore（provider={provider.name}，"
            f"symbols={len(candidate) if candidate else '全部'}）：{detail}"
        )
    steps["bars"] = "degraded" if bars_prov.warnings else "ok"
    for warning in bars_prov.warnings:
        notes.append(f"bars：{warning}")

    # ---------------- ⑤ 财务（可选） ----------------
    if not fundamentals:
        steps["fundamentals"] = "skipped"
        fund_raw = None
    else:
        fund_raw = _ingest_optional(
            "fundamentals",
            lambda: provider.fetch_fundamentals(candidate or [], start, end),
            declared=caps.fundamentals,
            failure_policy=cfg.failure_policy,
            steps=steps,
            notes=notes,
            provenance=provenance,
        )
    fund_norm = (
        normalize_fundamentals(fund_raw)
        if fund_raw is not None and len(fund_raw)
        else None
    )

    # ---------------- ⑥ 行业（可选） ----------------
    if not industry:
        steps["industry"] = "skipped"
        industry_raw = None
    else:
        industry_raw = _ingest_optional(
            "industry",
            lambda: provider.fetch_industry(candidate or []),
            declared=caps.industry,
            failure_policy=cfg.failure_policy,
            steps=steps,
            notes=notes,
            provenance=provenance,
        )

    # ---------------- ⑦ 归一 ----------------
    bars_norm, attached = _attach_static_columns(bars, meta if meta is not None else pd.DataFrame())
    if industry_raw is not None and len(industry_raw):
        bars_norm, extra = _attach_static_columns(bars_norm, industry_raw)
        attached.extend(c for c in extra if c not in attached)
    if attached:
        notes.append(f"以下静态列由侧表补齐（仅补缺失，不覆盖数据源取值）：{sorted(set(attached))}")
    bars_norm = normalize_bars(bars_norm, price_limit=PriceLimitConfig())
    steps["normalize"] = "ok"
    diagnostics["normalize"] = {
        "bars_rows": int(len(bars_norm)),
        "symbols": int(bars_norm["symbol"].nunique()),
        "attached_columns": sorted(set(attached)),
    }

    # ---------------- ⑧ 校验 ----------------
    validate_report: DataQualityReport = validate_bars(
        bars_norm, check_price_limits=cfg.quality.check_price_limits
    )
    diagnostics["validate"] = validate_report
    if validate_report.errors:
        if cfg.quality.strict:
            steps["validate"] = "failed"
            raise DataQualityError(
                f"数据校验未通过（quality.strict=True）：{validate_report.summary()}；"
                f"step_status={ {**steps, 'validate': 'failed'} }",
                report=validate_report,
            )
        steps["validate"] = "degraded"
        notes.append(
            f"数据校验发现 {len(validate_report.errors)} 个错误，quality.strict=False → 继续运行"
        )
    else:
        steps["validate"] = "ok"

    # ---------------- ⑨ 构建 store（store 内部会再校验一次并保留自己的报告） ----------------
    calendar_arg = calendar
    if calendar_arg is None:
        calendar_arg = TradingCalendar.from_bars(bars_norm, name=f"{provider.name}_derived")
    store = DataStore(
        bars_norm,
        calendar=calendar_arg,
        index_members=members,
        fundamentals=fund_norm,
        config=cfg,
        universe_config=uni,
        validate=True,
        name=f"ingest:{provider.name}",
    )
    steps["store"] = "ok"
    diagnostics["store_quality"] = store.quality

    # ---------------- 质量报告（Q1~Q12 + 取数层披露） ----------------
    quality = QualityChecker().run_all(
        bars=bars_norm,
        symbol_meta=meta if meta is not None and len(meta) else None,
        fundamentals=fund_norm,
        members=members,
        calendar=days or None,
    )
    # 取数层降级披露必须进**报告结构**（不只是 stdout/日志），报告层才能直接引用
    for note in dict.fromkeys(notes):
        quality.add(
            QualityFinding(code=INGEST_FINDING_CODE, level="warning", message=note, count=1)
        )

    # ---------------- ⑩ 缓存清单 ----------------
    manifest_fn = getattr(provider, "cache_manifest", None)
    if callable(manifest_fn):
        manifest = list(manifest_fn())
        if not manifest:
            notes.append("provider 启用了缓存但清单为空（本次可能全部命中缓存或尚未写入）")
    else:
        manifest = []
        notes.append("该 provider 未启用缓存，无 manifest")

    # ---------------- 汇总披露 ----------------
    notes.extend(
        degradation_notes(
            caps,
            need_index_members=bool(index_codes) and uni.mode == "index",
            need_fundamentals=bool(fundamentals),
            need_adjustment_factors=cfg.adjustment == "hfq",
            need_price_limits=cfg.quality.check_price_limits,
        )
    )
    notes.extend(_fallback_notes(caps, cfg))

    missing_steps = [name for name in INGEST_STEPS if name not in steps]
    if missing_steps:  # pragma: no cover - 内部一致性断言，绝不用默认值掩盖
        raise DataError(f"内部错误：以下步骤未设置状态 {missing_steps}（不得默认成 ok）")

    return IngestReport(
        store=store,
        quality=quality,
        provenance=provenance,
        capabilities=caps,
        step_status={name: steps[name] for name in INGEST_STEPS},
        degradation_notes=tuple(dict.fromkeys(notes)),
        manifest=manifest,
        diagnostics=diagnostics,
    )
