"""M4-5：数据质量检查 Q1~Q12。

对应 `docs/18_m4_dataprovider.md` §4.4 与 `docs/11_akshare_provider.md` §7。

重点覆盖两项本项目最在意的陷阱，且**都有反例**：
- Q3 成交量「手 vs 股」量级校验；
- Q12 幸存者偏差自检。
"""

from __future__ import annotations

import pandas as pd

from tests.compat import approx

from aqs.data.quality import (
    QUALITY_TITLES,
    ProviderQualityReport,
    QualityChecker,
    QualityFinding,
    QualityThresholds,
)


def bars(rows: list[dict]) -> pd.DataFrame:
    """构造 canonical 行情表（缺列会自动补默认值，便于单点构造反例）。"""
    base = {
        "date": pd.Timestamp("2022-03-01"),
        "symbol": "600000.SH",
        "open": 10.0,
        "high": 10.5,
        "low": 9.5,
        "close": 10.0,
        "volume": 1000.0,
        "amount": 10_000.0,   # volume × close = 10,000 → 比值 1.0（单位正确）
        "adj_factor": 1.0,
        "is_suspended": False,
    }
    out = []
    for i, row in enumerate(rows):
        merged = dict(base)
        merged.update(row)
        if "date" not in row:
            merged["date"] = base["date"] + pd.Timedelta(days=i)
        out.append(merged)
    return pd.DataFrame(out)


def codes(report: ProviderQualityReport) -> list[str]:
    return report.codes()


# --------------------------------------------------------------------------- #
# Q1 / Q2
# --------------------------------------------------------------------------- #
def test_q1_duplicate_primary_key_is_error():
    frame = bars([{}, {"date": pd.Timestamp("2022-03-01")}])  # 同一 (date, symbol) 两条
    report = QualityChecker().check_primary_key(frame)
    assert codes(report) == ["Q1"]
    finding = report.findings[0]
    assert finding.level == "error"
    assert finding.count == 2
    assert report.ok is False
    assert report.exit_code() == 2
    assert finding.samples, "应给出样本行便于定位"


def test_q2_ohlc_violations_are_errors():
    frame = bars(
        [
            {},                                              # 正常
            {"high": 9.0},                                   # high < low
            {"high": 10.0, "open": 11.0, "close": 11.0},     # high 未覆盖 open/close
            {"close": 0.0},                                  # 非正价格
        ]
    )
    report = QualityChecker().check_ohlc(frame)
    assert codes(report) == ["Q2"]
    assert report.findings[0].count == 3
    assert report.findings[0].level == "error"


# --------------------------------------------------------------------------- #
# Q3：手 → 股（核心陷阱，含正反例）
# --------------------------------------------------------------------------- #
def test_q3_detects_volume_in_lots_instead_of_shares():
    """成交量以「手」给出时，amount/(volume×close) ≈ 100 → 必须报错并给出修复提示。"""
    frame = bars([{"volume": 1000.0, "close": 10.0, "amount": 1_000_000.0}])  # 比值 100
    report = QualityChecker().check_volume_unit(frame)

    assert codes(report) == ["Q3"]
    finding = report.findings[0]
    assert finding.level == "error"
    assert "手" in finding.message
    assert "volume_to_shares" in finding.message
    assert report.stats["amount_ratio_median"] == approx(100.0)


def test_q3_accepts_correct_share_units():
    """单位正确（股/元）时不应报错 —— 这是防「检查器自己误报」的反例。"""
    frame = bars([{ "volume": 1000.0, "close": 10.0, "amount": 10_000.0}])  # 比值 1.0
    report = QualityChecker().check_volume_unit(frame)
    assert report.findings == []
    assert report.stats["amount_ratio_median"] == approx(1.0)


def test_q3_detects_amount_in_thousands():
    """amount 以「千元」给出时比值 ≈ 0.001 → 同样报错（偏低分支）。"""
    frame = bars([{"volume": 1000.0, "close": 10.0, "amount": 10.0}])
    report = QualityChecker().check_volume_unit(frame)
    assert codes(report) == ["Q3"]
    assert "偏低" in report.findings[0].message


def test_q3_skips_when_columns_missing_or_no_valid_rows():
    report = QualityChecker().check_volume_unit(bars([{"volume": 0.0}]))
    assert report.findings == [], "无有效行时不应报错"
    assert report.stats.get("amount_ratio_median") is None

    partial = bars([{}]).drop(columns=["amount"])
    assert QualityChecker().check_volume_unit(partial).findings == []


# --------------------------------------------------------------------------- #
# Q4 ~ Q7
# --------------------------------------------------------------------------- #
def test_q4_non_positive_factor_is_error_and_jump_is_warning():
    frame = bars(
        [
            {"date": pd.Timestamp("2022-03-01"), "adj_factor": 1.0},
            {"date": pd.Timestamp("2022-03-02"), "adj_factor": 0.0},   # 非正 → error
            {"date": pd.Timestamp("2022-03-03"), "adj_factor": 1.0},
            {"date": pd.Timestamp("2022-03-04"), "adj_factor": 2.0},   # Δln2≈0.69 > 0.30 → warning
        ]
    )
    report = QualityChecker().check_adjustment_factors(frame)
    levels = {f.code: f.level for f in report.findings}
    assert report.stats["adj_factor_invalid"] == 1
    assert report.stats["adj_factor_jumps"] >= 1
    assert levels["Q4"] in ("error", "warning")
    assert len(report.findings) == 2, "非正与跳变是两条独立 finding"


def test_q5_and_q6_detect_limit_band_and_suspension_mismatch():
    checker = QualityChecker()

    # (a) 上下限颠倒：注意它同时也会触发「收盘越界」（把 limit_up 当上限时 close 必然越界），
    #     因此这里只断言 reversed 计数，避免与下面的越界用例互相污染。
    inverted = bars([{"limit_up": 9.0, "limit_down": 11.0, "close": 10.0}])
    q5_inv = checker.check_price_limits(inverted)
    assert q5_inv.stats["limit_inverted"] == 1
    assert {f.level for f in q5_inv.findings} == {"warning"}

    # (b) 收盘价越出正常区间
    out_of_band = bars([{"limit_up": 11.0, "limit_down": 9.0, "close": 12.0}])
    q5_band = checker.check_price_limits(out_of_band)
    assert q5_band.stats["limit_inverted"] == 0
    assert q5_band.stats["close_out_of_limit"] == 1
    assert q5_band.findings[0].level == "warning"

    # (c) 停牌与成交量不一致
    mismatch = bars([{"is_suspended": True, "volume": 1000.0}])
    q6 = checker.check_suspension_consistency(mismatch)
    assert q6.stats["suspension_mismatch"] == 1
    assert q6.findings[0].level == "warning"

    # (d) 正常数据：三者都不应报
    clean = bars([{"limit_up": 11.0, "limit_down": 9.0, "close": 10.0}])
    assert checker.check_price_limits(clean).findings == []
    assert checker.check_suspension_consistency(clean).findings == []


def test_q7_listing_and_delisting_consistency():
    meta = pd.DataFrame(
        {
            "symbol": ["600000.SH"],
            "list_date": [pd.Timestamp("2022-03-02")],
            "delist_date": [pd.Timestamp("2022-03-03")],
        }
    )
    frame = bars(
        [
            {"date": pd.Timestamp("2022-03-01")},   # 早于上市日 → 违规
            {"date": pd.Timestamp("2022-03-02")},   # 边界内
            {"date": pd.Timestamp("2022-03-04")},   # 晚于退市日 → 违规
        ]
    )
    report = QualityChecker().check_listing_consistency(frame, symbol_meta=meta)
    assert codes(report) == ["Q7"]
    assert report.findings[0].count == 2
    assert report.findings[0].level == "error"


# --------------------------------------------------------------------------- #
# Q8 ~ Q12
# --------------------------------------------------------------------------- #
def test_q8_calendar_coverage_gap_is_warned():
    sessions = pd.bdate_range("2022-03-01", periods=10)
    # 标的 A 覆盖全部 10 天；标的 B 只有 3 天 → 缺口 70% > 5%
    rows = [{"date": d, "symbol": "600000.SH"} for d in sessions]
    rows += [{"date": d, "symbol": "600001.SH"} for d in sessions[:3]]
    report = QualityChecker().check_calendar_coverage(bars(rows), calendar=list(sessions))

    assert codes(report) == ["Q8"]
    assert report.stats["coverage_expected_sessions"] == 10
    assert report.stats["coverage_gap_symbols"] == 1
    assert report.findings[0].level == "warning"


def test_q9_cross_table_missing_meta_is_error():
    meta = pd.DataFrame({"symbol": ["600000.SH"]})
    frame = bars([{}, {"symbol": "600001.SH"}])
    members = pd.DataFrame({"symbol": ["600002.SH"], "index_code": "000300.SH"})
    report = QualityChecker().check_cross_tables(frame, symbol_meta=meta, members=members)

    assert codes(report) == ["Q9"]
    assert report.findings[0].level == "error"
    assert report.stats["symbols_missing_meta_from_bars"] == 1
    assert report.stats["symbols_missing_meta_from_members"] == 1


def test_q10_missing_or_inverted_announce_date_is_error():
    missing_col = pd.DataFrame({"symbol": ["600000.SH"], "report_period": [pd.Timestamp("2022-03-31")]})
    report = QualityChecker().check_fundamentals(missing_col)
    assert codes(report) == ["Q10"]
    assert "announce_date" in report.findings[0].message
    assert report.findings[0].level == "error"

    inverted = pd.DataFrame(
        {
            "symbol": ["600000.SH", "600001.SH"],
            "report_period": [pd.Timestamp("2022-03-31")] * 2,
            "announce_date": [pd.Timestamp("2022-03-01"), pd.Timestamp("2022-04-30")],
        }
    )
    report2 = QualityChecker().check_fundamentals(inverted)
    assert report2.stats["announce_date_before_report_period"] == 1
    assert report2.findings[0].level == "error"


def test_q11_non_positive_financials_are_warned():
    frame = pd.DataFrame(
        {
            "symbol": ["600000.SH", "600001.SH"],
            "report_period": [pd.Timestamp("2022-03-31")] * 2,
            "announce_date": [pd.Timestamp("2022-04-30")] * 2,
            "total_assets": [100.0, -5.0],
            "net_assets": [50.0, 0.0],
        }
    )
    report = QualityChecker().check_financial_non_negative(frame)
    assert codes(report) == ["Q11"]
    assert report.findings[0].level == "warning"
    assert report.stats["non_positive_total_assets"] == 1
    assert report.stats["non_positive_net_assets"] == 1


def test_q12_survivorship_self_check_flags_all_live_universe():
    """核心陷阱：候选池里一只退市标的都没有 → 必须告警（可能系统性高估收益）。"""
    live_only = pd.DataFrame(
        {
            "symbol": ["600000.SH", "600001.SH"],
            "list_date": [pd.Timestamp("2010-01-01")] * 2,
            "delist_date": [pd.NaT, pd.NaT],
        }
    )
    report = QualityChecker().check_survivorship(
        symbol_meta=live_only, candidates=["600000.SH", "600001.SH"]
    )
    assert codes(report) == ["Q12"]
    assert report.findings[0].level == "warning"
    assert "幸存者偏差" in report.findings[0].message
    assert report.stats["survivorship_delisted"] == 0


def test_q12_passes_when_delisted_symbol_present():
    """反例：候选池含退市标的 → 不应告警。"""
    with_delisted = pd.DataFrame(
        {
            "symbol": ["600000.SH", "600002.SH"],
            "list_date": [pd.Timestamp("2010-01-01")] * 2,
            "delist_date": [pd.NaT, pd.Timestamp("2021-06-01")],
        }
    )
    report = QualityChecker().check_survivorship(
        symbol_meta=with_delisted, candidates=["600000.SH", "600002.SH"]
    )
    assert report.findings == []
    assert report.stats["survivorship_delisted"] == 1

    # 空候选池：无法自检 → 告警（不能假装通过）
    empty = QualityChecker().check_survivorship(symbol_meta=with_delisted, candidates=[])
    assert codes(empty) == ["Q12"]


# --------------------------------------------------------------------------- #
# 报告结构与组合入口
# --------------------------------------------------------------------------- #
def test_report_serialisation_markdown_and_exit_code():
    report = ProviderQualityReport(stats={"bars_rows": 100})
    report.add(QualityFinding("Q3", "error", "单位异常", count=3, samples=[{"symbol": "600000.SH"}]))
    report.add(QualityFinding("Q8", "warning", "覆盖缺口", count=1))

    assert report.ok is False
    assert len(report.errors()) == 1
    assert len(report.warnings()) == 1
    assert report.exit_code() == 2

    d = report.to_dict()
    assert d["ok"] is False
    assert d["errors"] == 1
    assert d["warnings"] == 1
    assert d["exit_code"] == 2
    assert d["findings"][0]["title"] == QUALITY_TITLES["Q3"]
    assert d["findings"][0]["samples"] == [{"symbol": "600000.SH"}]

    md = report.to_markdown()
    assert "# 数据质量报告" in md
    assert "Q3" in md and "单位异常" in md
    assert "| `bars_rows` | 100 |" in md


def test_clean_data_produces_no_findings_and_zero_exit_code():
    """全部检查在同一份干净数据上必须零 finding（防检查器误报）。"""
    session_days = pd.bdate_range("2022-03-01", periods=4)
    rows = []
    for d in session_days:
        rows.append({"date": d, "symbol": "600000.SH"})
        rows.append({"date": d, "symbol": "600001.SH"})
    frame = bars(rows)
    meta = pd.DataFrame(
        {
            "symbol": ["600000.SH", "600001.SH"],
            "list_date": [pd.Timestamp("2010-01-01")] * 2,
            # 退市日设在行情窗口的最后一天：既满足 Q12（存在退市标的），
            # 又不触发 Q7（行情没有晚于退市日）
            "delist_date": [pd.NaT, pd.Timestamp("2022-03-04")],
        }
    )
    report = QualityChecker().run_all(
        bars=frame,
        symbol_meta=meta,
        candidates=["600000.SH", "600001.SH"],
        calendar=list(session_days),
    )
    assert report.findings == [], f"干净数据不应有 finding：{[f.as_dict() for f in report.findings]}"
    assert report.ok is True
    assert report.exit_code() == 0
    assert report.stats["bars_rows"] == 8
    assert report.stats["bars_symbols"] == 2


def test_run_all_merges_findings_from_multiple_checks():
    frame = bars([{}, {"date": pd.Timestamp("2022-03-01")}])   # 触发 Q1
    frame.loc[0, "volume"] = 1000.0
    frame.loc[0, "amount"] = 1_000_000.0                        # 触发 Q3
    report = QualityChecker().run_all(bars=frame)
    assert "Q1" in report.codes()
    assert "Q3" in report.codes()
    assert report.exit_code() == 2


def test_thresholds_are_configurable():
    frame = bars([{"volume": 1000.0, "close": 10.0, "amount": 15_000.0}])  # 比值 1.5
    strict = QualityChecker(thresholds=QualityThresholds(amount_ratio_high=1.2))
    loose = QualityChecker(thresholds=QualityThresholds(amount_ratio_high=2.0))
    assert codes(strict.check_volume_unit(frame)) == ["Q3"]
    assert loose.check_volume_unit(frame).findings == []

    # 跳变阈值同样可配
    frame2 = bars(
        [
            {"date": pd.Timestamp("2022-03-01"), "adj_factor": 1.0},
            {"date": pd.Timestamp("2022-03-02"), "adj_factor": 1.1},   # Δln≈0.095
        ]
    )
    assert QualityChecker(thresholds=QualityThresholds(adj_jump=0.05)).check_adjustment_factors(
        frame2
    ).findings != []
    assert QualityChecker(thresholds=QualityThresholds(adj_jump=0.5)).check_adjustment_factors(
        frame2
    ).findings == []


def test_checks_do_not_mutate_input():
    frame = bars([{}, {}])
    meta = pd.DataFrame({"symbol": ["600000.SH"], "list_date": [pd.Timestamp("2010-01-01")]})
    before = frame.copy(deep=True)
    before_meta = meta.copy(deep=True)

    checker = QualityChecker()
    checker.run_all(bars=frame, symbol_meta=meta, candidates=["600000.SH"], calendar=None)

    pd.testing.assert_frame_equal(frame, before)
    pd.testing.assert_frame_equal(meta, before_meta)
