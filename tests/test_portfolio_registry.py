"""组合层注册表与配置驱动构建测试。"""

from __future__ import annotations

from tests.compat import approx, raises

from aqs.config.loader import load_base_config
from aqs.core.exceptions import ConfigError
from aqs.portfolio.registry import (
    PORTFOLIO_REGISTRY,
    available_portfolios,
    build_portfolio,
    get_portfolio_class,
    load_portfolio,
    register_portfolio,
    sizing_from_params,
)
from aqs.portfolio.target_weight import TargetWeightPortfolio

CONFIG_FILES = {
    "ma_cross": "configs/strategies/ma_cross.yaml",
    "breakout": "configs/strategies/breakout.yaml",
    "volume": "configs/strategies/volume.yaml",
}


def test_registry_contains_builtin():
    assert "target_weight" in available_portfolios()
    assert get_portfolio_class("target_weight") is TargetWeightPortfolio
    assert get_portfolio_class("equal") is TargetWeightPortfolio


def test_unknown_portfolio_rejected():
    with raises(ConfigError):
        get_portfolio_class("magic_portfolio")
    with raises(ConfigError):
        build_portfolio({}, name="magic_portfolio")


def test_load_portfolio_from_strategy_files():
    base = load_base_config("configs/base.yaml")
    for path in CONFIG_FILES.values():
        portfolio = load_portfolio(path, defaults=base.portfolio, lot_size=base.engine.lot_size)
        info = portfolio.describe()
        assert isinstance(portfolio, TargetWeightPortfolio)
        assert info["weighting"] == "equal"
        assert info["max_positions"] == 10
        assert info["max_weight_per_symbol"] == approx(0.10)
        assert info["cash_buffer"] == approx(0.02)
        assert info["allow_reentry"] is True
        assert info["lot_size"] == 100


def test_defaults_come_from_base_config():
    base = load_base_config("configs/base.yaml")
    portfolio = build_portfolio({}, defaults=base.portfolio, lot_size=50)
    info = portfolio.describe()
    assert info["max_positions"] == base.portfolio.max_positions
    assert info["max_weight_per_symbol"] == approx(base.portfolio.max_weight_per_symbol)
    assert info["lot_size"] == 50  # 引擎的 lot_size 优先
    assert info["kelly"]["enabled"] is base.portfolio.kelly.enabled


def test_strategy_section_overrides_defaults():
    base = load_base_config("configs/base.yaml")
    portfolio = build_portfolio(
        {"max_positions": 3, "max_weight_per_symbol": 0.25, "rebalance": True},
        defaults=base.portfolio,
    )
    info = portfolio.describe()
    assert info["max_positions"] == 3
    assert info["max_weight_per_symbol"] == approx(0.25)
    assert info["rebalance"] is True


def test_unknown_key_rejected():
    with raises(ConfigError) as exc:
        sizing_from_params({"max_position": 10})
    assert "max_position" in str(exc.value)

    with raises(ConfigError) as exc2:
        sizing_from_params({"kelly": {"half": True}})
    assert "half" in str(exc2.value)


def test_invalid_weighting_rejected():
    with raises(ConfigError):
        sizing_from_params({"weighting": "mean_variance"})


def test_kelly_section_parsed():
    cfg = sizing_from_params(
        {"kelly": {"enabled": True, "mode": "continuous", "fraction": 0.25, "cap": 0.5, "min_trades": 5}}
    )
    assert cfg.kelly.enabled is True
    assert cfg.kelly.mode == "continuous"
    assert cfg.kelly.fraction == approx(0.25)
    assert cfg.kelly.cap == approx(0.5)
    assert cfg.kelly.min_trades == 5


def test_register_custom_portfolio():
    class Dummy(TargetWeightPortfolio):
        name = "dummy_portfolio"

    register_portfolio(Dummy, name="dummy_portfolio")
    try:
        assert "dummy_portfolio" in available_portfolios()
        instance = build_portfolio({}, name="dummy_portfolio")
        assert isinstance(instance, Dummy)
    finally:
        PORTFOLIO_REGISTRY.pop("dummy_portfolio", None)
    assert "dummy_portfolio" not in available_portfolios()


def test_portfolio_name_can_be_overridden_in_params():
    class Named(TargetWeightPortfolio):
        name = "named_portfolio"

    register_portfolio(Named, name="named_portfolio")
    try:
        instance = build_portfolio({"name": "named_portfolio"})
        assert isinstance(instance, Named)
    finally:
        PORTFOLIO_REGISTRY.pop("named_portfolio", None)
