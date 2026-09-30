"""配置层测试：默认值、覆盖、未知键、成本场景、RMS 配置。"""

from __future__ import annotations

from datetime import date

from tests.compat import approx, raises

from aqs.config.loader import (
    env_overrides,
    load_base_config,
    load_cost_scenario,
    load_risk_config,
    load_strategy_config,
    parse_overrides,
)
from aqs.config.schema import (
    BaseConfig,
    CostConfig,
    DataConfig,
    RiskConfig,
    RiskRuleConfig,
    construct,
)
from aqs.core.exceptions import ConfigError


def test_load_base_config_defaults():
    cfg = load_base_config("configs/base.yaml")
    assert cfg.project.name == "aqs"
    assert cfg.data.min_list_days == 60
    assert cfg.data.liquidity.min_amount == 50_000_000
    assert cfg.data.liquidity.window == 20
    assert cfg.engine.t_plus_one is True
    assert cfg.engine.lot_size == 100
    assert cfg.engine.max_defer_days == 5
    assert cfg.engine.matching.max_participation == approx(0.10)
    assert cfg.costs.commission_rate == approx(0.00025)
    assert cfg.costs.transfer_fee_rate == approx(0.00001)
    assert cfg.costs.slippage.prop_bps == approx(10.0)
    assert cfg.costs.impact.enabled is True
    assert cfg.portfolio.max_positions == 10
    assert cfg.portfolio.max_weight_per_symbol == approx(0.10)
    assert cfg.universe.index_code == "000300.SH"
    assert len(cfg.engine.benchmark) == 3


def test_unknown_key_rejected():
    with raises(ConfigError) as exc:
        construct(BaseConfig, {"data": {"min_list_days": 60, "typo_key": 1}})
    assert "typo_key" in str(exc.value)


def test_wrong_type_rejected():
    with raises(ConfigError):
        construct(BaseConfig, {"engine": {"initial_cash": "一百万"}})
    with raises(ConfigError):
        construct(BaseConfig, {"data": {"exclude_st": "yes"}})


def test_invalid_enum_like_value_rejected():
    with raises(ConfigError):
        construct(BaseConfig, {"data": {"provider": "wind"}})
    with raises(ConfigError):
        construct(BaseConfig, {"engine": {"execution_price": "midpoint"}})
    with raises(ConfigError):
        construct(BaseConfig, {"engine": {"matching": {"max_participation": 1.5}}})


def test_cli_overrides_parse_and_apply():
    overrides = parse_overrides(
        ["costs.commission_rate=0.0005", "engine.start=2020-01-01", "engine.matching.allow_partial_fill=false"]
    )
    assert overrides["costs"]["commission_rate"] == 0.0005
    cfg = load_base_config("configs/base.yaml", overrides=overrides, use_env=False)
    assert cfg.costs.commission_rate == approx(0.0005)
    assert cfg.engine.start == date(2020, 1, 1)
    assert cfg.engine.matching.allow_partial_fill is False
    assert cfg.engine.initial_cash == approx(1_000_000.0)


def test_env_overrides():
    env = {"AQS__DATA__MIN_LIST_DAYS": "30", "AQS__COSTS__COMMISSION_RATE": "0.0001", "OTHER": "x"}
    ov = env_overrides(env=env)
    assert ov == {"data": {"min_list_days": 30}, "costs": {"commission_rate": 0.0001}}
    cfg = load_base_config("configs/base.yaml", overrides=ov, use_env=False)
    assert cfg.data.min_list_days == 30
    assert cfg.costs.commission_rate == approx(0.0001)


def test_with_overlay_is_pure():
    cfg = load_base_config("configs/base.yaml")
    new = cfg.with_overlay({"costs": {"slippage": {"prop_bps": 30.0}}})
    assert new.costs.slippage.prop_bps == approx(30.0)
    assert cfg.costs.slippage.prop_bps == approx(10.0)


def test_cost_scale_is_not_double_counted():
    base = load_base_config("configs/base.yaml")
    doubled = base.with_cost_scale(2.0)
    assert doubled.costs.scale == approx(2.0)
    assert doubled.costs.commission_rate == approx(base.costs.commission_rate)
    assert doubled.costs.scaled(2.0).scale == approx(4.0)


def test_stamp_tax_schedule_by_date():
    cfg = load_base_config("configs/base.yaml")
    assert cfg.costs.stamp_tax.rate_on(date(2010, 1, 4)) == approx(0.001)
    assert cfg.costs.stamp_tax.rate_on(date(2023, 8, 25)) == approx(0.001)
    assert cfg.costs.stamp_tax.rate_on(date(2023, 8, 28)) == approx(0.0005)
    assert cfg.costs.stamp_tax.rate_on(date(2024, 5, 1)) == approx(0.0005)


def test_cost_scenarios():
    overlay = load_cost_scenario("configs/costs.yaml", "double_cost")
    assert overlay == {"costs": {"scale": 2.0}}
    cfg = load_base_config("configs/base.yaml").with_overlay(overlay)
    assert cfg.costs.scale == approx(2.0)
    with raises(ConfigError):
        load_cost_scenario("configs/costs.yaml", "not_exists")


def test_strategy_configs_loadable():
    for name in ("ma_cross", "breakout", "volume"):
        cfg = load_strategy_config(f"configs/strategies/{name}.yaml")
        assert cfg["name"] == name
        assert "params" in cfg
        assert "execution" in cfg
        assert cfg["portfolio"]["max_positions"] == 10
        assert cfg["filters"]["min_amount"] == 50_000_000


def test_risk_config_rules_sorted_and_unique():
    risk = load_risk_config("configs/risk.yaml")
    assert risk.mode == "enforce"
    priorities = [r.priority for r in risk.rules]
    assert priorities == sorted(priorities)
    names = [r.name for r in risk.rules]
    assert len(names) == len(set(names))
    for required in (
        "max_order_quantity",
        "max_trade_volume_daily",
        "max_position_per_symbol",
        "industry_exposure",
        "gross_exposure",
        "price_deviation",
        "order_frequency",
        "cancel_ratio",
        "daily_loss_limit",
        "max_drawdown_action",
    ):
        assert risk.rule(required) is not None, f"风控规则缺失：{required}"
    assert risk.rule("max_drawdown_action").action == "force_close"
    assert risk.rule("daily_loss_limit").action == "pause"


def test_duplicate_risk_rule_names_rejected():
    with raises(ConfigError):
        RiskConfig(
            rules=[
                RiskRuleConfig(name="dup", priority=1),
                RiskRuleConfig(name="dup", priority=2),
            ]
        )


def test_cost_config_scaled_requires_positive():
    with raises(ConfigError):
        CostConfig().scaled(0)


def test_data_config_validation():
    cfg = DataConfig()
    assert cfg.adjustment == "hfq"
    assert cfg.quality.strict is True
