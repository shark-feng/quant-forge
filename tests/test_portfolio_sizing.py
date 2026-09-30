"""仓位计算测试：等权、单票上限、凯利公式（含手算用例）。"""

from __future__ import annotations

from tests.compat import approx, raises

from aqs.core.exceptions import ConfigError
from aqs.portfolio.sizing import (
    KellySizing,
    SizingConfig,
    cap_weights,
    equal_weights,
    kelly_binary,
    kelly_continuous,
    kelly_from_trades,
    kelly_stats_from_trades,
    score_weights,
)


# --------------------------------------------------------------------------- #
# 基础权重
# --------------------------------------------------------------------------- #
def test_equal_weights():
    w = equal_weights(["A", "B", "C", "D", "E"])
    assert len(w) == 5
    for value in w.values():
        assert value == approx(0.2)
    assert sum(w.values()) == approx(1.0)


def test_equal_weights_dedup_and_empty():
    assert equal_weights([]) == {}
    w = equal_weights(["A", "A", "B"])
    assert set(w) == {"A", "B"}
    assert w["A"] == approx(0.5)


def test_equal_weights_with_cap():
    # 等权值低于上限 → 保持等权
    w = equal_weights([f"S{i}" for i in range(20)], max_weight=0.10)
    assert all(v == approx(0.05) for v in w.values())
    # 等权值高于上限 → 被削到上限，超出部分不重新分配（保守，留现金）
    w2 = equal_weights(["A", "B", "C"], max_weight=0.10)
    assert all(v == approx(0.10) for v in w2.values())
    assert sum(w2.values()) == approx(0.30)


def test_cap_weights_removes_negative():
    w = cap_weights({"A": -0.5, "B": 0.3, "C": 0.9}, 0.4)
    assert w == {"A": 0.0, "B": 0.3, "C": 0.4}


def test_cap_weights_invalid_max():
    with raises(ConfigError):
        cap_weights({"A": 0.5}, 0.0)


def test_score_weights_proportional():
    w = score_weights([("A", 3.0), ("B", 1.0)], max_weight=1.0)
    assert w["A"] == approx(0.75)
    assert w["B"] == approx(0.25)
    assert sum(w.values()) == approx(1.0)


def test_score_weights_negative_scores_ignored():
    w = score_weights([("A", 2.0), ("B", -1.0)], max_weight=1.0)
    assert w["A"] == approx(1.0)
    assert w["B"] == approx(0.0)


def test_score_weights_all_zero_falls_back_to_equal():
    w = score_weights([("A", 0.0), ("B", 0.0)], max_weight=1.0)
    assert w["A"] == approx(0.5)
    assert w["B"] == approx(0.5)


def test_score_weights_respect_cap():
    w = score_weights([("A", 9.0), ("B", 1.0)], max_weight=0.10)
    assert w["A"] == approx(0.10)
    assert w["B"] == approx(0.10)


# --------------------------------------------------------------------------- #
# 凯利公式（手算）
# --------------------------------------------------------------------------- #
def test_kelly_binary_hand_computed():
    # f* = p - q/b = 0.6 - 0.4/1.5 = 0.3333...
    assert kelly_binary(0.6, 1.5) == approx(0.6 - 0.4 / 1.5)
    # 无优势：p=0.5, b=1 → 0
    assert kelly_binary(0.5, 1.0) == approx(0.0)
    # 劣势：p=0.4, b=1 → -0.2（负值表示不应下注）
    assert kelly_binary(0.4, 1.0) < 0
    # 高赔率：p=0.4, b=3 → 0.4 - 0.6/3 = 0.2
    assert kelly_binary(0.4, 3.0) == approx(0.2)


def test_kelly_binary_edge_cases():
    assert kelly_binary(1.0, 2.0) == approx(1.0)
    assert kelly_binary(0.5, 0.0) == approx(0.0)   # 赔率非法 → 不下注
    with raises(ConfigError):
        kelly_binary(1.5, 1.0)


def test_kelly_continuous_hand_computed():
    # f* = μ/σ² = 0.002 / 0.02² = 5.0
    assert kelly_continuous(0.002, 0.02) == approx(5.0)
    assert kelly_continuous(0.001, 0.0) == approx(0.0)


def test_kelly_stats_from_trades():
    stats = kelly_stats_from_trades([0.10, -0.05, 0.20, -0.05])
    assert stats.n_trades == 4
    assert stats.win_rate == approx(0.5)
    assert stats.avg_win == approx(0.15)
    assert stats.avg_loss == approx(-0.05)
    assert stats.payoff_ratio == approx(3.0)
    assert stats.mean_return == approx(0.05)


def test_kelly_stats_without_losses():
    stats = kelly_stats_from_trades([0.1, 0.2])
    assert stats.payoff_ratio == approx(1.0)  # 无亏损样本时取下限，避免除零与放大
    assert stats.win_rate == approx(1.0)


def test_kelly_from_trades_hand_computed():
    trades = [0.10, -0.05, 0.20, -0.05] * 5  # 20 笔
    cfg = KellySizing(enabled=True, fraction=0.5, cap=1.0, min_trades=20)
    result = kelly_from_trades(trades, cfg)
    assert result.ready is True
    # f* = 0.5 - 0.5/3 = 0.3333；半凯利 = 0.1667
    assert result.f_star == approx(0.5 - 0.5 / 3.0)
    assert result.exposure == approx(0.5 * (0.5 - 0.5 / 3.0))
    assert result.stats.n_trades == 20


def test_half_kelly_fraction_applied():
    trades = [0.10, -0.05] * 10
    full = kelly_from_trades(trades, KellySizing(fraction=1.0, min_trades=20))
    quarter = kelly_from_trades(trades, KellySizing(fraction=0.25, min_trades=20))
    assert quarter.exposure == approx(full.exposure * 0.25)


def test_kelly_cap_limits_exposure():
    trades = [0.20, -0.01] * 10  # 高胜率 + 高赔率 → f* 很大
    result = kelly_from_trades(trades, KellySizing(fraction=1.0, cap=0.30, min_trades=20))
    assert result.f_star > 0.30
    assert result.exposure == approx(0.30)


def test_kelly_insufficient_sample_uses_initial_exposure():
    cfg = KellySizing(enabled=True, min_trades=20, initial_exposure=0.6)
    result = kelly_from_trades([0.1, -0.05, 0.2], cfg)
    assert result.ready is False
    assert result.exposure == approx(0.6)
    assert "样本不足" in result.note


def test_kelly_no_edge_gives_zero_exposure():
    trades = [0.05, -0.05] * 10
    result = kelly_from_trades(trades, KellySizing(fraction=0.5, min_trades=20))
    assert result.f_star == approx(0.0)
    assert result.exposure == approx(0.0)
    assert "无统计优势" in result.note


def test_kelly_continuous_mode():
    trades = [0.02, -0.01] * 10
    result = kelly_from_trades(trades, KellySizing(mode="continuous", fraction=0.5, cap=1.0, min_trades=20))
    assert result.mode == "continuous"
    stats = result.stats
    assert result.f_star == approx(kelly_continuous(stats.mean_return, stats.std_return))


# --------------------------------------------------------------------------- #
# 配置校验
# --------------------------------------------------------------------------- #
def test_sizing_config_validation():
    with raises(ConfigError):
        SizingConfig(weighting="magic")
    with raises(ConfigError):
        SizingConfig(max_positions=0)
    with raises(ConfigError):
        SizingConfig(max_weight_per_symbol=0.0)
    with raises(ConfigError):
        SizingConfig(max_weight_per_symbol=1.5)
    with raises(ConfigError):
        SizingConfig(cash_buffer=1.0)
    with raises(ConfigError):
        SizingConfig(lot_size=0)


def test_kelly_config_validation():
    with raises(ConfigError):
        KellySizing(mode="martingale")
    with raises(ConfigError):
        KellySizing(fraction=0.0)
    with raises(ConfigError):
        KellySizing(fraction=1.5)
    with raises(ConfigError):
        KellySizing(cap=0.0)
    with raises(ConfigError):
        KellySizing(initial_exposure=1.5)


def test_sizing_defaults_match_contract():
    cfg = SizingConfig()
    assert cfg.weighting == "equal"
    assert cfg.max_positions == 10
    assert cfg.max_weight_per_symbol == approx(0.10)
    assert cfg.cash_buffer == approx(0.02)
    assert cfg.lot_size == 100
