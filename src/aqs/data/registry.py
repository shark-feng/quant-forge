"""数据源注册表与配置驱动构建（M4）。

五条设计约束：

1. **取数入口只有一个派发点**：:func:`build_provider` 按名字查注册表，
   未注册即抛 :class:`ConfigError`，**绝不静默回退到 synthetic** ——
   静默回退意味着报告里的数字来自另一个数据源却看不出来，比直接报错危险得多。
2. **配置层声明式、运行层构造式**（A4 决议）：`DataConfig` 只校验
   ``provider in KNOWN_PROVIDERS``（内置白名单，纯声明）；
   「这个名字是否真的有实现」由 :func:`build_provider` 在运行时查注册表判定。
   这样 M4 分步交付期间（akshare 未注册 → 已注册）配置语义不会漂移。
3. **构建期协议闸门**：工厂返回对象必须满足 :class:`DataProvider`
   （``runtime_checkable``），否则抛 :class:`DataError` 并列出缺失属性 ——
   把「方法名拼错」挡在第一次取数之前，而不是等到某个报告数字算出来才发现。
4. **内置 provider 不得缺席**：import 期自检
   ``KNOWN_PROVIDERS - 已注册 - PENDING_PROVIDERS``，非空即拒绝导入
   （防「M4-8 忘了注册 akshare」这类漏接）。尚未接入的实现必须写进
   :data:`PENDING_PROVIDERS` 这个**显式**状态，而不是让检查失声。
5. **路径解析复用** ``config.loader.resolve_path``：相对路径一律以项目根为基准，
   避免「换个工作目录就读到另一个 data/raw」这种不报错的失真。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..config.loader import resolve_path
from ..config.schema import KNOWN_PROVIDERS, DataConfig, as_config
from ..core.exceptions import ConfigError, DataError
from .akshare_provider import AKShareProvider
from .cache import DataCache
from .file_provider import CsvProvider, ParquetProvider
from .provider import DataProvider, ProviderCapabilities
from .ratelimit import RateLimiter
from .synthetic_provider import SyntheticProvider

__all__ = [
    "PROVIDER_REGISTRY",
    "PENDING_PROVIDERS",
    "ProviderContext",
    "ProviderFactory",
    "register_provider",
    "available_providers",
    "get_provider_factory",
    "build_provider",
    "provider_capabilities",
]

#: 分步交付期的**显式**状态：已列入 KNOWN_PROVIDERS 但实现尚未接入。
#: M4-8 已接入 akshare，故为空；自检随之全面生效（漏注册任何内置源都会在 import 期报错）。
PENDING_PROVIDERS: tuple[str, ...] = ()

#: `DataProvider` 协议要求的全部属性名（方法 + 数据成员），
#: 由协议自身派生而非手抄，避免与 `provider.py` 漂移。
_PROTOCOL_ATTRS: tuple[str, ...] = tuple(
    sorted(
        {name for name in dir(DataProvider) if not name.startswith("_")}
        | set(getattr(DataProvider, "__annotations__", {}))
    )
)


@dataclass(frozen=True, slots=True)
class ProviderContext:
    """构建一个 provider 所需的全部外部依赖（显式传递，不用全局状态）。

    ``cache`` / ``limiter`` / ``client`` 为 ``None`` 时，由 provider 自己按
    ``config.cache`` / ``config.rate_limit`` 构建默认实例 —— 注册表保持「只传递、不决策」。
    """

    name: str
    config: DataConfig
    root: Path
    cache: DataCache | None = None
    limiter: RateLimiter | None = None
    client: Any | None = None
    index_members_path: Path | None = None
    fundamentals_path: Path | None = None


#: 工厂签名：上下文 → provider 实例
ProviderFactory = Callable[[ProviderContext], DataProvider]

#: provider 名 → 工厂
PROVIDER_REGISTRY: dict[str, ProviderFactory] = {}


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
def register_provider(name: str, factory: ProviderFactory, *, overwrite: bool = False) -> None:
    """注册 provider 工厂。

    - 名字必须是非空、无首尾空白的字符串（``" csv "`` 这类名字永远匹配不到配置，故直接拒绝）；
    - 重名默认**报错**：静默替换掉内置实现属于「不报错但行为变了」；
    - ``overwrite=True`` 时才允许覆盖（扩展/测试用）。
    """
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("provider 名必须是非空字符串", value=name)
    if name != name.strip():
        raise ConfigError(f"provider 名不应包含首尾空白：{name!r}", path="data.provider")
    if not callable(factory):
        raise ConfigError(
            f"provider {name} 的工厂必须可调用",
            path="data.provider",
            value=type(factory).__name__,
        )
    if name in PROVIDER_REGISTRY and not overwrite:
        raise ConfigError(
            f"provider 已注册：{name}（如需替换内置实现，请显式 overwrite=True）",
            path="data.provider",
        )
    PROVIDER_REGISTRY[name] = factory


def available_providers() -> list[str]:
    """已注册的 provider 名（排序，便于断言与展示）。"""
    return sorted(PROVIDER_REGISTRY)


def get_provider_factory(name: str) -> ProviderFactory:
    """按名字取工厂；未注册即 :class:`ConfigError`（**不做任何回退**）。"""
    try:
        return PROVIDER_REGISTRY[name]
    except KeyError as exc:
        raise ConfigError(
            f"未知数据源 provider：{name}；可用：{available_providers()}",
            path="data.provider",
            value=name,
        ) from exc


def _unregistered_builtins(
    registry: Mapping[str, Any], *, pending: tuple[str, ...] = ()
) -> list[str]:
    """内置名单里「既没注册、也不在待接入清单」的名字（纯函数，便于正反例测试）。"""
    return sorted(set(KNOWN_PROVIDERS) - set(registry) - set(pending))


def _require_protocol(obj: Any, name: str) -> DataProvider:
    """构建期协议闸门：不满足 `DataProvider` 就抛 `DataError`（含缺失属性清单）。"""
    if isinstance(obj, DataProvider):
        return obj
    missing = [attr for attr in _PROTOCOL_ATTRS if not hasattr(obj, attr)]
    raise DataError(
        f"provider {name} 的工厂返回 {type(obj).__name__}，不满足 DataProvider 协议；"
        f"缺失属性：{missing}"
    )


# --------------------------------------------------------------------------- #
# 构建
# --------------------------------------------------------------------------- #
def build_provider(
    config: DataConfig | Mapping[str, Any] | None = None,
    *,
    provider: str | None = None,
    root: str | Path | None = None,
    cache: DataCache | None = None,
    limiter: RateLimiter | None = None,
    client: Any | None = None,
    index_members_path: str | Path | None = None,
    fundamentals_path: str | Path | None = None,
) -> DataProvider:
    """按配置/名字构建 provider。

    Args:
        config: 数据层配置（``None`` 用默认值）。
        provider: 显式覆盖 ``config.provider``；**代码即白名单**，
            但仍必须在注册表内（未注册照样抛 ``ConfigError``）。
        root: 覆盖 ``config.root``；相对路径按项目根解析。
        cache / limiter / client: 注入给需要它们的 provider（如 akshare）；
            传 ``None`` 表示由 provider 自建。
        index_members_path / fundamentals_path: 文件型 provider 的附加数据文件。

    Raises:
        ConfigError: provider 名未注册。
        DataError: 工厂抛错，或返回对象不满足 ``DataProvider`` 协议。
    """
    cfg = as_config(config, DataConfig) if config is not None else DataConfig()
    name = provider or cfg.provider
    factory = get_provider_factory(name)

    resolved_root = resolve_path(root if root is not None else cfg.root)
    assert resolved_root is not None  # 输入恒非 None，故解析结果恒非 None
    ctx = ProviderContext(
        name=name,
        config=cfg,
        root=resolved_root,
        cache=cache,
        limiter=limiter,
        client=client,
        index_members_path=resolve_path(index_members_path),
        fundamentals_path=resolve_path(fundamentals_path),
    )
    try:
        obj = factory(ctx)
    except DataError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一为数据层错误，保留原始原因
        raise DataError(f"构建 provider {name} 失败：{exc}") from exc
    return _require_protocol(obj, name)


def provider_capabilities(
    name: str,
    *,
    config: DataConfig | Mapping[str, Any] | None = None,
    strict: bool = True,
) -> ProviderCapabilities:
    """查一个 provider 的能力声明（不取数、不联网；文件型会抽样读列）。

    ``strict`` 的含义是**「用于回测的可用性闸门」**，不是「探测过程是否报错」：

    - ``strict=True``：构建失败、或 **必需能力**（``BACKTEST_REQUIRED``：
      ``daily_bars`` / ``listing_dates``）缺失 → 抛 :class:`DataError`。
      注意「探测失败」在文件型 provider 里表现为「行情抽样失败 → ``daily_bars=False``」
      （见 `file_provider` 的设计：能力探测不抛异常，只在 ``notes`` 里说明），
      因此闸门必须落在**能力**上，而不是落在「有没有异常」上 ——
      把探测失败伪装成「天生缺这些能力」会让上层误入降级路径而不自知。
    - ``strict=False``：始终返回声明（含探测失败时的全 ``False`` **下界**），
      失败原因写在 ``notes`` 里，供能力矩阵报告对「当前不可用」的 provider 也出一行。

    未注册的名字始终抛 :class:`ConfigError`（那是配置错误，不是探测失败）。
    """
    cfg = as_config(config, DataConfig) if config is not None else DataConfig()
    factory = get_provider_factory(name)  # 未知名：ConfigError 直接向上抛

    def _floor(reason: str) -> ProviderCapabilities:
        return ProviderCapabilities(
            default_adjust=cfg.adjustment,
            notes=(f"能力探测失败，按最低估计（全部为 False）：{reason}",),
        )

    try:
        provider = _require_protocol(factory(_context(cfg, name)), name)
        caps = provider.capabilities()
        if not isinstance(caps, ProviderCapabilities):
            raise DataError(
                f"provider {name} 的 capabilities() 返回 {type(caps).__name__}，"
                "应为 ProviderCapabilities"
            )
    except Exception as exc:  # noqa: BLE001 - 探测失败的处理方式由 strict 决定
        if strict:
            raise DataError(f"无法探测 provider {name} 的能力：{exc}") from exc
        return _floor(str(exc))

    if strict:
        missing = caps.missing_required()
        if missing:
            raise DataError(
                f"provider {name} 缺少回测必需能力 {list(missing)}"
                f"（strict 是可用性闸门；只看能力声明请用 strict=False）；"
                f"notes：{list(caps.notes)}"
            )
    return caps


# --------------------------------------------------------------------------- #
# 内置工厂
# --------------------------------------------------------------------------- #
def _context(cfg: DataConfig, name: str) -> ProviderContext:
    """只带配置的上下文（供不注入依赖的调用方，如 `provider_capabilities`）。"""
    resolved_root = resolve_path(cfg.root)
    assert resolved_root is not None
    return ProviderContext(name=name, config=cfg, root=resolved_root)


def _make_synthetic(ctx: ProviderContext) -> DataProvider:
    return SyntheticProvider(ctx.config)


def _make_csv(ctx: ProviderContext) -> DataProvider:
    return CsvProvider(
        ctx.root,
        index_members_path=ctx.index_members_path,
        fundamentals_path=ctx.fundamentals_path,
        config=ctx.config,
    )


def _make_parquet(ctx: ProviderContext) -> DataProvider:
    return ParquetProvider(
        ctx.root,
        index_members_path=ctx.index_members_path,
        fundamentals_path=ctx.fundamentals_path,
        config=ctx.config,
    )


def _make_akshare(ctx: ProviderContext) -> DataProvider:
    """AKShare：``client`` / ``cache`` / ``limiter`` 由上下文注入（生产为 None → provider 自建）。

    注意 ``client=None`` 时 provider 会**惰性** import akshare（未安装则报 DataError，
    不静默换源）—— 故这里**不能**替它构造客户端，否则「构建即联网」。
    """
    return AKShareProvider(
        ctx.config, client=ctx.client, cache=ctx.cache, limiter=ctx.limiter
    )


for _name, _factory in (
    ("synthetic", _make_synthetic),
    ("csv", _make_csv),
    ("parquet", _make_parquet),
    ("akshare", _make_akshare),
):
    register_provider(_name, _factory)


# --------------------------------------------------------------------------- #
# import 期自检：内置名单里不能有「既没注册、也不在待接入清单」的实现
# --------------------------------------------------------------------------- #
_missing_builtins = _unregistered_builtins(PROVIDER_REGISTRY, pending=PENDING_PROVIDERS)
if _missing_builtins:
    raise ConfigError(
        f"以下内置 provider 未注册且未列入 PENDING_PROVIDERS：{_missing_builtins}；"
        f"已注册：{available_providers()}",
        path="data.provider",
    )
