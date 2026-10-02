"""配置数据结构（强类型 + 严格校验）。

设计原则：
1. **未知键直接报错** —— 防止拼写错误导致参数静默失效（量化系统里最危险的一类 bug）。
2. 所有影响回测结果的参数都在这里，代码中不允许出现魔法数字。
3. 提供 :meth:`BaseConfig.with_overlay` / :meth:`CostConfig.scaled`，支持敏感性测试与成本加倍。
"""

from __future__ import annotations

import dataclasses
import types
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime as _datetime
from typing import Any, Mapping, Sequence, Union, get_args, get_origin, get_type_hints

from ..core.exceptions import ConfigError

__all__ = [
    "ProjectConfig",
    "CalendarConfig",
    "LiquidityConfig",
    "QualityConfig",
    "ListingDateConfig",
    "ExposureControlConfig",
    "RetryConfig",
    "RateLimitConfig",
    "CacheConfig",
    "UnitConversionConfig",
    "SyntheticDataConfig",
    "DataConfig",
    "UniverseConfig",
    "MatchingConfig",
    "PriceLimitConfig",
    "EngineConfig",
    "StampTaxRule",
    "StampTaxConfig",
    "SlippageConfig",
    "ImpactConfig",
    "CostConfig",
    "KellyConfig",
    "PortfolioConfig",
    "RiskRefConfig",
    "StrategyRefConfig",
    "ReportConfig",
    "BaseConfig",
    "RiskRuleConfig",
    "RiskAlertsConfig",
    "RiskConfig",
    "construct",
]


# --------------------------------------------------------------------------- #
# 通用反序列化
# --------------------------------------------------------------------------- #
def _is_optional(tp: Any) -> bool:
    origin = get_origin(tp)
    return origin in (Union, types.UnionType) and type(None) in get_args(tp)


def _unwrap_optional(tp: Any) -> Any:
    args = [a for a in get_args(tp) if a is not type(None)]
    return args[0] if len(args) == 1 else Union[tuple(args)]  # type: ignore[arg-type]


def _coerce(tp: Any, value: Any, path: str) -> Any:
    if tp is Any or tp is None:
        return value
    if _is_optional(tp):
        if value is None:
            return None
        return _coerce(_unwrap_optional(tp), value, path)

    origin = get_origin(tp)
    if origin in (list, Sequence) or tp in (list, Sequence):
        if value is None:
            return []
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise ConfigError("应为列表", path=path, value=value)
        args = get_args(tp)
        item_tp = args[0] if args else Any
        return [_coerce(item_tp, v, f"{path}[{i}]") for i, v in enumerate(value)]
    if origin is tuple:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ConfigError("应为元组/列表", path=path, value=value)
        args = get_args(tp)
        return tuple(_coerce(args[i] if i < len(args) else Any, v, f"{path}[{i}]") for i, v in enumerate(value))
    if origin is dict or tp is dict:
        if not isinstance(value, Mapping):
            raise ConfigError("应为字典", path=path, value=value)
        args = get_args(tp)
        vt = args[1] if len(args) == 2 else Any
        return {k: _coerce(vt, v, f"{path}.{k}") for k, v in value.items()}

    if dataclasses.is_dataclass(tp):
        return construct(tp, value, path=path)

    if tp is bool:
        if not isinstance(value, bool):
            raise ConfigError("应为布尔值（true/false）", path=path, value=value)
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError("应为整数", path=path, value=value)
        if isinstance(value, float) and not float(value).is_integer():
            raise ConfigError("应为整数（当前为小数）", path=path, value=value)
        return int(value)
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError("应为数值", path=path, value=value)
        return float(value)
    if tp is str:
        if not isinstance(value, str):
            raise ConfigError("应为字符串", path=path, value=value)
        return value
    if tp is _date:
        if isinstance(value, _date) and not isinstance(value, _datetime):
            return value
        if isinstance(value, _datetime):
            return value.date()
        if isinstance(value, str):
            try:
                return _date.fromisoformat(value[:10])
            except ValueError as exc:
                raise ConfigError(f"日期格式错误（应为 YYYY-MM-DD）：{value}", path=path) from exc
        raise ConfigError("应为日期", path=path, value=value)
    if tp is _datetime:
        if isinstance(value, _datetime):
            return value
        if isinstance(value, str):
            return _datetime.fromisoformat(value)
        raise ConfigError("应为时间戳", path=path, value=value)
    return value


def construct(cls: type, value: Any, *, path: str = "") -> Any:
    """按 dataclass 定义严格构造配置对象（未知键报错）。"""
    if isinstance(value, cls):
        return value
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ConfigError("配置段应为映射（key: value）", path=path or cls.__name__, value=value)

    hints = get_type_hints(cls)
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = sorted(set(value.keys()) - set(fields.keys()))
    if unknown:
        raise ConfigError(
            f"存在未知配置键 {unknown}；可用键：{sorted(fields.keys())}",
            path=path or cls.__name__,
        )
    kwargs: dict[str, Any] = {}
    for name, f in fields.items():
        sub_path = f"{path}.{name}" if path else name
        if name in value:
            kwargs[name] = _coerce(hints.get(name, Any), value[name], sub_path)
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:  # type: ignore[misc]
            raise ConfigError("缺少必填配置项", path=sub_path)
    return cls(**kwargs)


def as_config(value: Any, cls: type) -> Any:
    """把 ``None`` / ``Mapping`` / 已构造对象统一转换为配置对象。"""
    if value is None:
        return cls()
    if isinstance(value, cls):
        return value
    if isinstance(value, Mapping):
        return construct(cls, dict(value))
    raise ConfigError(f"应为 {cls.__name__} 或映射", value=type(value).__name__)


def _to_plain(obj: Any) -> Any:
    """dataclass → 普通 dict（日期转 ISO 字符串）。"""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Mapping):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    if isinstance(obj, (_date, _datetime)):
        return obj.isoformat()
    return obj


# --------------------------------------------------------------------------- #
# 配置段
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ProjectConfig:
    name: str = "aqs"
    seed: int = 20240101
    timezone: str = "Asia/Shanghai"


@dataclass(slots=True)
class CalendarConfig:
    source: str = "derived"          # derived | file
    file: str | None = None


@dataclass(slots=True)
class LiquidityConfig:
    window: int = 20
    min_amount: float = 50_000_000.0


@dataclass(slots=True)
class QualityConfig:
    strict: bool = True
    check_price_limits: bool = True


# --------------------------------------------------------------------------- #
# M4：数据源取数行为配置（缓存 / 限流 / 重试 / 单位换算）
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RetryConfig:
    """取数重试策略（M4）。

    ``retry_on`` 用**异常类名字符串**而非异常类：配置层保持纯数据，
    不 import 网络异常类型；具体类型在 ``data/ratelimit.py`` 侧解析。
    """

    max_attempts: int = 5
    backoff: float = 1.5
    jitter: bool = True
    retry_on: tuple[str, ...] = ("ConnectionError", "TimeoutError", "OSError")

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ConfigError("max_attempts 至少为 1", path="data.retry.max_attempts", value=self.max_attempts)
        if self.backoff < 1.0:
            raise ConfigError(
                "backoff 必须 >= 1.0（指数退避底数）", path="data.retry.backoff", value=self.backoff
            )


@dataclass(slots=True)
class RateLimitConfig:
    """令牌桶限流（M4）。``requests_per_minute = 0`` 表示不限流。"""

    enabled: bool = True
    requests_per_minute: int = 300
    burst: int = 10
    min_interval_ms: float = 0.0

    def __post_init__(self) -> None:
        if self.requests_per_minute < 0:
            raise ConfigError(
                "requests_per_minute 不能为负（0 = 不限流）",
                path="data.rate_limit.requests_per_minute",
                value=self.requests_per_minute,
            )
        if self.burst < 1:
            raise ConfigError("burst 至少为 1", path="data.rate_limit.burst", value=self.burst)
        if self.min_interval_ms < 0:
            raise ConfigError(
                "min_interval_ms 不能为负", path="data.rate_limit.min_interval_ms", value=self.min_interval_ms
            )


@dataclass(slots=True)
class CacheConfig:
    """本地缓存（M4）。

    ``version`` 是 **schema 版本**：字段或口径变更即 +1，旧版本缓存**不读也不删**
    （由人工清理），避免新旧 schema 混用产生难以察觉的口径错误。
    """

    enabled: bool = True
    root: str = "data/cache"
    version: int = 1
    fmt: str = "parquet"           # parquet | csv（pyarrow 不可用时自动退化并 warning）
    ttl_hours: float = 24.0
    refresh: bool = False

    def __post_init__(self) -> None:
        if self.fmt not in ("parquet", "csv"):
            raise ConfigError("fmt 只能是 parquet/csv", path="data.cache.fmt", value=self.fmt)
        if self.version < 1:
            raise ConfigError("version 至少为 1", path="data.cache.version", value=self.version)
        if self.ttl_hours < 0:
            raise ConfigError("ttl_hours 不能为负", path="data.cache.ttl_hours", value=self.ttl_hours)


@dataclass(slots=True)
class UnitConversionConfig:
    """原始数据的成交量/成交额单位换算（M4，专治 Q3「手 → 股」陷阱）。

    真实数据源里 ``volume`` 常见「手」（1 手 = 100 股），``amount`` 偶见「千元」。
    若误把「手」当「股」，成交额、参与率上限、冲击成本会整体错 100 倍 ——
    而且**不会报错**，只会让回测结果悄悄失真。因此把换算系数显式配置化。
    """

    volume_to_shares: float = 1.0    # 股数 = 原始 volume × 该系数（日线为「手」时填 100）
    amount_to_yuan: float = 1.0      # 元   = 原始 amount × 该系数（千元时填 1000）

    def __post_init__(self) -> None:
        if self.volume_to_shares <= 0:
            raise ConfigError(
                "volume_to_shares 必须为正", path="data.unit_conversion.volume_to_shares",
                value=self.volume_to_shares,
            )
        if self.amount_to_yuan <= 0:
            raise ConfigError(
                "amount_to_yuan 必须为正", path="data.unit_conversion.amount_to_yuan",
                value=self.amount_to_yuan,
            )


@dataclass(slots=True)
class SyntheticDataConfig:
    """合成数据生成参数（M4）。

    合成行情用于测试与演示，其规模与种子**同样影响回测结果**，
    因此必须配置化（不得写成代码里的魔法数字）；CLI 参数会覆盖这里的值。
    """

    n_symbols: int = 30
    seed: int = 20240101
    index_size: int = 15
    index_code: str = "000300.SH"

    def __post_init__(self) -> None:
        if self.n_symbols < 1:
            raise ConfigError(
                "n_symbols 至少为 1", path="data.synthetic.n_symbols", value=self.n_symbols
            )
        if self.index_size < 1:
            raise ConfigError(
                "index_size 至少为 1", path="data.synthetic.index_size", value=self.index_size
            )


#: 内置数据源名单（**唯一来源**；注册表 `data/registry.py` 据此做 import 期自检）。
#: 注意：配置层是**声明式**的 —— 这里只校验「名字在内置名单内」，
#: 「这个名字是否真的有实现」由运行时的 `registry.build_provider` 查注册表判定。
KNOWN_PROVIDERS: tuple[str, ...] = ("synthetic", "csv", "parquet", "akshare")


@dataclass(slots=True)
class DataConfig:
    provider: str = "synthetic"      # 取值见 KNOWN_PROVIDERS
    root: str = "data/raw"
    adjustment: str = "hfq"          # hfq（后复权）| none
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    min_list_days: int = 60
    liquidity: LiquidityConfig = field(default_factory=LiquidityConfig)
    exclude_st: bool = True
    exclude_suspended: bool = True
    quality: QualityConfig = field(default_factory=QualityConfig)
    listing_date: "ListingDateConfig" = field(default_factory=lambda: ListingDateConfig())
    # ---- M4：取数行为（平铺，四 provider 共用；差异只在字段映射）----
    cache: CacheConfig = field(default_factory=CacheConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    unit_conversion: UnitConversionConfig = field(default_factory=UnitConversionConfig)
    failure_policy: str = "fallback"   # fallback（用本地缓存快照）| fail（抛 DataError）
    max_missing_ratio: float = 0.01    # 单标的失败比例阈值，超过升级为 error
    synthetic: SyntheticDataConfig = field(default_factory=SyntheticDataConfig)

    def __post_init__(self) -> None:
        if self.provider not in KNOWN_PROVIDERS:
            raise ConfigError(
                f"provider 只能是 {list(KNOWN_PROVIDERS)}（内置名单）",
                path="data.provider",
                value=self.provider,
            )
        if self.adjustment not in ("hfq", "none"):
            raise ConfigError("adjustment 只能是 hfq/none", path="data.adjustment", value=self.adjustment)
        if self.min_list_days < 0:
            raise ConfigError("min_list_days 不能为负", path="data.min_list_days", value=self.min_list_days)
        if self.failure_policy not in ("fallback", "fail"):
            raise ConfigError(
                "failure_policy 只能是 fallback/fail", path="data.failure_policy", value=self.failure_policy
            )
        if not (0.0 <= self.max_missing_ratio <= 1.0):
            raise ConfigError(
                "max_missing_ratio 应在 [0, 1]", path="data.max_missing_ratio", value=self.max_missing_ratio
            )


@dataclass(slots=True)
class UniverseConfig:
    mode: str = "index"              # index | all | file
    index_code: str = "000300.SH"
    history_file: str | None = None
    fallback_to_all: bool = True

    def __post_init__(self) -> None:
        if self.mode not in ("index", "all", "file"):
            raise ConfigError("mode 只能是 index/all/file", path="universe.mode", value=self.mode)


@dataclass(slots=True)
class MatchingConfig:
    max_participation: float = 0.10
    allow_partial_fill: bool = True
    limit_up_fill_prob: float = 0.0
    slippage_price_adjust: bool = True

    def __post_init__(self) -> None:
        if not 0.0 < self.max_participation <= 1.0:
            raise ConfigError("max_participation 必须在 (0, 1]", path="engine.matching.max_participation", value=self.max_participation)
        if not 0.0 <= self.limit_up_fill_prob <= 1.0:
            raise ConfigError("limit_up_fill_prob 必须在 [0, 1]", path="engine.matching.limit_up_fill_prob", value=self.limit_up_fill_prob)


@dataclass(slots=True)
class PriceLimitConfig:
    enabled: bool = True
    use_provided_limits: bool = True
    main_board_pct: float = 0.10
    gem_pct: float = 0.20            # 创业板/科创板
    bse_pct: float = 0.30            # 北交所
    st_pct: float = 0.05             # 主板 ST（创业板/科创板 ST 仍为 20%）
    round_to_cent: bool = True
    first_day_no_limit: bool = True  # 上市首日不设涨跌幅（注册制/简化处理）

    def pct_for(self, board: object, is_st: bool) -> float:
        """返回该板块 + ST 状态对应的涨跌幅比例。

        规则（截至当前交易所规定）：
        - 主板：普通 10%，ST 5%；
        - 创业板/科创板：20%（ST 亦为 20%）；
        - 北交所：30%。
        """
        from ..core.enums import Board as _Board

        if board == _Board.GEM:
            return self.gem_pct
        if board == _Board.STAR:
            return self.gem_pct
        if board == _Board.BSE:
            return self.bse_pct
        return self.st_pct if is_st else self.main_board_pct


@dataclass(slots=True)
class ListingDateConfig:
    """上市日缺失的处理策略（缺陷修复 #8）。

    - ``strict``（默认）：任何标的缺少 ``list_date`` → 数据质量错误（拒绝构建 ``DataStore``）；
    - ``proxy``：允许降级为「该标的第几根 K 线」，但必须**显式标记**
      （``DataStore.listed_days_source`` / 质量报告 / 回测诊断），不得静默。
    """

    policy: str = "strict"
    proxy_warn_once: bool = True

    def __post_init__(self) -> None:
        if self.policy not in ("strict", "proxy"):
            raise ConfigError(
                "listing_date.policy 只能是 strict/proxy", path="data.listing_date.policy", value=self.policy
            )


@dataclass(slots=True)
class ExposureControlConfig:
    """RMS 减仓/强平的可达性与偏差控制（缺陷修复 #4）。"""

    unmet_warning_days: int = 5        # 连续未达标的交易日数超过该值 → 预警
    tolerance: float = 0.01            # 敞口容差（目标 ±1% 内视为达标）


@dataclass(slots=True)
class EngineConfig:
    start: _date | None = None
    end: _date | None = None
    initial_cash: float = 1_000_000.0
    execution_price: str = "open"    # open | vwap | close
    t_plus_one: bool = True
    lot_size: int = 100
    max_defer_days: int = 5
    risk_audit_log: str | None = None   # None=跟随 risk.yaml；""/"none"=仅内存；其他=路径
    matching: MatchingConfig = field(default_factory=MatchingConfig)
    price_limit: PriceLimitConfig = field(default_factory=PriceLimitConfig)
    exposure_control: ExposureControlConfig = field(default_factory=ExposureControlConfig)
    benchmark: list[str] = field(default_factory=lambda: ["000300.SH", "000905.SH", "000852.SH"])

    def __post_init__(self) -> None:
        if self.execution_price not in ("open", "vwap", "close"):
            raise ConfigError("execution_price 只能是 open/vwap/close", path="engine.execution_price", value=self.execution_price)
        if self.lot_size <= 0:
            raise ConfigError("lot_size 必须为正", path="engine.lot_size", value=self.lot_size)
        if self.initial_cash <= 0:
            raise ConfigError("initial_cash 必须为正", path="engine.initial_cash", value=self.initial_cash)
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ConfigError("start 不能晚于 end", path="engine.start", value=str(self.start))


@dataclass(slots=True)
class StampTaxRule:
    effective_from: _date
    rate: float


@dataclass(slots=True)
class StampTaxConfig:
    """印花税按生效日期分段（仅卖方单边征收）。"""

    schedule: list[StampTaxRule] = field(
        default_factory=lambda: [
            StampTaxRule(effective_from=_date(1990, 1, 1), rate=0.001),
            StampTaxRule(effective_from=_date(2023, 8, 28), rate=0.0005),
        ]
    )

    def rate_on(self, day: _date) -> float:
        """返回某日生效的印花税率。"""
        rules = sorted(self.schedule, key=lambda r: r.effective_from)
        rate = rules[0].rate if rules else 0.0
        for rule in rules:
            if day >= rule.effective_from:
                rate = rule.rate
            else:
                break
        return rate


@dataclass(slots=True)
class SlippageConfig:
    fixed_bps: float = 0.0
    prop_bps: float = 10.0           # 0.1% = 10 bps

    @property
    def total_bps(self) -> float:
        return self.fixed_bps + self.prop_bps

    @property
    def rate(self) -> float:
        return self.total_bps / 10_000.0


@dataclass(slots=True)
class ImpactConfig:
    enabled: bool = True
    eta: float = 0.1
    theta: float = 0.5
    advisory_window: int = 20


@dataclass(slots=True)
class CostConfig:
    fixed_fee_per_order: float = 0.0
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    transfer_fee_rate: float = 0.00001
    stamp_tax: StampTaxConfig = field(default_factory=StampTaxConfig)
    slippage: SlippageConfig = field(default_factory=SlippageConfig)
    impact: ImpactConfig = field(default_factory=ImpactConfig)
    scale: float = 1.0                # 全局成本倍数（2.0 = 成本加倍）

    def scaled(self, k: float) -> "CostConfig":
        """返回成本整体放大 k 倍的新配置（用于成本敏感性测试）。

        实现方式：只放大全局倍数 ``scale``；所有成本科目（含最低佣金）在
        :class:`aqs.engine.cost.CostModel` 计算末尾统一乘 ``scale``，
        避免「既放大费率又放大倍数」的重复计费。
        """
        if k <= 0:
            raise ConfigError("成本倍数必须为正", path="costs.scale", value=k)
        return dataclasses.replace(self, scale=self.scale * k)


@dataclass(slots=True)
class KellyConfig:
    """凯利仓位控制（第一阶段：简化版 + 半凯利）。"""

    enabled: bool = False
    mode: str = "binary"              # binary（胜率/赔率）| continuous（μ/σ²）
    fraction: float = 0.5             # 半凯利系数
    cap: float = 1.0                  # 总敞口上限（占总资产比例）
    min_trades: int = 20              # 启用凯利所需的最小平仓样本数
    initial_exposure: float = 1.0     # 样本不足时的敞口

    def __post_init__(self) -> None:
        if self.mode not in ("binary", "continuous"):
            raise ConfigError("kelly.mode 只能是 binary/continuous", path="portfolio.kelly.mode", value=self.mode)
        if not 0.0 < self.fraction <= 1.0:
            raise ConfigError("kelly.fraction 必须在 (0, 1]", path="portfolio.kelly.fraction", value=self.fraction)
        if not 0.0 < self.cap <= 1.0:
            raise ConfigError("kelly.cap 必须在 (0, 1]", path="portfolio.kelly.cap", value=self.cap)


@dataclass(slots=True)
class PortfolioConfig:
    weighting: str = "equal"          # equal | score_prop（凯利控制总敞口，见 kelly）
    max_positions: int = 10
    max_weight_per_symbol: float = 0.10
    cash_buffer: float = 0.02
    allow_reentry: bool = True        # 卖出后是否允许再次买入同一标的
    rebalance: bool = False           # True=按目标权重调仓；False=仅信号驱动卖出
    kelly: KellyConfig = field(default_factory=KellyConfig)

    def __post_init__(self) -> None:
        if self.weighting not in ("equal", "score_prop"):
            raise ConfigError(
                "weighting 只能是 equal/score_prop", path="portfolio.weighting", value=self.weighting
            )
        if self.max_positions <= 0:
            raise ConfigError("max_positions 必须为正", path="portfolio.max_positions", value=self.max_positions)
        if not 0.0 < self.max_weight_per_symbol <= 1.0:
            raise ConfigError("max_weight_per_symbol 必须在 (0, 1]", path="portfolio.max_weight_per_symbol", value=self.max_weight_per_symbol)
        if not 0.0 <= self.cash_buffer < 1.0:
            raise ConfigError("cash_buffer 必须在 [0, 1)", path="portfolio.cash_buffer", value=self.cash_buffer)


@dataclass(slots=True)
class RiskRefConfig:
    enabled: bool = True
    config_file: str | None = "configs/risk.yaml"


@dataclass(slots=True)
class StrategyRefConfig:
    name: str = "ma_cross"
    config_file: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ReportConfig:
    output_dir: str = "reports"
    save_trades: bool = True
    save_positions: bool = True
    save_equity_curve: bool = True
    benchmark: list[str] = field(default_factory=lambda: ["000300.SH", "000905.SH", "000852.SH"])


@dataclass(slots=True)
class BaseConfig:
    """系统主配置。"""

    project: ProjectConfig = field(default_factory=ProjectConfig)
    data: DataConfig = field(default_factory=DataConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    risk: RiskRefConfig = field(default_factory=RiskRefConfig)
    strategy: StrategyRefConfig = field(default_factory=StrategyRefConfig)
    report: ReportConfig = field(default_factory=ReportConfig)

    # ------------------------------ 工具 ------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return _to_plain(self)

    def with_overlay(self, overlay: Mapping[str, Any] | None) -> "BaseConfig":
        """返回叠加覆盖后的**新配置**（不修改原对象）。"""
        if not overlay:
            return self
        merged = _deep_merge(self.to_dict(), dict(overlay))
        return construct(BaseConfig, merged)

    def with_cost_scale(self, k: float) -> "BaseConfig":
        """成本整体缩放（敏感性测试）；``k`` 为绝对倍数。"""
        return dataclasses.replace(self, costs=dataclasses.replace(self.costs, scale=float(k)))


# --------------------------------------------------------------------------- #
# RMS 配置（configs/risk.yaml）
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RiskRuleConfig:
    name: str
    priority: int = 100
    action: str = "reject"            # allow | reject | reduce | pause | force_close
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.action not in ("allow", "reject", "reduce", "pause", "force_close"):
            raise ConfigError(
                "action 只能是 allow/reject/reduce/pause/force_close",
                path=f"risk.rules[{self.name}].action",
                value=self.action,
            )


@dataclass(slots=True)
class RiskAlertsConfig:
    enabled: bool = True
    channels: list[str] = field(default_factory=lambda: ["log"])


@dataclass(slots=True)
class RiskConfig:
    enabled: bool = True
    mode: str = "enforce"             # enforce | observe
    audit_log: str | None = None
    hot_reload: bool = True
    rules: list[RiskRuleConfig] = field(default_factory=list)
    alerts: RiskAlertsConfig = field(default_factory=RiskAlertsConfig)

    def __post_init__(self) -> None:
        if self.mode not in ("enforce", "observe"):
            raise ConfigError("mode 只能是 enforce/observe", path="risk.mode", value=self.mode)
        self.rules.sort(key=lambda r: r.priority)
        names = [r.name for r in self.rules]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ConfigError(f"风控规则名重复：{sorted(dup)}", path="risk.rules")

    def rule(self, name: str) -> RiskRuleConfig | None:
        for r in self.rules:
            if r.name == name:
                return r
        return None


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """递归合并字典；``override`` 中的 ``None`` 表示显式置空。"""
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], Mapping) and isinstance(value, Mapping):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


__all__.append("_deep_merge")
__all__.append("as_config")
