"""VaR / ES（历史模拟法）测试，含突破率验收。"""

from __future__ import annotations

import numpy as np

from tests.compat import approx, raises

from aqs.core.exceptions import ConfigError, InsufficientDataError
from aqs.risk.var import (
    expected_shortfall,
    historical_var_es,
    portfolio_returns,
    quantile_loss,
    var_breach_rate,
)

RETURNS = [-0.05, -0.03, -0.02, -0.01, 0.0, 0.01, 0.02, -0.04, 0.03, 0.015]


def test_quantile_loss_hand_computed():
    # 线性插值分位数：95% → 位置 (10-1)×0.05 = 0.45 → -0.05 + 0.45×0.01 = -0.0455
    assert quantile_loss(RETURNS, 0.95) == approx(0.0455)
    # 80% → 位置 (10-1)×0.20 = 1.8 → -0.04 + 0.8×0.01 = -0.032
    assert quantile_loss(RETURNS, 0.80) == approx(0.032)


def test_expected_shortfall_at_least_var():
    var = quantile_loss(RETURNS, 0.95)
    es = expected_shortfall(RETURNS, 0.95)
    assert es >= var - 1e-12


def test_horizon_scaling_square_root():
    one = historical_var_es(RETURNS, confidence=0.95, horizon=1, min_obs=5)
    four = historical_var_es(RETURNS, confidence=0.95, horizon=4, min_obs=5)
    assert four.var == approx(one.var * 2.0)
    assert four.es == approx(one.es * 2.0)
    assert four.horizon == 4


def test_confidence_monotonic():
    low = historical_var_es(RETURNS, confidence=0.90, horizon=1, min_obs=5).var
    high = historical_var_es(RETURNS, confidence=0.99, horizon=1, min_obs=5).var
    assert high >= low


def test_var_result_fields():
    result = historical_var_es(RETURNS, confidence=0.95, min_obs=5)
    assert result.n_obs == len(RETURNS)
    assert result.quantile_return < 0
    assert result.var > 0
    assert set(result.as_dict()) >= {"var", "es", "confidence", "horizon", "n_obs", "quantile_return"}


def test_insufficient_observations_rejected():
    with raises(InsufficientDataError):
        historical_var_es([0.01, -0.02], confidence=0.95, min_obs=20)


def test_invalid_confidence_rejected():
    with raises(ConfigError):
        quantile_loss(RETURNS, 1.5)
    with raises(ConfigError):
        historical_var_es(RETURNS, horizon=0, min_obs=5)


def test_nan_returns_are_dropped():
    result = historical_var_es([*RETURNS, float("nan"), float("inf")], confidence=0.95, min_obs=5)
    assert result.n_obs == len(RETURNS)


# --------------------------------------------------------------------------- #
# 组合收益
# --------------------------------------------------------------------------- #
def test_portfolio_returns_weighted_sum():
    matrix = np.array([[0.01, 0.02], [0.03, -0.01], [-0.02, 0.005]])
    out = portfolio_returns([0.5, 0.5], matrix)
    assert out.shape == (3,)
    assert out[0] == approx(0.015)
    assert out[1] == approx(0.01)
    assert out[2] == approx(-0.0075)


def test_portfolio_returns_partial_cash_is_not_leveraged():
    matrix = np.array([[0.10, 0.20], [0.00, 0.00]])
    out = portfolio_returns([0.5, 0.0], matrix)
    assert out[0] == approx(0.05)  # 剩余 50% 为现金，收益 0


def test_portfolio_returns_rejects_leverage():
    matrix = np.array([[0.01, 0.02]])
    with raises(ConfigError):
        portfolio_returns([1.0, 0.5], matrix)


def test_portfolio_returns_dimension_mismatch():
    matrix = np.array([[0.01, 0.02]])
    with raises(ConfigError):
        portfolio_returns([1.0], matrix)


# --------------------------------------------------------------------------- #
# 突破率（验收口径）
# --------------------------------------------------------------------------- #
def test_breach_rate_close_to_expected():
    rng = np.random.default_rng(20240101)
    returns = rng.normal(0.0, 0.02, 3000)
    result = var_breach_rate(returns, confidence=0.95, window=250, min_window=60)
    assert result.n_obs > 2000
    assert 0.03 <= result.breach_rate <= 0.08, result.as_dict()
    assert result.expected_rate == approx(0.05)
    assert result.within_tolerance is True


def test_breach_rate_higher_confidence_fewer_breaches():
    rng = np.random.default_rng(7)
    returns = rng.normal(0.0, 0.015, 2000)
    r95 = var_breach_rate(returns, confidence=0.95, window=250)
    r99 = var_breach_rate(returns, confidence=0.99, window=250)
    assert r99.n_breach <= r95.n_breach
    assert r99.breach_rate < r95.breach_rate


def test_breach_rate_validates_window():
    rng = np.random.default_rng(1)
    returns = rng.normal(0.0, 0.01, 200)
    with raises(ConfigError):
        var_breach_rate(returns, window=10, min_window=60)


def test_breach_rate_reports_fields():
    rng = np.random.default_rng(3)
    result = var_breach_rate(rng.normal(0.0, 0.01, 500), confidence=0.95, window=100, min_window=60)
    assert set(result.as_dict()) == {
        "confidence",
        "n_obs",
        "n_breach",
        "breach_rate",
        "expected_rate",
    }
