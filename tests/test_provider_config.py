"""M4-1：数据源取数行为配置（缓存 / 限流 / 重试 / 单位换算）。

设计依据 `docs/18_m4_dataprovider.md` §3.3（O4 决议：**平铺字段**，
不做 `data.providers.<name>.*` 嵌套 —— 四 provider 的取数行为应当一致，差异只在字段映射）。
"""

from __future__ import annotations

from tests.compat import approx, raises

from aqs.config.loader import load_base_config
from aqs.config.schema import (
    CacheConfig,
    DataConfig,
    RateLimitConfig,
    RetryConfig,
    UnitConversionConfig,
    construct,
)
from aqs.core.exceptions import ConfigError


# --------------------------------------------------------------------------- #
# 1. 默认值是安全的
# --------------------------------------------------------------------------- #
def test_m4_config_defaults_are_conservative():
    """默认值必须「开箱安全」：缓存开、限流保守、单位不做隐式换算。"""
    data = DataConfig()

    assert data.cache.enabled is True
    assert data.cache.fmt == "parquet"
    assert data.cache.version == 1
    assert data.cache.ttl_hours == approx(24.0)
    assert data.cache.refresh is False

    assert data.rate_limit.enabled is True
    assert data.rate_limit.requests_per_minute == 300
    assert data.rate_limit.burst == 10

    assert data.retry.max_attempts == 5
    assert data.retry.backoff == approx(1.5)
    assert data.retry.jitter is True

    # 单位换算默认恒等：**不猜**数据源的单位（Q3 陷阱必须显式配置）
    assert data.unit_conversion.volume_to_shares == approx(1.0)
    assert data.unit_conversion.amount_to_yuan == approx(1.0)

    assert data.failure_policy == "fallback"
    assert data.max_missing_ratio == approx(0.01)


def test_m4_blocks_are_present_in_base_yaml():
    """`configs/base.yaml` 必须显式携带 M4 配置块（不允许只靠代码默认值）。"""
    cfg = load_base_config("configs/base.yaml")

    assert cfg.data.cache.root == "data/cache"
    assert cfg.data.cache.version == 1
    assert cfg.data.rate_limit.requests_per_minute == 300
    assert cfg.data.retry.max_attempts == 5
    assert cfg.data.retry.retry_on == ("ConnectionError", "TimeoutError", "OSError")
    assert cfg.data.unit_conversion.volume_to_shares == approx(1.0)
    assert cfg.data.failure_policy == "fallback"
    # listing_date 之前只靠默认值，现在显式写出（默认即 strict，缺 list_date 报错）
    assert cfg.data.listing_date.policy == "strict"


# --------------------------------------------------------------------------- #
# 2. 校验：非法值必须报错，而不是静默生效
# --------------------------------------------------------------------------- #
def test_cache_config_validation():
    assert CacheConfig(fmt="csv").fmt == "csv"

    with raises(ConfigError) as e1:
        CacheConfig(fmt="feather")
    assert "fmt" in str(e1.value)

    with raises(ConfigError):
        CacheConfig(version=0)

    with raises(ConfigError):
        CacheConfig(ttl_hours=-1.0)


def test_rate_limit_retry_and_unit_validation():
    # requests_per_minute = 0 合法：表示不限流
    assert RateLimitConfig(requests_per_minute=0).requests_per_minute == 0

    with raises(ConfigError):
        RateLimitConfig(requests_per_minute=-1)
    with raises(ConfigError):
        RateLimitConfig(burst=0)
    with raises(ConfigError):
        RateLimitConfig(min_interval_ms=-0.5)

    with raises(ConfigError):
        RetryConfig(max_attempts=0)
    with raises(ConfigError):
        RetryConfig(backoff=0.5)      # 指数退避底数必须 >= 1

    with raises(ConfigError):
        UnitConversionConfig(volume_to_shares=0.0)
    with raises(ConfigError):
        UnitConversionConfig(amount_to_yuan=-100.0)


def test_data_config_rejects_bad_failure_policy_and_missing_ratio():
    with raises(ConfigError) as e1:
        DataConfig(failure_policy="ignore")
    assert "failure_policy" in str(e1.value)

    for bad in (-0.01, 1.5):
        with raises(ConfigError):
            DataConfig(max_missing_ratio=bad)

    # 边界值合法
    assert DataConfig(max_missing_ratio=0.0).max_missing_ratio == approx(0.0)
    assert DataConfig(max_missing_ratio=1.0).max_missing_ratio == approx(1.0)


def test_m4_blocks_survive_overlay_and_construct():
    """通过 `construct`/`with_overlay` 构造时，嵌套块必须被正确反序列化。"""
    cfg = construct(
        DataConfig,
        {
            "provider": "akshare",
            "cache": {"enabled": False, "version": 3, "fmt": "csv"},
            "rate_limit": {"requests_per_minute": 60, "burst": 2},
            "retry": {"max_attempts": 2, "retry_on": ["TimeoutError"]},
            "unit_conversion": {"volume_to_shares": 100.0},
        },
    )
    assert cfg.cache.enabled is False
    assert cfg.cache.version == 3
    assert cfg.cache.fmt == "csv"
    assert cfg.rate_limit.requests_per_minute == 60
    assert cfg.rate_limit.burst == 2
    assert cfg.retry.retry_on == ("TimeoutError",)
    assert cfg.unit_conversion.volume_to_shares == approx(100.0)


def test_base_config_overlay_reaches_m4_blocks():
    """`BaseConfig.with_overlay` 必须能改到 M4 配置块（否则敏感性/CLI 覆盖会静默失效）。"""
    from aqs.config.schema import BaseConfig

    base = BaseConfig()
    overlaid = base.with_overlay(
        {
            "data": {
                "provider": "akshare",
                "cache": {"ttl_hours": 1.0, "version": 2},
                "unit_conversion": {"volume_to_shares": 100.0},
                "rate_limit": {"requests_per_minute": 30},
            }
        }
    )
    assert overlaid.data.provider == "akshare"
    assert overlaid.data.cache.ttl_hours == approx(1.0)
    assert overlaid.data.cache.version == 2
    assert overlaid.data.unit_conversion.volume_to_shares == approx(100.0)
    assert overlaid.data.rate_limit.requests_per_minute == 30
    # 覆盖必须是纯函数：原对象不受影响
    assert base.data.cache.ttl_hours == approx(24.0)
    assert base.data.provider == "synthetic"
