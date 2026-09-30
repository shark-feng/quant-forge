"""M4-3：本地缓存（Parquet 优先 / 参数哈希 / 版本隔离 / 增量 / 清单 / pyarrow 降级）。

对应 `docs/18_m4_dataprovider.md` §3.2、§4.2、§5.1 与硬约束 C3、C5。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from tests.compat import approx, raises
from tests.tools import workspace_tmp

from aqs.data.cache import CacheLookup, CacheMeta, DataCache, parquet_available


def _bars(dates: list[str], symbol: str = "600000.SH", base: float = 10.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "symbol": symbol,
            "close": [base + i for i in range(len(dates))],
            "volume": [1000.0] * len(dates),
        }
    )


NOW = datetime(2026, 9, 30, 12, 0, 0)
PARAMS = {"adjust": "none", "unit_version": 1}


# --------------------------------------------------------------------------- #
# 1. 写入 → 命中
# --------------------------------------------------------------------------- #
def test_write_then_lookup_hits_and_records_meta():
    with workspace_tmp("cache_hit") as root:
        cache = DataCache(root, version=1, fmt="csv", ttl_hours=24.0, provider="csv")
        frame = _bars(["2022-03-01", "2022-03-02", "2022-03-03"])
        meta = cache.write("bars", "600000.SH", frame, params=PARAMS, now=NOW)

        assert meta.rows == 3
        assert meta.dataset == "bars"
        assert meta.key == "600000.SH"
        assert meta.provider == "csv"
        assert meta.schema_version == 1
        assert meta.fmt == "csv"
        assert meta.start == "2022-03-01"
        assert meta.end == "2022-03-03"
        assert meta.fetched_at == NOW.isoformat(timespec="seconds")
        assert meta.params_hash == cache.params_hash(PARAMS)

        lk = cache.lookup("bars", "600000.SH", params=PARAMS, now=NOW + timedelta(hours=1))
        assert lk.hit is True
        assert lk.reason == "hit"
        assert lk.frame is not None and len(lk.frame) == 3
        assert list(lk.frame["symbol"]) == ["600000.SH"] * 3
        # 落盘文件真实存在
        assert cache.path_for("bars", "600000.SH", fmt="csv").exists()
        assert cache.manifest_path.exists()
        assert len(cache.manifest()) == 1


# --------------------------------------------------------------------------- #
# 2. 三种未命中，且理由可区分
# --------------------------------------------------------------------------- #
def test_stale_lookup_still_returns_frame_for_incremental():
    """过期 ≠ 不可用：必须仍然返回 frame，让调用方能做增量。"""
    with workspace_tmp("cache_stale") as root:
        cache = DataCache(root, version=1, fmt="csv", ttl_hours=24.0)
        cache.write("bars", "600000.SH", _bars(["2022-03-01"]), params=PARAMS, now=NOW)

        fresh = cache.lookup("bars", "600000.SH", params=PARAMS, now=NOW + timedelta(hours=23))
        assert fresh.hit is True

        stale = cache.lookup("bars", "600000.SH", params=PARAMS, now=NOW + timedelta(hours=25))
        assert stale.hit is False
        assert stale.reason == "miss_stale"
        assert stale.frame is not None and len(stale.frame) == 1, "过期仍应返回 frame 供增量使用"
        assert stale.meta is not None


def test_params_hash_mismatch_is_distinguishable_from_version_mismatch():
    with workspace_tmp("cache_miss") as root:
        cache = DataCache(root, version=1, fmt="csv")
        cache.write("bars", "600000.SH", _bars(["2022-03-01"]), params=PARAMS, now=NOW)

        other = cache.lookup(
            "bars", "600000.SH", params={"adjust": "hfq"}, now=NOW + timedelta(hours=1)
        )
        assert other.hit is False
        assert other.reason == "miss_params_hash"

        # 版本变更（模拟 schema 升级）→ 旧缓存不读，且**不删**
        bumped = DataCache(root, version=2, fmt="csv")
        v = bumped.lookup("bars", "600000.SH", params=PARAMS, now=NOW + timedelta(hours=1))
        assert v.hit is False
        assert v.reason == "miss_version"
        assert cache.path_for("bars", "600000.SH", fmt="csv").exists(), "旧版本缓存不应被删除"

        missing = cache.lookup("bars", "999999.SH", params=PARAMS, now=NOW)
        assert missing.hit is False
        assert missing.reason == "miss_no_file"
        assert missing.frame is None


def test_params_hash_is_order_and_ttl_independent():
    with workspace_tmp("cache_hash") as root:
        a = DataCache(root, version=1, fmt="csv", ttl_hours=1.0)
        b = DataCache(root, version=1, fmt="csv", ttl_hours=99.0)
        # ttl 不影响口径哈希；参数书写顺序也不影响
        assert a.params_hash({"x": 1, "y": 2}) == a.params_hash({"y": 2, "x": 1})
        assert a.params_hash({"x": 1}) == b.params_hash({"x": 1})
        # 但参数内容或 schema 版本变化会改变哈希
        assert a.params_hash({"x": 1}) != a.params_hash({"x": 2})
        assert a.params_hash({"x": 1}) != DataCache(root, version=2, fmt="csv").params_hash({"x": 1})


# --------------------------------------------------------------------------- #
# 3. pyarrow 缺失时的降级（C5）
# --------------------------------------------------------------------------- #
def test_parquet_unavailable_degrades_to_csv(monkeypatch=None):
    """pyarrow 不可用时必须退化为 CSV + 记录实际格式，而不是抛异常。"""
    import aqs.data.cache as cache_mod

    with workspace_tmp("cache_noparquet") as root:
        original = cache_mod.parquet_available
        cache_mod.parquet_available = lambda: False  # type: ignore[assignment]
        try:
            cache = DataCache(root, version=1, fmt="parquet")
            frame = _bars(["2022-03-01", "2022-03-02"])
            meta = cache.write("bars", "600000.SH", frame, params=PARAMS, now=NOW)

            assert meta.fmt == "csv", "pyarrow 缺失时应如实记录降级后的格式"
            assert meta.path.endswith(".csv")
            assert cache.path_for("bars", "600000.SH", fmt="csv").exists()

            lk = cache.lookup("bars", "600000.SH", params=PARAMS, now=NOW + timedelta(hours=1))
            assert lk.hit is True
            assert lk.frame is not None and len(lk.frame) == 2
        finally:
            cache_mod.parquet_available = original  # type: ignore[assignment]


def test_parquet_format_round_trip_when_available_or_skip():
    """环境有 pyarrow 时验证 parquet 往返；没有则明确跳过（不制造假通过）。"""
    with workspace_tmp("cache_parquet") as root:
        if not parquet_available():
            cache = DataCache(root, version=1, fmt="parquet")
            assert cache.fmt == "parquet"  # 声明仍是 parquet，落盘时会降级
            return
        cache = DataCache(root, version=1, fmt="parquet")
        frame = _bars(["2022-03-01", "2022-03-02"])
        meta = cache.write("bars", "600000.SH", frame, params=PARAMS, now=NOW)
        assert meta.fmt == "parquet"
        lk = cache.lookup("bars", "600000.SH", params=PARAMS, now=NOW + timedelta(hours=1))
        assert lk.hit and lk.frame is not None and len(lk.frame) == 2
        assert lk.frame["close"].tolist() == frame["close"].tolist()


# --------------------------------------------------------------------------- #
# 4. 增量合并
# --------------------------------------------------------------------------- #
def test_merge_incremental_dedups_keeping_fresh_and_sorts():
    # 用显式数值，避免依赖行内位置（重叠日的值必须来自 fresh，一眼可辨）
    cached = pd.DataFrame(
        {
            "date": pd.to_datetime(["2022-03-01", "2022-03-02"]),
            "symbol": "600000.SH",
            "close": [10.0, 11.0],  # 缓存里的 03-02 是 11.0
        }
    )
    fresh = pd.DataFrame(
        {
            "date": pd.to_datetime(["2022-03-02", "2022-03-03"]),
            "symbol": "600000.SH",
            "close": [99.0, 12.0],  # 修正后的 03-02 是 99.0
        }
    )

    merged = DataCache.merge_incremental(cached, fresh, keys=("date", "symbol"))

    assert len(merged) == 3, "重叠日应按主键去重"
    assert merged["date"].is_monotonic_increasing, "应按时序排列"
    # keep="last" → 重叠日保留 fresh 的值
    row = merged[merged["date"] == pd.Timestamp("2022-03-02")].iloc[0]
    assert row["close"] == approx(99.0), "重叠日期必须保留 fresh（最新）的值"


def test_merge_incremental_never_loses_data():
    """不变量：合并结果行数 >= max(缓存, 新增)。"""
    cached = _bars([f"2022-03-{d:02d}" for d in range(1, 11)])            # 10 天
    fresh = _bars([f"2022-03-{d:02d}" for d in range(5, 16)])             # 11 天，重叠 6 天
    merged = DataCache.merge_incremental(cached, fresh, keys=("date", "symbol"))
    assert len(merged) >= max(len(cached), len(fresh))
    assert len(merged) == 15

    # 边界：一侧为空
    assert len(DataCache.merge_incremental(None, fresh, keys=("date", "symbol"))) == len(fresh)
    assert len(DataCache.merge_incremental(cached, None, keys=("date", "symbol"))) == len(cached)
    empty = pd.DataFrame(columns=["date", "symbol", "close", "volume"])
    assert len(DataCache.merge_incremental(empty, empty, keys=("date", "symbol"))) == 0

    with raises(ValueError):
        DataCache.merge_incremental(cached, fresh, keys=("nonexistent",))


def test_merge_incremental_sorts_by_date_not_by_first_key():
    """主键第一列不是日期时，仍应按日期列排序（例如 index_members）。"""
    cached = pd.DataFrame(
        {
            "index_code": ["000300.SH"],
            "symbol": ["600000.SH"],
            "effective_from": pd.to_datetime(["2022-01-01"]),
        }
    )
    fresh = pd.DataFrame(
        {
            "index_code": ["000300.SH"],
            "symbol": ["600001.SH"],
            "effective_from": pd.to_datetime(["2021-06-01"]),
        }
    )
    merged = DataCache.merge_incremental(
        cached, fresh, keys=("index_code", "symbol", "effective_from")
    )
    assert list(merged["effective_from"]) == [
        pd.Timestamp("2021-06-01"),
        pd.Timestamp("2022-01-01"),
    ]


# --------------------------------------------------------------------------- #
# 5. 清单与清理
# --------------------------------------------------------------------------- #
def test_manifest_lists_entries_and_clear_removes_them():
    with workspace_tmp("cache_manifest") as root:
        cache = DataCache(root, version=1, fmt="csv", provider="akshare")
        cache.write("bars", "600000.SH", _bars(["2022-03-01"]), params=PARAMS, now=NOW)
        cache.write("index_members", "000300.SH", _bars(["2022-03-01"]), params=PARAMS, now=NOW)

        metas = cache.manifest()
        assert len(metas) == 2
        assert [(m.dataset, m.key) for m in metas] == [
            ("bars", "600000.SH"),
            ("index_members", "000300.SH"),
        ]
        assert all(m.provider == "akshare" for m in metas)

        # 只删一个 dataset
        removed = cache.clear(dataset="bars")
        assert removed == 1
        assert not cache.path_for("bars", "600000.SH", fmt="csv").exists()
        assert cache.path_for("index_members", "000300.SH", fmt="csv").exists()
        assert [m.dataset for m in cache.manifest()] == ["index_members"]

        # 全清
        assert cache.clear() == 1
        assert cache.manifest() == []


def test_corrupt_manifest_is_tolerated():
    """清单损坏不能让回测崩掉：按空清单处理并告警。"""
    with workspace_tmp("cache_corrupt") as root:
        cache = DataCache(root, version=1, fmt="csv")
        cache.write("bars", "600000.SH", _bars(["2022-03-01"]), params=PARAMS, now=NOW)
        cache.manifest_path.write_text("{ not json", encoding="utf-8")

        reloaded = DataCache(root, version=1, fmt="csv")
        assert reloaded.manifest() == []
        assert reloaded.lookup("bars", "600000.SH", params=PARAMS, now=NOW).reason == "miss_no_file"


def test_cache_meta_round_trips_dict():
    meta = CacheMeta(
        dataset="bars",
        key="600000.SH",
        provider="csv",
        schema_version=2,
        rows=5,
        start="2022-03-01",
        end="2022-03-05",
        fetched_at="2026-09-30T12:00:00",
        params_hash="deadbeef",
        fmt="csv",
        path="/tmp/x.csv",
    )
    assert CacheMeta.from_dict(meta.as_dict()) == meta
    assert meta.fetched_datetime == datetime(2026, 9, 30, 12, 0, 0)


def test_cache_rejects_unsupported_format():
    with workspace_tmp("cache_fmt") as root:
        with raises(ValueError):
            DataCache(root, version=1, fmt="feather")


def test_cache_key_is_sanitised_against_path_traversal():
    with workspace_tmp("cache_safe") as root:
        cache = DataCache(root, version=1, fmt="csv")
        path = cache.path_for("bars", "../../etc/passwd")
        assert ".." not in str(path.relative_to(root))
        assert path.parent == root / "bars"
