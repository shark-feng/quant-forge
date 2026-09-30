"""策略注册表与配置驱动构建测试。"""

from __future__ import annotations

from tests.compat import raises

from aqs.config.schema import DataConfig, construct
from aqs.core.exceptions import ConfigError
from aqs.strategy.breakout import BreakoutStrategy
from aqs.strategy.ma_cross import MACrossStrategy
from aqs.strategy.registry import (
    STRATEGY_REGISTRY,
    StrategySpec,
    available_strategies,
    build_strategy,
    filters_to_data_overrides,
    get_strategy_class,
    load_spec,
    load_strategy,
    register_strategy,
)
from aqs.strategy.volume import VolumeStrategy

CONFIG_FILES = {
    "ma_cross": "configs/strategies/ma_cross.yaml",
    "breakout": "configs/strategies/breakout.yaml",
    "volume": "configs/strategies/volume.yaml",
}


def test_registry_contains_builtins():
    names = available_strategies()
    for expected in ("ma_cross", "breakout", "volume"):
        assert expected in names
        assert expected in STRATEGY_REGISTRY
    assert get_strategy_class("ma_cross") is MACrossStrategy
    assert get_strategy_class("breakout") is BreakoutStrategy
    assert get_strategy_class("volume") is VolumeStrategy


def test_unknown_strategy_rejected():
    with raises(ConfigError) as exc:
        get_strategy_class("magic_alpha")
    assert "magic_alpha" in str(exc.value)
    with raises(ConfigError):
        build_strategy({"name": "magic_alpha"})


def test_all_shipped_configs_are_valid_and_buildable():
    """配置文件与策略类的参数定义必须一致（防止改名后配置静默失效）。"""
    expected_params = {
        "ma_cross": {"fast_window": 5, "slow_window": 20},
        "breakout": {"window": 20, "exclude_today": True},
        "volume": {"volume_window": 20, "volume_ratio": 1.5},
    }
    for name, path in CONFIG_FILES.items():
        spec = load_spec(path)
        assert spec.name == name
        strategy = build_strategy(spec)
        assert isinstance(strategy, get_strategy_class(name))
        for key, value in expected_params[name].items():
            assert spec.params[key] == value
            assert strategy.params[key] == value
        assert spec.filters["min_amount"] == 50_000_000
        assert spec.execution["price"] == "open"
        assert spec.portfolio["max_positions"] == 10
        assert spec.signal  # 人类可读信号表达式存在


def test_load_strategy_returns_instance():
    strategy = load_strategy("configs/strategies/ma_cross.yaml")
    assert isinstance(strategy, MACrossStrategy)
    assert strategy.fast_window == 5
    assert strategy.slow_window == 20


def test_spec_rejects_unknown_section():
    with raises(ConfigError) as exc:
        StrategySpec.from_mapping({"name": "ma_cross", "param": {"fast_window": 5}})
    assert "param" in str(exc.value)


def test_spec_requires_name():
    with raises(ConfigError):
        StrategySpec.from_mapping({"params": {"fast_window": 5}})


def test_spec_rejects_non_mapping_section():
    with raises(ConfigError):
        StrategySpec.from_mapping({"name": "ma_cross", "params": [1, 2, 3]})


def test_spec_to_dict_roundtrip():
    spec = load_spec("configs/strategies/breakout.yaml")
    again = StrategySpec.from_mapping(spec.to_dict())
    assert again.name == spec.name
    assert again.params == spec.params
    assert again.filters == spec.filters


def test_filters_to_data_overrides():
    overrides = filters_to_data_overrides(
        {
            "exclude_st": True,
            "exclude_suspended": False,
            "min_list_days": 60,
            "liquidity_window": 20,
            "min_amount": 50_000_000,
        }
    )
    assert overrides == {
        "exclude_st": True,
        "exclude_suspended": False,
        "min_list_days": 60,
        "liquidity": {"window": 20, "min_amount": 50_000_000},
    }
    cfg = construct(DataConfig, overrides)
    assert cfg.min_list_days == 60
    assert cfg.liquidity.min_amount == 50_000_000
    assert cfg.exclude_suspended is False


def test_filters_reject_unknown_key():
    with raises(ConfigError) as exc:
        filters_to_data_overrides({"min_list_day": 60})
    assert "min_list_day" in str(exc.value)


def test_register_custom_strategy():
    class Dummy(MACrossStrategy):
        name = "dummy_test_strategy"

    register_strategy(Dummy, name="dummy_test_strategy")
    try:
        assert "dummy_test_strategy" in available_strategies()
        built = build_strategy({"name": "dummy_test_strategy", "params": {"fast_window": 3, "slow_window": 8}})
        assert isinstance(built, Dummy)
        assert built.fast_window == 3
    finally:
        STRATEGY_REGISTRY.pop("dummy_test_strategy", None)
    assert "dummy_test_strategy" not in available_strategies()


def test_strategy_params_from_config_are_typed():
    spec = load_spec("configs/strategies/volume.yaml")
    strategy = build_strategy(spec)
    assert isinstance(strategy, VolumeStrategy)
    assert strategy.exclude_today is True
    assert strategy.volume_ratio == 1.5
