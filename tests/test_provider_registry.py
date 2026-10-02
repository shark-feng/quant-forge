"""M4-7：数据源注册表与配置驱动构建。

对应 `docs/18_m4_dataprovider.md` §4.5 与 A1~A5 决议：

- A1：未知名抛 ``ConfigError``（不静默回退到 synthetic）；
- A2：工厂签名为 ``ProviderContext -> DataProvider``；
- A3：``provider_capabilities`` 的「查不到能力 ≠ 没有能力」（strict/degraded 两态）；
- A4：配置层只认内置白名单，**不查注册表**；运行层 ``build_provider`` 才查；
- A5：导出完整性守护放在 `test_packaging.py`（同名守护集中一处）。

本模块全部用 ``test_*`` 函数（不用 ``setUp``）：零依赖运行器只调用测试函数本身，
不调用 ``setUp`` —— 依赖 ``setUp`` 会让「两套运行方式结果不一致」。
"""

from __future__ import annotations

from contextlib import contextmanager

import pandas as pd

from tests.compat import raises
from tests.tools import PROJECT_ROOT, workspace_tmp

from aqs.config.schema import KNOWN_PROVIDERS, DataConfig
from aqs.core.exceptions import ConfigError, DataError
from aqs.data.cache import parquet_available
from aqs.data.file_provider import CsvProvider, ParquetProvider
from aqs.data.provider import DataProvider, ProviderCapabilities
from aqs.data.registry import (
    PENDING_PROVIDERS,
    PROVIDER_REGISTRY,
    _PROTOCOL_ATTRS,
    _unregistered_builtins,
    available_providers,
    build_provider,
    provider_capabilities,
    register_provider,
)
from aqs.data.synthetic_provider import SyntheticProvider

START = "2022-01-04"
END = "2022-03-31"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@contextmanager
def temp_provider(name: str, factory):
    """临时注册一个 provider，退出时**精确还原**（不污染其他用例）。"""
    existed = name in PROVIDER_REGISTRY
    previous = PROVIDER_REGISTRY.get(name)
    register_provider(name, factory, overwrite=True)
    try:
        yield
    finally:
        if existed and previous is not None:
            PROVIDER_REGISTRY[name] = previous
        else:
            PROVIDER_REGISTRY.pop(name, None)


class StubProvider:
    """手工满足 `DataProvider` 协议的最小对象（仅用于测试注册表，不做取数）。"""

    name = "stub"

    def __init__(self, ctx=None) -> None:
        self.ctx = ctx

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(daily_bars=True, listing_dates=True)

    def health_check(self):
        raise NotImplementedError

    def fetch_bars(self, *args, **kwargs):
        raise NotImplementedError

    def fetch_symbol_meta(self, *args, **kwargs):
        raise NotImplementedError

    def fetch_index_members(self, *args, **kwargs):
        raise NotImplementedError

    def fetch_fundamentals(self, *args, **kwargs):
        raise NotImplementedError

    def fetch_trading_calendar(self, *args, **kwargs):
        raise NotImplementedError

    def fetch_industry(self, *args, **kwargs):
        raise NotImplementedError


class PartialProvider:
    """缺 `fetch_bars` / `name` 的对象：必须被构建期闸门拦下。"""

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()


def stub_without(attr: str) -> object:
    """构造一个「只缺某一个协议属性」的桩对象（用于逐项验证闸门清单）。"""
    members = {name: getattr(StubProvider, name) for name in _PROTOCOL_ATTRS if name != attr}
    members["__init__"] = StubProvider.__init__
    return type("StubWithout", (), members)()


def capture_context(sink: dict) -> object:
    """返回一个把 `ProviderContext` 记进 `sink` 的工厂。"""
    def factory(ctx):
        sink["ctx"] = ctx
        return StubProvider(ctx)

    return factory


def write_min_csv(root, *, symbols=("600000.SH", "600001.SH"), days=4):
    """只写必需列的最小 CSV 数据集（能力推断应显示「除行情外什么都没有」）。"""
    root.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range("2022-03-01", periods=days)
    frames = []
    for i, symbol in enumerate(symbols):
        price = 10.0 + i
        frames.append(
            pd.DataFrame(
                {
                    "date": dates,
                    "symbol": symbol,
                    "open": price,
                    "high": price + 0.5,
                    "low": price - 0.5,
                    "close": price,
                    "volume": 1000.0,
                    "amount": 1000.0 * price,
                }
            )
        )
    pd.concat(frames, ignore_index=True).to_csv(root / "bars.csv", index=False, encoding="utf-8-sig")
    return root


# --------------------------------------------------------------------------- #
# 1~3：注册表本身
# --------------------------------------------------------------------------- #
def test_available_providers_covers_builtins_and_declares_pending():
    names = available_providers()

    assert names == sorted(names), "available_providers 必须排序，便于断言与展示"
    assert names == sorted(set(names)), "不应出现重复名字"
    assert {"synthetic", "csv", "parquet"}.issubset(set(names))
    # 注册表里不能出现配置层不认的名字（否则注册了也永远调不到 → 死代码）
    assert set(names) <= set(KNOWN_PROVIDERS), f"注册表含配置未声明的 provider：{names}"
    # 待接入清单必须与注册表状态一致：列在 PENDING 里的名字就是「还没实现」的
    for name in PENDING_PROVIDERS:
        assert name not in PROVIDER_REGISTRY, f"{name} 已注册，应从 PENDING_PROVIDERS 移除"


def test_register_duplicate_requires_explicit_overwrite():
    original = PROVIDER_REGISTRY["csv"]

    with raises(ConfigError) as ctx:
        register_provider("csv", StubProvider)
    assert "已注册" in str(ctx.value)
    assert PROVIDER_REGISTRY["csv"] is original, "重名注册失败后不得改动注册表"

    try:
        register_provider("csv", StubProvider, overwrite=True)
        assert PROVIDER_REGISTRY["csv"] is StubProvider
    finally:
        PROVIDER_REGISTRY["csv"] = original
    assert PROVIDER_REGISTRY["csv"] is original


def test_register_rejects_bad_name_or_non_callable_factory():
    with raises(ConfigError):
        register_provider("", StubProvider)
    with raises(ConfigError):
        register_provider("   ", StubProvider)
    with raises(ConfigError) as ctx:
        register_provider(" csv ", StubProvider)
    assert "空白" in str(ctx.value), "首尾空白的名字永远匹配不到配置，必须拒绝而不是照收"
    with raises(ConfigError) as ctx2:
        register_provider("demo", "not-callable")  # type: ignore[arg-type]
    assert "可调用" in str(ctx2.value)


# --------------------------------------------------------------------------- #
# 4~5：构建分发
# --------------------------------------------------------------------------- #
def test_build_provider_dispatches_by_config_provider_field():
    synthetic = build_provider(DataConfig(provider="synthetic"))
    assert isinstance(synthetic, SyntheticProvider) and synthetic.name == "synthetic"
    assert isinstance(synthetic, DataProvider)

    with workspace_tmp("registry_csv") as root:
        csv = build_provider(DataConfig(provider="csv", root=str(root)))
        assert isinstance(csv, CsvProvider) and csv.name == "csv"
        assert csv.root == root

    with workspace_tmp("registry_pq") as root:
        if parquet_available():
            pq = build_provider(DataConfig(provider="parquet", root=str(root)))
            assert isinstance(pq, ParquetProvider)
        else:
            # 环境不具备时必须显式报错并指出装什么，而不是静默换一个能用的源
            with raises(DataError) as ctx:
                build_provider(DataConfig(provider="parquet", root=str(root)))
            assert "pyarrow" in str(ctx.value)


def test_explicit_provider_argument_overrides_config_field():
    with workspace_tmp("registry_override") as root:
        provider = build_provider(DataConfig(provider="synthetic"), provider="csv", root=str(root))
    assert isinstance(provider, CsvProvider), "显式 provider= 必须覆盖 config.provider，而不是被忽略"
    assert provider.name == "csv"


# --------------------------------------------------------------------------- #
# 6：未知名 —— 绝不静默回退
# --------------------------------------------------------------------------- #
def test_unknown_provider_name_raises_config_error_without_fallback():
    for name in ("nope", "akshare_x", "Synthetic"):
        with raises(ConfigError) as ctx:
            build_provider(DataConfig(), provider=name)
        message = str(ctx.value)
        assert name in message
        assert "available" in message or "可用" in message, "报错必须给出可用清单"
        assert "synthetic" in message, "可用清单里应能看到默认源（正是不能回退到它的那个）"

    # 配置字段本身填了未知名：配置层先拦（内置白名单），运行层无需兜底
    with raises(ConfigError):
        DataConfig(provider="nope")


# --------------------------------------------------------------------------- #
# 7 / 13：上下文注入与路径解析口径
# --------------------------------------------------------------------------- #
def test_build_provider_passes_context_dependencies_verbatim():
    captured: dict[str, object] = {}
    marker_cache, marker_limiter, marker_client = object(), object(), object()

    def factory(ctx):
        captured["ctx"] = ctx
        return StubProvider(ctx)

    with workspace_tmp("registry_ctx") as root:
        with temp_provider("probe_ctx", factory):
            built = build_provider(
                DataConfig(provider="synthetic"),
                provider="probe_ctx",
                root=root,
                cache=marker_cache,
                limiter=marker_limiter,
                client=marker_client,
                index_members_path=root / "index" / "index_members.csv",
                fundamentals_path=root / "fundamentals.csv",
            )

    ctx = captured["ctx"]
    assert isinstance(built, StubProvider)
    assert ctx.name == "probe_ctx"
    assert ctx.root == root and ctx.root.is_absolute()
    assert ctx.cache is marker_cache and ctx.limiter is marker_limiter and ctx.client is marker_client
    assert ctx.index_members_path.name == "index_members.csv"
    assert ctx.fundamentals_path.name == "fundamentals.csv"


def test_root_resolution_reuses_project_root_rule():
    """相对路径一律按项目根解析（同一规则只有一处实现，避免 CWD 漂移）。"""
    captured: dict[str, object] = {}
    with temp_provider("probe_root", capture_context(captured)):
        build_provider(DataConfig(), provider="probe_root")  # 默认 root="data/raw"
        assert captured["ctx"].root == PROJECT_ROOT / "data" / "raw"
        assert captured["ctx"].root.is_absolute()

        build_provider(DataConfig(root="data/custom"), provider="probe_root")
        assert captured["ctx"].root == PROJECT_ROOT / "data" / "custom"

        with workspace_tmp("registry_abs") as abs_root:
            build_provider(DataConfig(root=str(abs_root)), provider="probe_root")
            assert captured["ctx"].root == abs_root, "绝对路径必须原样使用"


# --------------------------------------------------------------------------- #
# 8：构建期协议闸门
# --------------------------------------------------------------------------- #
def test_build_provider_rejects_protocol_violation_and_names_missing_attrs():
    with temp_provider("bad_provider", lambda ctx: PartialProvider()):
        with raises(DataError) as ctx:
            build_provider(DataConfig(), provider="bad_provider")
    message = str(ctx.value)
    assert "DataProvider" in message
    assert "fetch_bars" in message, "报错必须点名缺了哪个方法，而不是只说「不满足协议」"
    assert "name" in message, "数据成员 name 同样是协议要求"

    # 闸门清单自身不能漂移：逐项删除都必须被拦下
    for attr in _PROTOCOL_ATTRS:
        assert not isinstance(stub_without(attr), DataProvider), f"缺少 {attr} 时协议检查应失败"
    assert isinstance(StubProvider(), DataProvider), "完整实现必须通过闸门"


# --------------------------------------------------------------------------- #
# 9：A4 —— 配置层声明式、运行层构造式
# --------------------------------------------------------------------------- #
def test_config_layer_is_declarative_while_build_provider_uses_registry():
    with temp_provider("demo", lambda ctx: StubProvider(ctx)):
        # 配置层只认内置白名单：扩展 provider 不能写进 YAML（声明与实现解耦）
        with raises(ConfigError) as ctx:
            DataConfig(provider="demo")
        assert "demo" in str(ctx.value)
        # 运行层按注册表查找：扩展 provider 可以用代码显式构建
        built = build_provider(DataConfig(), provider="demo")
        assert isinstance(built, StubProvider)


# --------------------------------------------------------------------------- #
# 10：内置 provider 缺席自检（纯函数，正反例）
# --------------------------------------------------------------------------- #
def test_unregistered_builtins_detects_missing_implementation():
    # 正例：当前状态（akshare 尚在 PENDING）不应报警
    assert _unregistered_builtins(PROVIDER_REGISTRY, pending=PENDING_PROVIDERS) == []
    # 反例：漏注册内置 provider 必须被点名
    without_csv = {k: v for k, v in PROVIDER_REGISTRY.items() if k != "csv"}
    assert _unregistered_builtins(without_csv, pending=PENDING_PROVIDERS) == ["csv"]
    # 穷尽性：不做任何豁免时，四个内置名必须全部列出（检查不是「看起来跑了」）
    assert _unregistered_builtins({}, pending=()) == sorted(KNOWN_PROVIDERS)
    assert _unregistered_builtins({}, pending=KNOWN_PROVIDERS) == []
    # 反例：PENDING 写错了名字（不再等于「确实没实现」）—— 靠子集断言拦住
    assert set(PENDING_PROVIDERS) <= set(KNOWN_PROVIDERS), (
        f"PENDING_PROVIDERS 必须是内置名单的子集，否则拼错名字会让自检失声：{PENDING_PROVIDERS}"
    )


# --------------------------------------------------------------------------- #
# 11~12：能力查询
# --------------------------------------------------------------------------- #
def test_provider_capabilities_for_synthetic_and_csv_without_network():
    caps = provider_capabilities("synthetic")
    assert caps.daily_bars and caps.listing_dates
    assert caps.default_adjust == DataConfig().adjustment

    with workspace_tmp("registry_caps") as root:
        write_min_csv(root)
        cfg = DataConfig(provider="csv", root=str(root))
        # strict 是「可用性闸门」：缺 list_date → 必需能力不全 → 抛错（并说清缺什么）
        with raises(DataError) as ctx:
            provider_capabilities("csv", config=cfg)
        assert "listing_dates" in str(ctx.value)

        csv_caps = provider_capabilities("csv", config=cfg, strict=False)
        assert csv_caps.daily_bars is True
        assert csv_caps.listing_dates is False, "没有 list_date 列就必须如实说没有"
        assert csv_caps.missing_required() == ("listing_dates",)

    # 未知名：无论 strict 与否都是配置错误（不是「探测失败」）
    with raises(ConfigError):
        provider_capabilities("nope")
    with raises(ConfigError):
        provider_capabilities("nope", strict=False)


def test_provider_capabilities_strict_vs_degraded_are_distinguishable():
    with workspace_tmp("registry_caps_bad") as root:
        missing = root / "does_not_exist"
        bad = DataConfig(provider="csv", root=str(missing))

        with raises(DataError):
            provider_capabilities("csv", config=bad)

        degraded = provider_capabilities("csv", config=bad, strict=False)
        assert degraded.daily_bars is False, "探测失败时必须退到下界"
        assert degraded.notes and any("失败" in note for note in degraded.notes), (
            "「查不到能力」必须写明原因，不能伪装成「天生没有这个能力」"
        )

        # Parquet 分支在有/无 pyarrow 两种环境下都必须报错（构造失败或探测失败）
        with raises(DataError) as ctx2:
            provider_capabilities("parquet", config=DataConfig(provider="parquet", root=str(missing)))
        assert "parquet" in str(ctx2.value)
