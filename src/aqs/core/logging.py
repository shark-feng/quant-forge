"""日志与审计流。

- 库代码统一使用 :func:`get_logger`，禁止 ``print``。
- :class:`AuditStream` 提供结构化审计记录（风控/订单），落盘为 JSONL。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

__all__ = ["get_logger", "setup_logging", "AuditStream", "AuditRecord"]

_LOGGER_NAME = "aqs"
_CONFIGURED = False


def get_logger(name: str | None = None) -> logging.Logger:
    """返回带统一前缀的 logger。"""
    if name is None or name == _LOGGER_NAME:
        return logging.getLogger(_LOGGER_NAME)
    if name.startswith(f"{_LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")


def setup_logging(level: int | str = logging.INFO, *, log_file: str | Path | None = None) -> None:
    """初始化根 logger（重复调用安全）。"""
    global _CONFIGURED
    logger = logging.getLogger(_LOGGER_NAME)
    if not _CONFIGURED:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(handler)
        _CONFIGURED = True
    logger.setLevel(level)
    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(fh)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """一条审计记录（风控决策、订单状态变更等）。"""

    ts: str
    category: str
    action: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {"ts": self.ts, "category": self.category, "action": self.action, "payload": dict(self.payload)},
            ensure_ascii=False,
            default=str,
        )


class AuditStream:
    """内存 + 可选落盘的审计流。

    Args:
        path: JSONL 输出路径；``None`` 表示仅内存保留。
        capacity: 内存中最多保留的记录数（超出丢弃最旧的）。
    """

    def __init__(self, path: str | Path | None = None, *, capacity: int = 100_000) -> None:
        self._path = Path(path) if path is not None else None
        self._capacity = capacity
        self._records: list[AuditRecord] = []
        self._handle = None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self._path.open("a", encoding="utf-8")

    def write(self, record: AuditRecord) -> None:
        self._records.append(record)
        if len(self._records) > self._capacity:
            del self._records[: len(self._records) - self._capacity]
        if self._handle is not None:
            self._handle.write(record.to_json() + "\n")
            self._handle.flush()

    def log(self, category: str, action: str, *, ts: Any = "", **payload: Any) -> AuditRecord:
        """写入一条审计记录。

        ``ts`` 为关键字参数，且 payload 中请不要使用 ``category``/``action``/``ts`` 作为键，
        避免与固定字段冲突。
        """
        record = AuditRecord(ts=str(ts), category=category, action=action, payload=payload)
        self.write(record)
        return record

    @property
    def records(self) -> list[AuditRecord]:
        return list(self._records)

    def filter(self, *, category: str | None = None, action: str | None = None) -> list[AuditRecord]:
        out = self._records
        if category is not None:
            out = [r for r in out if r.category == category]
        if action is not None:
            out = [r for r in out if r.action == action]
        return list(out)

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "AuditStream":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __len__(self) -> int:
        return len(self._records)
