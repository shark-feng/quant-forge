"""回归缺陷 #8：缺少 ``list_date`` 时静默用 ``bar_seq`` 兜底。

原缺陷：``DataStore._listed_days_for`` 在缺 ``list_date`` 时用「该标的第几根 K 线」
代替上市交易日数。若数据从区间中间开始，上市天数被严重低估，
股票池「上市不足 60 日」过滤会**把老股误判为新股剔除**（或反之），且无任何提示。

修复后：
- ``data.listing_date.policy=strict``（默认）：缺 list_date → 直接报错（含修复建议）；
- ``policy=proxy``：允许降级，但必须显式标记 ——
  ``listed_days_source``、质量报告 stats、UniverseStats、回测诊断四处都会披露。
"""

from __future__ import annotations

from tests.compat import raises
from tests.tools import bar_row, dates, make_bars

from aqs.config.schema import DataConfig, construct
from aqs.core.exceptions import DataQualityError
from aqs.data.store import DataStore

D = dates(40)
SYM = "600000.SH"
NEW = "600001.SH"


def market(with_list_date: bool, *, only_recent: bool = False):
    """构造：SYM 正常，NEW 在 D[30] 上市（或完全不提供 list_date）。"""
    rows = []
    for i, day in enumerate(D if not only_recent else D[20:]):
        rows.append(bar_row(day, SYM, close=10.0, amount=1e8, list_date=D[0] if with_list_date else None))
        if i >= 30:
            rows.append(bar_row(day, NEW, close=10.0, amount=1e8, list_date=D[30] if with_list_date else None))
    return make_bars(rows)


def cfg(policy: str, **over):
    base = {"quality": {"strict": False}, "listing_date": {"policy": policy}}
    base.update(over)
    return construct(DataConfig, base)


def make(policy: str, frame, **over):
    return DataStore(frame, config=cfg(policy, **over), universe_config={"mode": "all"}, validate=True)


# --------------------------------------------------------------------------- #
# strict
# --------------------------------------------------------------------------- #
def test_strict_rejects_missing_list_date():
    with raises(DataQualityError) as exc:
        make("strict", market(with_list_date=False))
    message = str(exc.value)
    assert "list_date" in message
    assert "policy=proxy" in message, "报错信息必须给出可行的修复路径"


def test_strict_accepts_complete_list_date():
    store = make("strict", market(with_list_date=True))
    assert store.listed_days_source(SYM) == "list_date"


def test_strict_catches_middle_of_range_data():
    """数据只取了区间后半段（老股会被误判为新股）→ strict 必须报错而不是静默放行。"""
    with raises(DataQualityError):
        make("strict", market(with_list_date=False, only_recent=True))


# --------------------------------------------------------------------------- #
# proxy
# --------------------------------------------------------------------------- #
def test_proxy_marks_source_and_quality_stats():
    store = make("proxy", market(with_list_date=False))
    assert store.listed_days_source(SYM) == "bar_seq_proxy"
    assert store.proxy_listed_symbols, "代理标的必须被记录"
    stats = store.quality.stats
    assert stats["listed_days_proxy_symbols"] >= 1
    assert SYM in stats["listed_days_proxy_sample"]


def test_proxy_listed_days_equals_bar_sequence():
    store = make("proxy", market(with_list_date=False))
    assert store.listed_days(SYM, D[0]) == 1
    assert store.listed_days(SYM, D[9]) == 10


def test_list_date_source_is_exact_when_provided():
    store = make("proxy", market(with_list_date=True))
    assert store.listed_days_source(SYM) == "list_date"
    assert store.listed_days(SYM, D[0]) == 1
    assert store.listed_days(SYM, D[9]) == 10
    assert store.listed_days(NEW, D[30]) == 1
    assert store.listed_days(NEW, D[39]) == 10
    assert store.proxy_listed_symbols == []


def test_list_date_before_window_is_marked_as_approximation():
    """list_date 早于数据起点：按工作日近似并标记 list_date_window（不冒充精确值）。"""
    rows = [bar_row(day, SYM, close=10.0, list_date=D[0] - __import__("datetime").timedelta(days=400)) for day in D]
    store = make("proxy", make_bars(rows))
    # 该 list_date 不在日历内 → 近似口径
    assert store.listed_days_source(SYM) == "list_date_window"
    assert store.listed_days(SYM, D[0]) > 60, "老股不应被当成新股"
    assert store.window_listed_symbols == [SYM]
    assert store.quality.stats["listed_days_window_symbols"] == 1


def test_universe_discloses_proxy_symbols():
    store = make("proxy", market(with_list_date=False), min_list_days=0, liquidity={"window": 5, "min_amount": 0.0})
    stats = store.universe_builder.build_stats(D[-1])
    assert stats.proxy_symbols, "股票池统计必须披露代理口径标的"
    assert stats.as_dict()["proxy_listed_symbols"] >= 1


def test_diagnostics_disclose_proxy_count():
    from tests.tools import make_store

    rows = [bar_row(day, SYM, close=10.0) for day in D]
    store = make_store(make_bars(rows))  # tests 夹具默认 proxy
    assert store.describe()["listed_days_proxy_symbols"] == 1
    assert store.describe()["listing_date_policy"] == "proxy"


def test_quality_report_warns_on_missing_list_date():
    from aqs.data.schema import validate_bars

    report = validate_bars(make_bars([bar_row(day, SYM, close=10.0) for day in D]))
    assert report.stats["missing_list_date_symbols"] == 1
    assert any("list_date" in w for w in report.warnings)


def test_invalid_policy_rejected():
    with raises(Exception):
        construct(DataConfig, {"listing_date": {"policy": "loose"}})
