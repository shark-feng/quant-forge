"""本地数据缓存（M4）：Parquet 优先、参数哈希去重、schema 版本隔离、增量合并、清单。

设计要点（`docs/18_m4_dataprovider.md` §3.2 / §5.1）：

- **按参数哈希去重**：``params_hash`` 覆盖「口径类」参数（复权方式、单位换算版本等），
  **不含日期区间** —— 否则每次取新区间都会整个缓存失效，增量就失去意义；
- **schema 版本隔离**：``version`` 变更后旧缓存**不读也不删**（由人工清理），
  避免新旧 schema 混用造成难以察觉的口径错误；
- **pyarrow 可选**：不可用时自动退化为 CSV 并 warning 一次（**不抛异常**），
  且 ``CacheMeta.fmt`` 如实记录实际落盘格式；
- **过期不等于不可用**：``lookup`` 在 stale 时**仍然返回 frame**，供调用方做增量；
- 写入走「临时文件 + 原子替换」，避免中断留下半截文件被后续误读。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from ..core.logging import get_logger

__all__ = ["CacheMeta", "CacheLookup", "DataCache", "parquet_available"]

logger = get_logger("data.cache")

_MANIFEST = "manifest.json"
_SUPPORTED_FMT = ("parquet", "csv")

#: 排序偏好的日期列（增量合并时用；比「按主键第一列排序」更符合语义）
_DATE_COLUMNS = ("date", "effective_from", "report_period", "announce_date")

#: pyarrow 不可用时只警告一次，避免刷屏
_warned_no_parquet = False


def parquet_available() -> bool:
    """是否具备 Parquet 读写能力（pyarrow 或 fastparquet 任一可用）。"""
    for mod in ("pyarrow", "fastparquet"):
        try:
            __import__(mod)
        except ImportError:
            continue
        return True
    return False


def _now() -> datetime:
    """缓存层「当前时间」：带时区 UTC（与 `provider.utc_now` 同一约定）。"""
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """把**调用方传入**的时间统一为带时区 UTC。

    naive 值按 **UTC** 解释（而不是本机本地时间）：数据层的时间戳约定是 UTC，
    且按本地时间解释会让同一份测试在不同机器上得出不同结论（本项目已多次被
    「机器相关结果」咬到）。历史 manifest 里 naive 的 ``fetched_at`` 是另一回事 ——
    那是旧版本用 ``datetime.now()``（本地时间）写下的，见 :meth:`CacheMeta.fetched_datetime`。
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class CacheMeta:
    """一份缓存样本的元信息（同时写入 manifest.json）。"""

    dataset: str
    key: str
    provider: str = ""
    schema_version: int = 1
    rows: int = 0
    start: str | None = None
    end: str | None = None
    fetched_at: str = ""
    params_hash: str = ""
    fmt: str = "parquet"
    path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "key": self.key,
            "provider": self.provider,
            "schema_version": self.schema_version,
            "rows": self.rows,
            "start": self.start,
            "end": self.end,
            "fetched_at": self.fetched_at,
            "params_hash": self.params_hash,
            "fmt": self.fmt,
            "path": self.path,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "CacheMeta":
        return cls(
            dataset=str(d.get("dataset", "")),
            key=str(d.get("key", "")),
            provider=str(d.get("provider", "")),
            schema_version=int(d.get("schema_version", 1)),
            rows=int(d.get("rows", 0)),
            start=d.get("start"),
            end=d.get("end"),
            fetched_at=str(d.get("fetched_at", "")),
            params_hash=str(d.get("params_hash", "")),
            fmt=str(d.get("fmt", "parquet")),
            path=str(d.get("path", "")),
        )

    @property
    def fetched_datetime(self) -> datetime | None:
        """解析 ``fetched_at``；**历史写入的 naive 值按其本机本地时区解释**并转为 UTC。

        本项目的数据层时间戳统一为**带时区 UTC**（见 `provider.utc_now`）。
        早期版本用 ``datetime.now()``（naive 本地时间）写入，直接当 UTC 会偏移一个时区；
        故对 naive 值调用 ``astimezone(utc)``（Python 语义：naive 视为本地时间）。
        """
        try:
            dt = datetime.fromisoformat(self.fetched_at)
        except (TypeError, ValueError):
            return None
        return dt if dt.tzinfo is not None else dt.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class CacheLookup:
    """一次缓存查询的结果。

    ``hit`` 表示「命中**且新鲜**」；未命中时 ``frame`` 仍可能非空（stale），
    供调用方做增量抓取。
    """

    frame: pd.DataFrame | None = None
    meta: CacheMeta | None = None
    hit: bool = False
    reason: str = "miss_no_file"


class DataCache:
    """按 ``<root>/<dataset>/<key>.<fmt>`` 落盘的本地缓存。"""

    def __init__(
        self,
        root: str | Path,
        *,
        version: int = 1,
        fmt: str = "parquet",
        ttl_hours: float = 24.0,
        provider: str = "",
    ) -> None:
        if fmt not in _SUPPORTED_FMT:
            raise ValueError(f"fmt 只能是 {_SUPPORTED_FMT}，收到 {fmt!r}")
        self.root = Path(root)
        self.version = int(version)
        self.fmt = fmt
        self.ttl_hours = float(ttl_hours)
        self.provider = provider
        self._manifest_cache: dict[str, CacheMeta] | None = None

    # ------------------------------------------------------------------ #
    # 路径与哈希
    # ------------------------------------------------------------------ #
    @staticmethod
    def _safe(key: str) -> str:
        """把 key 里可能出现的路径分隔符替换掉，避免越界写到目录之外。"""
        return str(key).replace("/", "_").replace("\\", "_").replace("..", "_")

    def path_for(self, dataset: str, key: str, *, fmt: str | None = None) -> Path:
        suffix = fmt or self.fmt
        return self.root / self._safe(dataset) / f"{self._safe(key)}.{suffix}"

    def params_hash(self, params: Mapping[str, Any] | None) -> str:
        """口径参数的稳定哈希（**不含日期区间**）。

        用 `sort_keys=True` 保证「同一组参数不同书写顺序」得到同一哈希。
        """
        payload = {"params": dict(params or {}), "schema_version": self.version}
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------------ #
    # manifest
    # ------------------------------------------------------------------ #
    @property
    def manifest_path(self) -> Path:
        return self.root / _MANIFEST

    def manifest(self) -> list[CacheMeta]:
        """返回清单（按 dataset/key 排序）。文件缺失或损坏时返回空清单。"""
        raw = self._read_manifest_raw()
        metas = [CacheMeta.from_dict(v) for v in raw.values()]
        return sorted(metas, key=lambda m: (m.dataset, m.key))

    def _read_manifest_raw(self) -> dict[str, dict[str, Any]]:
        if self._manifest_cache is not None:
            return {k: v.as_dict() for k, v in self._manifest_cache.items()}
        if not self.manifest_path.exists():
            self._manifest_cache = {}
            return {}
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            entries = data.get("entries", {}) if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("缓存清单损坏，按空清单处理：%s（%s）", self.manifest_path, exc)
            entries = {}
        self._manifest_cache = {k: CacheMeta.from_dict(v) for k, v in entries.items()}
        return {k: v.as_dict() for k, v in self._manifest_cache.items()}

    @staticmethod
    def _entry_key(dataset: str, key: str) -> str:
        return f"{dataset}::{key}"

    def _write_manifest(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        entries = {k: v.as_dict() for k, v in (self._manifest_cache or {}).items()}
        payload = {"schema_version": self.version, "entries": entries}
        _atomic_write_text(
            self.manifest_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        )

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #
    def read(self, dataset: str, key: str) -> tuple[pd.DataFrame | None, CacheMeta | None]:
        """按清单读取样本；返回 ``(frame, meta)``，不存在时 ``(None, None)``。"""
        raw = self._read_manifest_raw()
        entry = raw.get(self._entry_key(dataset, key))
        if entry is None:
            return None, None
        meta = CacheMeta.from_dict(entry)
        path = Path(meta.path) if meta.path else self.path_for(dataset, key, fmt=meta.fmt)
        if not path.exists():
            logger.warning("缓存清单存在但样本文件缺失：%s", path)
            return None, meta
        try:
            frame = self._load(path, meta.fmt)
        except Exception as exc:  # noqa: BLE001 - 读取失败按未命中处理，不阻断回测
            logger.warning("缓存读取失败，按未命中处理：%s（%s）", path, exc)
            return None, meta
        return frame, meta

    def lookup(
        self,
        dataset: str,
        key: str,
        *,
        params: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> CacheLookup:
        """命中判定。``hit=True`` 仅当「文件存在 + 版本一致 + 参数哈希一致 + 未过期」。

        ``ttl_hours <= 0`` 表示**不做新鲜度判定**（一律视为命中）—— 需要「立即陈旧」的
        测试请用很小的正数 TTL 并注入 ``now``，不要把 0/负值当成「立刻过期」。
        """
        now = _as_utc(now or _now())
        raw = self._read_manifest_raw()
        entry = raw.get(self._entry_key(dataset, key))
        if entry is None:
            return CacheLookup(None, None, False, "miss_no_file")
        meta = CacheMeta.from_dict(entry)

        if meta.schema_version != self.version:
            return CacheLookup(None, meta, False, "miss_version")
        if meta.params_hash and meta.params_hash != self.params_hash(params):
            return CacheLookup(None, meta, False, "miss_params_hash")

        frame, meta2 = self.read(dataset, key)
        meta = meta2 or meta
        if frame is None:
            return CacheLookup(None, meta, False, "miss_no_file")

        if self.ttl_hours > 0:
            fetched = meta.fetched_datetime
            if fetched is None or now - fetched > timedelta(hours=self.ttl_hours):
                # 过期仍返回 frame：调用方据此做增量，避免重复抓全量
                return CacheLookup(frame, meta, False, "miss_stale")
        return CacheLookup(frame, meta, True, "hit")

    def write(
        self,
        dataset: str,
        key: str,
        frame: pd.DataFrame,
        *,
        params: Mapping[str, Any] | None = None,
        start: Any = None,
        end: Any = None,
        now: datetime | None = None,
    ) -> CacheMeta:
        """写入样本并更新清单；返回实际落盘的元信息。"""
        now = _as_utc(now or _now())
        fmt = self.fmt
        if fmt == "parquet" and not parquet_available():
            fmt = "csv"
            _warn_parquet_once()
        path = self.path_for(dataset, key, fmt=fmt)
        path.parent.mkdir(parents=True, exist_ok=True)
        frame = frame.copy()
        self._dump(frame, path, fmt)

        meta = CacheMeta(
            dataset=dataset,
            key=key,
            provider=self.provider,
            schema_version=self.version,
            rows=int(len(frame)),
            start=str(start) if start is not None else _infer(frame, ("date", "effective_from")),
            end=str(end) if end is not None else _infer_last(frame, ("date", "effective_to")),
            fetched_at=now.isoformat(timespec="seconds"),
            params_hash=self.params_hash(params),
            fmt=fmt,
            path=str(path),
        )
        if self._manifest_cache is None:
            self._read_manifest_raw()
        assert self._manifest_cache is not None
        self._manifest_cache[self._entry_key(dataset, key)] = meta
        self._write_manifest()
        return meta

    @staticmethod
    def merge_incremental(
        cached: pd.DataFrame | None,
        fresh: pd.DataFrame,
        *,
        keys: Sequence[str],
    ) -> pd.DataFrame:
        """增量合并：按主键去重（**保留 fresh**）、排序、重置索引。

        不变量：合并结果行数 **≥ max(len(cached), len(fresh))** ——
        合并只允许「补数据」，不允许丢数据（丢数据会被这条断言拦住）。
        """
        if cached is None or len(cached) == 0:
            out = fresh.copy()
        elif fresh is None or len(fresh) == 0:
            out = cached.copy()
        else:
            combined = pd.concat([cached, fresh], ignore_index=True)
            subset = [c for c in keys if c in combined.columns]
            if not subset:
                raise ValueError(f"merge_incremental 找不到任何主键列：{list(keys)}")
            out = combined.drop_duplicates(subset=subset, keep="last").reset_index(drop=True)

        sort_key = next((c for c in _DATE_COLUMNS if c in out.columns), None)
        if sort_key is None:
            sort_key = next((c for c in keys if c in out.columns), None)
        if sort_key is not None and len(out):
            out = out.sort_values(sort_key, kind="stable").reset_index(drop=True)

        floor = max(len(cached) if cached is not None else 0, len(fresh) if fresh is not None else 0)
        if len(out) < floor:  # pragma: no cover - 防御性断言，正常情况下不可达
            raise AssertionError(
                f"增量合并丢数据：cached={len(cached) if cached is not None else 0}, "
                f"fresh={len(fresh) if fresh is not None else 0}, merged={len(out)}"
            )
        return out

    def clear(self, *, dataset: str | None = None) -> int:
        """删除缓存样本（可只删某个 dataset）并同步清单；返回删除的文件数。"""
        if self._manifest_cache is None:
            self._read_manifest_raw()
        assert self._manifest_cache is not None

        removed = 0
        for entry_key in list(self._manifest_cache):
            meta = self._manifest_cache[entry_key]
            if dataset is not None and meta.dataset != dataset:
                continue
            path = Path(meta.path) if meta.path else self.path_for(meta.dataset, meta.key, fmt=meta.fmt)
            if path.exists():
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:  # pragma: no cover - 权限问题
                    logger.warning("删除缓存文件失败：%s（%s）", path, exc)
            del self._manifest_cache[entry_key]
        self._write_manifest()
        return removed

    # ------------------------------------------------------------------ #
    # 落盘细节
    # ------------------------------------------------------------------ #
    @staticmethod
    def _dump(frame: pd.DataFrame, path: Path, fmt: str) -> None:
        if fmt == "parquet":
            _atomic_write(frame, path, lambda p: frame.to_parquet(p, index=False))
        else:
            _atomic_write(frame, path, lambda p: frame.to_csv(p, index=False, encoding="utf-8-sig"))

    @staticmethod
    def _load(path: Path, fmt: str) -> pd.DataFrame:
        if fmt == "parquet":
            return pd.read_parquet(path)
        return pd.read_csv(path, encoding="utf-8-sig")


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _warn_parquet_once() -> None:
    global _warned_no_parquet
    if not _warned_no_parquet:
        logger.warning(
            "未安装 pyarrow，缓存格式由 parquet 降级为 csv（结果正确但体积更大）。"
            "安装方式：pip install pyarrow"
        )
        _warned_no_parquet = True


def _atomic_write(frame: pd.DataFrame, path: Path, writer: Any) -> None:
    """临时文件 + 原子替换，避免中断留下半截文件。"""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=path.suffix)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        writer(tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():  # pragma: no cover - 正常路径下已被 replace 移走
            tmp.unlink(missing_ok=True)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if tmp.exists():  # pragma: no cover
            tmp.unlink(missing_ok=True)


def _infer(frame: pd.DataFrame, columns: Sequence[str]) -> str | None:
    """推断起始日期；无法解析或全为 NaT 时返回 None。"""
    col = next((c for c in columns if c in frame.columns), None)
    if col is None or not len(frame):
        return None
    series = pd.to_datetime(frame[col], errors="coerce").dropna()
    if not len(series):
        return None
    return str(series.min().date())


def _infer_last(frame: pd.DataFrame, columns: Sequence[str]) -> str | None:
    """推断结束日期；``effective_to`` 为 NaT 表示「仍在生效」，因此取有效值的最大值。"""
    col = next((c for c in columns if c in frame.columns), None)
    if col is None or not len(frame):
        return None
    series = pd.to_datetime(frame[col], errors="coerce").dropna()
    if not len(series):
        return None
    return str(series.max().date())
