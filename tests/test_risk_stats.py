"""RMS 验收指标测试：拒单率、误杀率、触发延迟。"""

from __future__ import annotations

from tests.compat import approx
from tests.tools import DEFAULT_START, bar_row, dates, make_bars, make_store, price_series

from aqs.core.enums import Side
from aqs.risk.base import RiskTrigger
from aqs.risk.stats import RejectionRecord, evaluate_rejections, summarize_latency

D = dates(12)
SYM = "600000.SH"


def rising_store():
    closes = [10.0 + 0.2 * i for i in range(12)]
    return make_store(make_bars(price_series(SYM, closes)))


def falling_store():
    closes = [12.0 - 0.2 * i for i in range(12)]
    return make_store(make_bars(price_series(SYM, closes)))


def test_evaluate_rejections_counts_missed_gains():
    """上升行情中被拒的买单 → 记为误杀。"""
    store = rising_store()
    records = [
        RejectionRecord("O1", SYM, Side.BUY, 1000, 10.0, D[2], "max_position_per_symbol"),
        RejectionRecord("O2", SYM, Side.BUY, 1000, 10.4, D[3], "max_position_per_symbol"),
    ]
    report = evaluate_rejections(records, store, horizon=5)
    assert report.n_rejections == 2
    assert report.n_evaluated == 2
    assert report.n_false == 2
    assert report.false_reject_rate == approx(1.0)
    assert report.by_rule["max_position_per_symbol"] == 2
    assert report.details[0]["forward_return"] > 0


def test_evaluate_rejections_ignores_correct_blocks():
    """下跌行情中被拒的买单 → 不算误杀（风控做对了）。"""
    store = falling_store()
    records = [RejectionRecord("O1", SYM, Side.BUY, 1000, 12.0, D[0], "gross_exposure")]
    report = evaluate_rejections(records, store, horizon=5)
    assert report.n_evaluated == 1
    assert report.n_false == 0
    assert report.false_reject_rate == approx(0.0)


def test_rejected_sell_in_rising_market_is_not_false_reject():
    """上涨行情中被拒的卖单 → 拦下是对的，不算误杀。"""
    store = rising_store()
    records = [RejectionRecord("O1", SYM, Side.SELL, 1000, 10.0, D[1], "gross_exposure")]
    report = evaluate_rejections(records, store, horizon=5)
    assert report.n_false == 0


def test_rejected_sell_in_falling_market_is_false_reject():
    store = falling_store()
    records = [RejectionRecord("O1", SYM, Side.SELL, 1000, 12.0, D[0], "cancel_ratio")]
    report = evaluate_rejections(records, store, horizon=5)
    assert report.n_false == 1


def test_rejections_without_forward_data_are_skipped():
    store = rising_store()
    records = [RejectionRecord("O1", SYM, Side.BUY, 1000, 10.0, D[11], "rule")]  # 最后一日的订单
    report = evaluate_rejections(records, store, horizon=5)
    assert report.n_rejections == 1
    assert report.n_evaluated == 0
    assert report.false_reject_rate == approx(0.0)


def test_unknown_symbol_is_skipped():
    store = rising_store()
    records = [RejectionRecord("O1", "999999.SH", Side.BUY, 1000, 10.0, D[0], "rule")]
    report = evaluate_rejections(records, store, horizon=5)
    assert report.n_evaluated == 0


def test_rejection_report_dict():
    store = rising_store()
    report = evaluate_rejections([RejectionRecord("O1", SYM, Side.BUY, 100, 10.0, D[0], "r")], store, horizon=3)
    payload = report.as_dict()
    assert set(payload) == {
        "n_rejections",
        "n_evaluated",
        "n_false",
        "false_reject_rate",
        "horizon_days",
        "by_rule",
    }
    assert payload["horizon_days"] == 3


# --------------------------------------------------------------------------- #
# 触发延迟
# --------------------------------------------------------------------------- #
def test_latency_summary_for_order_level_rules():
    triggers = [
        RiskTrigger(rule="r1", action="reject", triggered_on=D[0], acted_on=D[0], latency_days=0),
        RiskTrigger(rule="r2", action="reduce", triggered_on=D[1], acted_on=D[1], latency_days=0),
    ]
    report = summarize_latency(triggers)
    assert report.n_triggers == 2
    assert report.mean_days == approx(0.0)
    assert report.by_action["reject"] == 1


def test_latency_summary_for_control_rules():
    triggers = [
        RiskTrigger(rule="daily_loss_limit", action="pause", triggered_on=D[0], acted_on=D[1], latency_days=1),
        RiskTrigger(rule="max_drawdown_action", action="force_close", triggered_on=D[2], acted_on=D[3], latency_days=1),
    ]
    report = summarize_latency(triggers)
    assert report.mean_days == approx(1.0)
    assert report.max_days == 1
    assert report.by_rule["daily_loss_limit"] == 1
    assert report.n_approx == 0
    assert set(report.as_dict()) == {
        "n_triggers",
        "mean_days",
        "max_days",
        "n_approx",
        "approx_ratio",
        "by_action",
        "by_rule",
    }


def test_latency_empty():
    report = summarize_latency([])
    assert report.n_triggers == 0
    assert report.mean_days == approx(0.0)
    assert report.max_days == 0
