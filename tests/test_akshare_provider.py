"""M4-8：AKShare provider 的字段映射骨架（**全部离线**，使用 FakeAKShareClient + fixtures）。

设计依据 `docs/11_akshare_provider.md` §4（字段映射）/ §6（缓存、限流、重试、降级）
与 M4-8 确认项 B1~B5 + 三处硬缺口：

- 硬缺口 1：多 symbol 逐标的调用、失败阈值升级、进度审计、合并排序；
- 硬缺口 2：快照累积是**状态**（固定 key、区间闭合、同日幂等），单独测纯函数；
- 硬缺口 3：成交量「手→股」用模块常量（不读配置），并在能力 notes 里披露。

**不联网**：所有样例都是手工构造的 fixture（见 `tests/fixtures/akshare/README.md`）。
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from tests.compat import raises
from tests.fake_akshare import FakeAKShareClient
from tests.tools import workspace_tmp

from aqs.core.exceptions import DataError
from aqs.config.schema import DataConfig
from aqs.data.akshare_provider import (
    ENDPOINTS,
    MAPPERS,
    _DEFAULT_VOLUME_TO_SHARES,
    _SUPPORTED_CAPABILITY_NAMES,
    _accumulate_snapshot,
    AKShareProvider,
    derive_adj_factor,
    normalize_akshare_symbol,
    report_periods_for_range,
)
from aqs.data.cache import DataCache
from aqs.data.provider import DataProvider, ProviderCapabilities, utc_now
from aqs.data.ratelimit import RateLimiter
from aqs.data.registry import PENDING_PROVIDERS, available_providers, build_provider

START = "2022-03-01"
END = "2022-03-07"

#: 固定时刻：所有时间相关行为都通过 provider 的可注入时钟控制，**不依赖真实时间流逝**
T0 = pd.Timestamp("2022-03-15 08:00:00", tz="UTC").to_pydatetime()
T0_PLUS_2D = pd.Timestamp("2022-03-17 08:00:00", tz="UTC").to_pydatetime()


def clock(when: "pd.Timestamp | datetime") -> Any:
    """返回固定时间源（provider 的 ``now`` 参数）。"""
    moment = when if isinstance(when, datetime) else pd.Timestamp(when).to_pydatetime()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return lambda: moment


def build_akshare(tmp, *, client=None, cache=None, config=None, now=None, **kwargs) -> AKShareProvider:
    """构造离线 provider：缓存与时钟都可控，限流不限速，重试不真睡。"""
    return AKShareProvider(
        config if config is not None else DataConfig(provider="akshare"),
        client=client if client is not None else FakeAKShareClient(),
        cache=cache if cache is not None else DataCache(tmp / "cache", provider="akshare"),
        limiter=RateLimiter(requests_per_minute=0),
        sleeper=lambda _seconds: None,
        now=now if now is not None else clock(T0),
        **kwargs,
    )


def make_provider(tmp, *, client=None, **kwargs) -> AKShareProvider:
    """默认构造（固定时钟 T0、缓存不过期）。"""
    return build_akshare(tmp, client=client, **kwargs)


# --------------------------------------------------------------------------- #
# 1~3：注册、端点表自洽、能力与实现一致
# --------------------------------------------------------------------------- #
def test_provider_registered_and_pending_list_cleared():
    assert "akshare" in available_providers()
    assert PENDING_PROVIDERS == (), "M4-8 已接入 akshare，待接入清单必须清空（否则自检形同失效）"
    with workspace_tmp("ak_m8_reg") as tmp:
        provider = build_provider(DataConfig(provider="akshare"), root=tmp)
    assert isinstance(provider, AKShareProvider)
    assert isinstance(provider, DataProvider), "必须满足统一取数协议"


def test_endpoint_table_is_self_consistent_with_fake_client():
    fake = FakeAKShareClient()
    used_fns = {ep.fn for ep in ENDPOINTS.values()}
    # 只比较「akshare 函数替身」：FakeClient 另有记账等辅助方法，不算端点实现
    prefixes = ("stock_", "tool_", "index_")
    implemented = {
        name
        for name in dir(fake)
        if name.startswith(prefixes) and callable(getattr(fake, name))
    }
    assert used_fns <= implemented, f"端点表用到但 FakeClient 未实现：{sorted(used_fns - implemented)}"
    assert used_fns == implemented, f"FakeClient 实现了端点表未用的函数：{sorted(implemented - used_fns)}"

    for key, ep in ENDPOINTS.items():
        assert ep.mapper in MAPPERS, f"{key} 的 mapper {ep.mapper} 不在 MAPPERS"
        assert ep.strategy in ("incremental", "fixed", "snapshot"), f"{key} 策略非法：{ep.strategy}"
        if ep.capability is not None:
            assert ep.capability in _SUPPORTED_CAPABILITY_NAMES, f"{key} 能力名非法：{ep.capability}"

    # capability 名必须都是 ProviderCapabilities 的真实字段（否则声明写错也无人发现）
    fields = set(ProviderCapabilities.__dataclass_fields__)
    assert _SUPPORTED_CAPABILITY_NAMES <= fields, (
        f"能力名与 ProviderCapabilities 字段不一致：{sorted(_SUPPORTED_CAPABILITY_NAMES - fields)}"
    )


def test_declared_capabilities_are_backed_by_working_endpoints():
    with workspace_tmp("ak_m8_caps") as tmp:
        provider = make_provider(tmp)
        caps = provider.capabilities()

        # 声明为 True 的能力，其支撑端点必须真的能取到数据（声明与实现一致）
        supported = {ep.capability for ep in ENDPOINTS.values() if ep.capability}
        for name in ("daily_bars", "adjustment_factors", "listing_dates", "fundamentals", "industry"):
            assert getattr(caps, name) is True, f"{name} 声明为 True"
            assert name in supported, f"{name} 声明为 True 却没有支撑端点"

        # 未实现的必须如实为 False
        for name in ("suspensions", "price_limits", "st_flags", "delistings", "market_cap", "intraday"):
            assert getattr(caps, name) is False, f"{name} 未实现，必须声明 False"

        # B1：只有「当前成分」的接口不得声称历史能力
        assert caps.index_members is False
        notes = " | ".join(caps.notes)
        assert "手→股" in notes, "单位换算必须在能力说明里披露（否则使用者无从得知已换算）"
        assert "当前" in notes and "未经联网探测" in notes


# --------------------------------------------------------------------------- #
# 4~6：bars 映射
# --------------------------------------------------------------------------- #
def test_bars_mapping_converts_volume_from_hands_to_shares():
    with workspace_tmp("ak_m8_bars") as tmp:
        provider = make_provider(tmp)
        bars, prov = provider.fetch_bars(["600000"], START, END)

    assert len(bars) == 5, f"应取到 5 个交易日，实际 {len(bars)}"
    assert prov.rows == len(bars)
    assert prov.source == "akshare"
    # 手 → 股：fixture 首行成交量 1234 手 → 123400 股
    first = bars.loc[bars["date"] == pd.Timestamp("2022-03-01")].iloc[0]
    assert first["volume"] == 1234 * _DEFAULT_VOLUME_TO_SHARES == 123400
    # amount 是元，**不**乘 100
    assert first["amount"] == 1258680.0
    # 量级自检（Q3 的口径）：成交额 ≈ 成交量 × 价格
    implied = first["amount"] / first["volume"]
    assert 9.0 < implied < 11.0, f"量纲不一致：amount/volume={implied}"


def test_bars_mapping_is_canonical_sorted_and_deduped():
    with workspace_tmp("ak_m8_bars2") as tmp:
        provider = make_provider(tmp)
        bars, _ = provider.fetch_bars(["600000"], START, END)

    for column in ("date", "symbol", "open", "high", "low", "close", "volume", "amount", "adj_factor"):
        assert column in bars.columns, f"canonical 行情缺少列：{column}"
    assert set(bars["symbol"]) == {"600000.SH"}, "代码必须归一为带交易所后缀"
    assert bars["date"].is_monotonic_increasing, "合并结果必须按 (date, symbol) 排序"
    assert bars["date"].min() >= pd.Timestamp(START) and bars["date"].max() <= pd.Timestamp(END)
    assert not bars.duplicated(subset=["date", "symbol"]).any(), "主键必须唯一"
    # hfq 口径：adj_factor 首日归一为 1.0，且 03-04 起比值变化体现除权
    assert bars["adj_factor"].iloc[0] == 1.0
    assert bars["adj_factor"].nunique() > 1, "样例含一次除权，归一后仍应出现变化"


def test_symbol_normalisation_rules_and_validation():
    assert normalize_akshare_symbol("600000") == "600000.SH"
    assert normalize_akshare_symbol("000001") == "000001.SZ"
    assert normalize_akshare_symbol("300750") == "300750.SZ"
    assert normalize_akshare_symbol("688981") == "688981.SH"
    assert normalize_akshare_symbol("830799") == "830799.BJ"
    # pandas 读成 float 的尾巴必须剥掉
    assert normalize_akshare_symbol("600000.0") == "600000.SH"
    # 已带后缀的幂等
    assert normalize_akshare_symbol("600000.SH") == "600000.SH"
    assert normalize_akshare_symbol("600000.sh") == "600000.SH"
    # 反例：非法代码必须报错，而不是被宽松兜底成 "abc.SZ"
    for bad in ("abc", "", "12345", "6000000", None):
        with raises(DataError):
            normalize_akshare_symbol(bad)


def test_adj_factor_derivation_boundaries():
    raw = pd.DataFrame(
        {
            "date": pd.to_datetime(["2022-03-01", "2022-03-02", "2022-03-03"]),
            "close": [10.0, 10.0, 10.0],
        }
    )
    # 正例：常数比值 → 归一后全为 1.0（不得出现 NaN 或 0）
    hfq_const = pd.DataFrame({"date": raw["date"], "close_hfq": [20.0, 20.0, 20.0]})
    out = derive_adj_factor(raw, hfq_const)
    assert list(out["adj_factor"]) == [1.0, 1.0, 1.0]

    # 正例：比值在第三天翻倍 → 归一后 [1, 1, 2]
    hfq_step = pd.DataFrame({"date": raw["date"], "close_hfq": [20.0, 20.0, 40.0]})
    assert list(derive_adj_factor(raw, hfq_step)["adj_factor"]) == [1.0, 1.0, 2.0]

    # 反例：没有后复权序列 → 置缺失并给出 warning（**不填 1.0**，填 1.0 等于伪造无复权事件）
    missing = derive_adj_factor(raw, pd.DataFrame(columns=["date", "close_hfq"]))
    assert missing["adj_factor"].isna().all()
    assert missing.attrs["warnings"]

    # 反例：原始收盘非正 → 该行不猜测
    bad_raw = raw.copy()
    bad_raw.loc[1, "close"] = 0.0
    out_bad = derive_adj_factor(bad_raw, hfq_const)
    assert pd.isna(out_bad["adj_factor"].iloc[1])


# --------------------------------------------------------------------------- #
# 7~9：meta / 财务
# --------------------------------------------------------------------------- #
def test_symbol_meta_parses_compact_list_date_and_missing_raises():
    with workspace_tmp("ak_m8_meta") as tmp:
        provider = make_provider(tmp)
        meta, prov = provider.fetch_symbol_meta(["600000"])
    assert len(meta) == 1
    assert meta["symbol"].tolist() == ["600000.SH"]
    assert meta["list_date"].iloc[0] == pd.Timestamp("1999-11-10"), "紧凑写法 19991110 必须被解析"
    assert meta["industry"].iloc[0] == "银行"
    assert prov.rows == 1

    # 反例：缺少上市时间 → 硬错误（缺陷 #8：不得静默兜底）
    with workspace_tmp("ak_m8_meta_bad") as tmp:
        frame = pd.DataFrame({"item": ["股票简称"], "value": ["某某"]})
        provider = make_provider(tmp, client=FakeAKShareClient())
        with raises(DataError) as ctx:
            from aqs.data.akshare_provider import map_symbol_meta

            map_symbol_meta(frame, symbol="600000.SH")
        assert "上市时间" in str(ctx.value) or "list_date" in str(ctx.value)


def test_fundamentals_drop_rows_without_announce_date_and_filter_by_pit():
    # 窄区间：只有公告日在 03 月内的那一条可见（000001 公告于 04-20，属于未来信息）
    with workspace_tmp("ak_m8_fund") as tmp:
        provider = make_provider(tmp)
        narrow, prov_narrow = provider.fetch_fundamentals(["600000", "000001"], "2022-03-01", "2022-03-31")
    assert narrow["symbol"].tolist() == ["600000.SH"], "公告日不在区间内的行必须按 PIT 口径排除"
    assert narrow["announce_date"].notna().all(), "无公告日的行必须被丢弃，不得用报告期顶替"
    assert any("丢弃" in w for w in prov_narrow.warnings), f"丢行必须披露：{prov_narrow.warnings}"

    # 宽区间：两条都在（证明不是「取不到」而是「按公告日过滤」）
    with workspace_tmp("ak_m8_fund_wide") as tmp:
        provider = make_provider(tmp)
        wide, prov = provider.fetch_fundamentals(["600000", "000001"], "2022-03-01", "2022-04-30")
    assert set(wide["symbol"]) == {"600000.SH", "000001.SZ"}
    assert (wide["announce_date"] >= pd.Timestamp("2022-03-01")).all()
    assert (wide["announce_date"] <= pd.Timestamp("2022-04-30")).all()
    # 公告日必须晚于报告期（否则就是财务未来函数）
    assert (wide["announce_date"] > wide["report_period"]).all()
    assert not wide.duplicated(subset=["symbol", "report_period", "announce_date"]).any()
    assert prov.rows == len(wide)


def test_report_periods_lookback_covers_late_announcements():
    # 正例：2021-12-31 的报告期，公告日可能落在 2022-03 → 必须被请求
    periods = report_periods_for_range("2022-03-01", "2022-03-31")
    assert pd.Timestamp("2021-12-31") in periods
    assert pd.Timestamp("2022-03-31") in periods
    # 反例：久远的报告期不得被请求（避免无谓抓取）
    assert pd.Timestamp("2019-03-31") not in periods
    # 区间倒置 → 空列表（边界反例）
    assert report_periods_for_range("2022-03-31", "2022-03-01") == []


# --------------------------------------------------------------------------- #
# 10~11：指数成分（快照累积 = 状态）
# --------------------------------------------------------------------------- #
def test_accumulate_snapshot_closes_intervals_and_is_idempotent():
    day1 = pd.Timestamp("2022-03-01")
    snap1 = pd.DataFrame(
        {
            "index_code": "000300.SH",
            "symbol": ["600000.SH", "000001.SZ"],
            "effective_from": day1,
            "effective_to": pd.NaT,
        }
    )
    # 首次：全部追加
    state, action = _accumulate_snapshot(None, snap1, day1)
    assert action == "appended"
    assert len(state) == 2
    assert state["effective_to"].isna().all()

    # 同日重复抓取：幂等，不重复追加
    same, action_same = _accumulate_snapshot(state, snap1, day1)
    assert action_same == "no_change"
    assert len(same) == 2
    assert not same.duplicated(subset=["index_code", "symbol"]).any()

    # 同日但内容不同：保留首次观测（不覆盖），并如实报告
    changed = snap1.copy()
    changed.loc[0, "symbol"] = "300750.SZ"
    kept, action_diff = _accumulate_snapshot(state, changed, day1)
    assert action_diff == "same_day_ignored"
    assert set(kept["symbol"]) == {"600000.SH", "000001.SZ"}, "同日修订不得覆盖已观测历史"

    # 第二天：600000 退出（区间闭合到最后观测日）、300750 新进、000001 留存
    day2 = pd.Timestamp("2022-03-15")
    snap2 = pd.DataFrame(
        {
            "index_code": "000300.SH",
            "symbol": ["000001.SZ", "300750.SZ"],
            "effective_from": day2,
            "effective_to": pd.NaT,
        }
    )
    state2, action2 = _accumulate_snapshot(state, snap2, day2)
    assert action2 == "appended"
    assert len(state2) == 3
    closed = state2.loc[state2["symbol"] == "600000.SH", "effective_to"].iloc[0]
    assert closed == pd.Timestamp("2022-03-14"), "退出标的闭合到「快照日 − 1 自然日」"
    retained = state2.loc[state2["symbol"] == "000001.SZ", "effective_to"].iloc[0]
    assert pd.isna(retained), "留存标的区间必须继续延伸，不得被截断"
    assert state2.loc[state2["symbol"] == "300750.SZ", "effective_from"].iloc[0] == day2

    # 纯函数：不得修改入参
    assert len(state) == 2 and state["effective_to"].isna().all()


def test_index_members_snapshot_overlap_and_disclosure():
    client = FakeAKShareClient(
        index_snapshots={
            "000300": [
                "index_stock_cons_csindex_000300_20220301.csv",
                "index_stock_cons_csindex_000300_20220315.csv",
            ]
        }
    )
    with workspace_tmp("ak_m8_index") as tmp:
        cache = DataCache(tmp / "cache", provider="akshare")
        provider = AKShareProvider(
            DataConfig(provider="akshare"),
            client=client,
            cache=cache,
            limiter=RateLimiter(requests_per_minute=0),
            now=lambda: pd.Timestamp("2022-03-01", tz="UTC").to_pydatetime(),
        )
        first, first_prov = provider.fetch_index_members("000300.SH", "2022-03-01", "2022-03-31")
        assert set(first["symbol"]) == {"600000.SH", "000001.SZ"}
        assert first["effective_to"].isna().all(), "首次快照全部处于生效中"

        # B1：声明为不可用能力，但**仍返回数据**并附具体警告（不假装没有数据）
        assert provider.capabilities().index_members is False
        text = " | ".join(first_prov.warnings)
        assert "000300.SH" in text and "当前" in text and "幸存者偏差" in text
        assert "2022-03-31" in text, "警告必须写明请求区间"

        # 第二次抓取（次日）：600000 退出 → 区间闭合
        provider._now = lambda: pd.Timestamp("2022-03-15", tz="UTC").to_pydatetime()
        second, _ = provider.fetch_index_members("000300.SH", "2022-03-01", "2022-03-31")
        assert len(second) == 3
        exit_row = second.loc[second["symbol"] == "600000.SH"].iloc[0]
        assert exit_row["effective_to"] == pd.Timestamp("2022-03-14")

        # 区间相交语义（M4-6 同一口径）：跨窗口生效的成分必须保留
        keep, _ = provider.fetch_index_members("000300.SH", "2022-03-10", "2022-03-20")
        assert "600000.SH" in set(keep["symbol"]), "03-01 生效、03-14 结束的成分在 [03-10,03-20] 内必须保留"
        # 反例：窗口完全早于首次快照日 → 空
        early, prov_early = provider.fetch_index_members("000300.SH", "2022-01-01", "2022-02-28")
        assert len(early) == 0 and prov_early.warnings

    # 反例：裸指数代码无法区分交易所，必须报错
    with workspace_tmp("ak_m8_index_bad") as tmp:
        provider = make_provider(tmp)
        with raises(DataError) as ctx:
            provider.fetch_index_members("000300", START, END)
        assert "后缀" in str(ctx.value)


# --------------------------------------------------------------------------- #
# 12~14：缓存 / 限流 / 重试 / 降级
# --------------------------------------------------------------------------- #
def test_cache_hit_avoids_any_client_call():
    client = FakeAKShareClient()
    with workspace_tmp("ak_m8_cache") as tmp:
        provider = make_provider(tmp, client=client)
        # ttl_hours=0 表示不过期，故第二次必须命中
        first, _ = provider.fetch_bars(["600000"], START, END)
        calls_after_first = client.call_count()
        assert calls_after_first > 0
        second, prov = provider.fetch_bars(["600000"], START, END)

    assert client.call_count() == calls_after_first, "命中缓存后不得再发任何请求"
    assert prov.cache_hit is True
    pd.testing.assert_frame_equal(first, second)


def test_incremental_fetch_uses_stale_cache_as_baseline():
    """增量：陈旧缓存作为基线 → 只抓 (缓存末日+1)~end；已覆盖则不抓并披露。"""
    client = FakeAKShareClient()
    with workspace_tmp("ak_m8_incr") as tmp:
        cache_dir = tmp / "cache"
        # T0 写入 03-01~03-03；ttl_hours=1 表示超过 1 小时即陈旧
        cache = DataCache(cache_dir, ttl_hours=1.0, provider="akshare")
        first, _ = build_akshare(tmp, client=client, cache=cache, now=clock(T0)).fetch_bars(
            ["600000"], "2022-03-01", "2022-03-03"
        )
        assert len(first) == 3

        # T0+2天 再读：陈旧 → 以缓存末日为基线做增量
        provider2 = build_akshare(
            tmp, client=client,
            cache=DataCache(cache_dir, ttl_hours=1.0, provider="akshare"),
            now=clock(T0_PLUS_2D),
        )
        before = client.call_count("stock_zh_a_hist")
        merged, prov = provider2.fetch_bars(["600000"], "2022-03-01", "2022-03-07")
        after = client.call_count("stock_zh_a_hist")
        assert len(merged) == 5, "增量合并后必须包含旧数据与新数据（不丢数据）"
        assert not merged.duplicated(subset=["date", "symbol"]).any()
        assert merged["date"].tolist() == sorted(merged["date"].tolist())
        assert prov.cache_hit is False, "发生了真实请求时不得标记为缓存命中"
        assert after > before, "陈旧缓存必须触发增量抓取"
        starts = [kw.get("start_date") for fn, kw in client.calls if fn == "stock_zh_a_hist"]
        assert "20220304" in starts[before:], f"增量起点应为缓存末日+1：{starts[before:]}"

        # 边界反例：缓存已覆盖请求区间且**已陈旧** → 一个请求都不发，并披露「未刷新」
        provider3 = build_akshare(
            tmp, client=client,
            cache=DataCache(cache_dir, ttl_hours=1.0, provider="akshare"),
            now=clock("2022-03-19 08:00:00"),
        )
        calls_before = client.call_count()
        cached, prov3 = provider3.fetch_bars(["600000"], "2022-03-01", "2022-03-03")
        assert client.call_count() == calls_before, "缓存已覆盖区间时不得再发请求"
        assert len(cached) == 5, "第二次增量抓取已把缓存扩到 03-07，故这里是 5 行"
        assert prov3.cache_hit is True
        assert any("未刷新缓存" in w for w in prov3.warnings), f"复用旧缓存必须披露：{prov3.warnings}"


def test_retry_transient_error_and_exhaustion():
    # 正例：前 2 次失败 → 第 3 次成功；sleeper 注入，测试不真睡
    with workspace_tmp("ak_m8_retry") as tmp:
        client = FakeAKShareClient(fail_times=2)
        provider = make_provider(tmp, client=client)
        bars, prov = provider.fetch_bars(["600000"], START, END)
        assert len(bars) == 5
        assert provider._stats["retries"] == 2, "应恰好重试两次"

    # 反例：始终失败 → 重试耗尽后升级为 DataError（消息含端点名与失败原因）
    with workspace_tmp("ak_m8_retry_fail") as tmp:
        client = FakeAKShareClient(fail_symbols=["600000"])
        provider = make_provider(tmp, client=client)
        with raises(DataError) as ctx:
            provider.fetch_bars(["600000"], START, END)
        message = str(ctx.value)
        assert "max_missing_ratio" in message
        assert "stock_zh_a_hist" in message, "升级错误必须带上失败原因（指出是哪个端点）"
        assert client.call_count("stock_zh_a_hist") == 5, "默认最多尝试 5 次（含首次）"


def test_failure_policy_fallback_vs_fail():
    """B3 附加要求：stale 缓存 + 抓取失败时的两种行为必须都明确且有测试。

    注意区间选择：请求区间必须**超出**缓存已覆盖的范围，否则会走「缓存已覆盖 → 不发请求」
    的短路分支（那是正确行为，但测不到失败策略）。
    """
    beyond = "2022-03-10"  # 缓存只到 03-07，故 03-08~03-10 必须真实抓取
    with workspace_tmp("ak_m8_fallback") as tmp:
        cache_dir = tmp / "cache"
        build_akshare(
            tmp, client=FakeAKShareClient(),
            cache=DataCache(cache_dir, ttl_hours=1.0, provider="akshare"), now=clock(T0),
        ).fetch_bars(["600000"], START, END)

        # fallback：陈旧缓存 + 抓取失败 → 退回快照 + cache_hit=True + 说清「哪次抓取、多少行、什么错」
        provider = build_akshare(
            tmp, client=FakeAKShareClient(fail_symbols=["600000"]),
            cache=DataCache(cache_dir, ttl_hours=1.0, provider="akshare"), now=clock(T0_PLUS_2D),
        )
        bars, prov = provider.fetch_bars(["600000"], START, beyond)
        assert len(bars) == 5, "必须退回本地快照，而不是返回空"
        assert prov.cache_hit is True
        assert any("退回本地快照" in w for w in prov.warnings), f"降级必须披露：{prov.warnings}"
        assert any("stock_zh_a_hist" in w for w in prov.warnings)
        assert any("2022-03-07" in w for w in prov.warnings), "披露必须写明快照截至日期"

        # fail：同一场景直接抛错，不静默用旧数据
        strict = build_akshare(
            tmp, client=FakeAKShareClient(fail_symbols=["600000"]),
            cache=DataCache(cache_dir, ttl_hours=1.0, provider="akshare"), now=clock(T0_PLUS_2D),
            config=DataConfig(provider="akshare", failure_policy="fail"),
        )
        with raises(DataError) as ctx:
            strict.fetch_bars(["600000"], START, beyond)
        assert "failure_policy=fail" in str(ctx.value)

        # 无缓存可退 + fallback → 也必须抛（不能凭空造数据）
        missing = build_akshare(
            tmp, client=FakeAKShareClient(fail_symbols=["600000"]),
            cache=DataCache(tmp / "cache_empty", provider="akshare"), now=clock(T0),
        )
        with raises(DataError) as ctx2:
            missing.fetch_bars(["600000"], START, beyond)
        assert "无可用快照" in str(ctx2.value)


def test_max_missing_ratio_escalates_to_error():
    """硬缺口 1：单标的失败只记 warning；超过比例阈值升级为 DataError。"""
    no_sleep = {"sleeper": lambda _seconds: None}
    with workspace_tmp("ak_m8_ratio") as tmp:
        # 阈值放宽到 1.0（允许全失败）→ 返回成功的部分 + 逐条 warning
        lenient = AKShareProvider(
            DataConfig(provider="akshare", max_missing_ratio=1.0),
            client=FakeAKShareClient(fail_symbols=["000001", "300750"]),
            cache=DataCache(tmp / "cache_lenient", provider="akshare"),
            limiter=RateLimiter(requests_per_minute=0), **no_sleep,
        )
        bars, prov = lenient.fetch_bars(["600000", "000001", "300750"], START, END)
        assert set(bars["symbol"]) == {"600000.SH"}, "失败标的被跳过，成功的仍返回"
        assert any("000001.SZ 取数失败" in w for w in prov.warnings), f"失败必须逐条披露：{prov.warnings}"

        # 默认阈值 0.01：2/3 失败 → 升级为错误（不静默返回残缺面板）
        strict = AKShareProvider(
            DataConfig(provider="akshare"),
            client=FakeAKShareClient(fail_symbols=["000001", "300750"]),
            cache=DataCache(tmp / "cache_strict", provider="akshare"),
            limiter=RateLimiter(requests_per_minute=0), **no_sleep,
        )
        with raises(DataError) as ctx:
            strict.fetch_bars(["600000", "000001", "300750"], START, END)
        message = str(ctx.value)
        assert "max_missing_ratio" in message
        assert "000001.SZ" in message and "300750.SZ" in message

        # 边界正例：失败比例恰好等于阈值 → 不升级（比较是「超过」而不是「达到」）
        boundary = AKShareProvider(
            DataConfig(provider="akshare", max_missing_ratio=0.5),
            client=FakeAKShareClient(fail_symbols=["000001"]),
            cache=DataCache(tmp / "cache_boundary", provider="akshare"),
            limiter=RateLimiter(requests_per_minute=0), **no_sleep,
        )
        partial, _ = boundary.fetch_bars(["600000", "000001"], START, END)
        assert set(partial["symbol"]) == {"600000.SH"}, "恰好等于阈值时不应升级为错误"


def test_large_universe_writes_progress_audit():
    """硬缺口 1：标的数 > 50 时每 10 个写一条审计（内存 + 落盘）。"""
    symbols = [f"{600000 + i:06d}" for i in range(60)]

    class ManyClient(FakeAKShareClient):
        def stock_zh_a_hist(self, **kwargs):  # noqa: D102 - 替身
            self._record("stock_zh_a_hist", kwargs)
            frame = self._read("stock_zh_a_hist_600000_raw.csv")
            return frame.assign(股票代码=kwargs["symbol"])

    with workspace_tmp("ak_m8_audit") as tmp:
        provider = make_provider(tmp, client=ManyClient())
        provider.fetch_bars(symbols, START, END)

        records = provider.audit_records
        assert len(records) == 6, f"60 个标的应每 10 个记一条（共 6 条），实际 {len(records)}"
        assert records[0]["done"] == 10 and records[0]["total"] == 60
        assert records[-1]["done"] == 60

        log = provider._audit_path
        assert log is not None and log.exists(), "审计必须在缓存启用时落盘"
        lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
        assert len(lines) == 6 and lines[0]["endpoint"] == "bars"

    # 反例：标的数不多时不写审计（避免噪声）
    with workspace_tmp("ak_m8_noaudit") as tmp:
        provider = make_provider(tmp)
        provider.fetch_bars(["600000"], START, END)
        assert provider.audit_records == []


def test_calendar_is_sorted_deduped_and_in_range():
    with workspace_tmp("ak_m8_cal") as tmp:
        provider = make_provider(tmp)
        days, prov = provider.fetch_trading_calendar("2022-03-01", "2022-03-08")

    assert days == sorted(set(days)), "交易日历必须升序去重"
    assert all(pd.Timestamp(d) >= pd.Timestamp("2022-03-01") for d in days)
    assert all(pd.Timestamp(d) <= pd.Timestamp("2022-03-08") for d in days)
    assert prov.rows == len(days)
    assert prov.symbols == 0


def test_industry_snapshot_is_accumulated_and_filtered():
    with workspace_tmp("ak_m8_industry") as tmp:
        provider = make_provider(tmp)
        frame, prov = provider.fetch_industry(["600000", "300750"])

    assert set(frame["symbol"]) == {"600000.SH", "300750.SZ"}
    assert frame["industry"].notna().all()
    assert frame["effective_from"].notna().all()
    assert any("快照累积" in w for w in prov.warnings), "行业快照语义必须披露"


def test_health_check_does_not_probe_network():
    with workspace_tmp("ak_m8_health") as tmp:
        provider = make_provider(tmp, client=FakeAKShareClient())
        health = provider.health_check()
    assert health.ok is True
    assert health.details["network_probe"] is False, "健康检查刻意不联网，必须如实标注"
    assert health.checked_at.tzinfo is not None, "时间戳统一为带时区 UTC"

    # 未装/未传 client：必须是可执行的报错（含安装提示），且不影响其它 provider
    with workspace_tmp("ak_m8_health2") as tmp:
        bare = AKShareProvider(DataConfig(provider="akshare"), cache=DataCache(tmp / "c", provider="akshare"))
        if importlib.util.find_spec("akshare") is None:
            health2 = bare.health_check()
            assert health2.ok is False
            assert any("pip install akshare" in e for e in health2.errors)
            with raises(DataError) as ctx:
                bare.fetch_bars(["600000"], START, END)
            assert "pip install akshare" in str(ctx.value)
        else:  # pragma: no cover - 取决于环境
            assert bare.health_check().ok is True


def test_provenance_timestamps_are_timezone_aware():
    with workspace_tmp("ak_m8_ts") as tmp:
        provider = make_provider(tmp)
        _, prov = provider.fetch_bars(["600000"], START, END)
    assert prov.fetched_at.tzinfo is not None
    assert prov.fetched_at == provider._now()
    # 缓存层也必须能比较（naive 历史记录与 aware 当前时间混用时不得抛 TypeError）
    assert (utc_now() - prov.fetched_at).total_seconds() >= 0


def test_fixtures_are_documented_as_synthetic_samples():
    from tests.fake_akshare import FIXTURES

    readme = FIXTURES / "README.md"
    assert readme.exists(), "fixture 目录必须有来源说明"
    text = readme.read_text(encoding="utf-8")
    assert "手工构造" in text and "不是" in text, "必须明确声明样例不是真实抓取数据"
    assert "probe_akshare" in text, "必须指向探测脚本（真实字段名由它固化）"
    assert len(list(FIXTURES.glob("*.csv"))) >= 9, "样例文件数量与文档清单需一致"
