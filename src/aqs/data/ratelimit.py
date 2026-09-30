"""限流与重试（M4）。

`docs/18_m4_dataprovider.md` §4.3 / §5.2 / §5.3。

**可测试性是本模块的设计前提**：时钟与睡眠函数全部可注入，
因此单元测试用「假时钟 + 假 sleeper」断言「等了多久、重试几次」，
既不真的 sleep，也不依赖墙钟时间。

两条容易踩错的语义，这里显式固定：

1. **空结果不重试**：返回 ``[]``/空 DataFrame 往往是「停牌 / 退市 / 当日无数据」，
   重试既无意义又白耗配额 → 由 ``is_empty`` 判定后**立即返回**，
   由调用方转成 warning 进质量报告；
2. **不可重试异常直接抛**：例如参数错误（``TypeError``）不该被退避重试掩盖。
"""

from __future__ import annotations

import random as _random
import time
from typing import Any, Callable, Sequence, TypeVar

from ..core.logging import get_logger

__all__ = ["RateLimiter", "retry_call", "resolve_exceptions"]

logger = get_logger("data.ratelimit")

T = TypeVar("T")

#: 配置里写异常类名，这里解析为实际类型（配置层保持纯数据，不 import 网络异常）
_DEFAULT_RETRY_NAMES = ("ConnectionError", "TimeoutError", "OSError")
#: 这些内置名可直接解析；其余按「常见网络库异常名」尽力匹配
_BUILTIN_EXC: dict[str, type[BaseException]] = {
    "ConnectionError": ConnectionError,
    "TimeoutError": TimeoutError,
    "OSError": OSError,
    "IOError": OSError,
    "ValueError": ValueError,
    "RuntimeError": RuntimeError,
    "KeyError": KeyError,
}


def resolve_exceptions(names: Sequence[str]) -> tuple[type[BaseException], ...]:
    """把配置中的异常类名解析为异常类型。

    解析不到的类名**不会**被静默忽略：记一条 warning 并跳过，
    避免「以为在重试、其实没重试」。
    """
    out: list[type[BaseException]] = []
    for name in names:
        exc = _BUILTIN_EXC.get(name)
        if exc is None:
            logger.warning("重试配置里的异常类名无法解析，已跳过：%s", name)
            continue
        out.append(exc)
    return tuple(out) or (ConnectionError, TimeoutError, OSError)


class RateLimiter:
    """令牌桶限流器。

    Args:
        requests_per_minute: 每分钟允许的请求数；``<= 0`` 表示**不限流**。
        burst: 桶容量（允许的瞬时突发请求数）。
        min_interval_ms: 两次请求之间的最小间隔（毫秒），0 表示不限制。
        clock: 时间源（默认 ``time.monotonic``）；测试注入假时钟。
        sleeper: 睡眠函数（默认 ``time.sleep``）；测试注入假 sleeper。
    """

    def __init__(
        self,
        *,
        requests_per_minute: int = 300,
        burst: int = 10,
        min_interval_ms: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if burst < 1:
            raise ValueError(f"burst 至少为 1，收到 {burst}")
        if requests_per_minute < 0:
            raise ValueError(f"requests_per_minute 不能为负，收到 {requests_per_minute}")
        if min_interval_ms < 0:
            raise ValueError(f"min_interval_ms 不能为负，收到 {min_interval_ms}")

        self.requests_per_minute = int(requests_per_minute)
        self.burst = int(burst)
        self.min_interval_ms = float(min_interval_ms)
        self._clock = clock
        self._sleeper = sleeper

        self._capacity = float(burst)
        self._tokens = float(burst)
        self._last_refill = clock()
        self._last_request_at: float | None = None

        self._acquired = 0
        self._waits = 0
        self._waited_total = 0.0
        self._waited_max = 0.0

    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return self.requests_per_minute > 0

    @property
    def _rate_per_second(self) -> float:
        return self.requests_per_minute / 60.0

    def _refill(self) -> None:
        now = self._clock()
        if self.enabled:
            elapsed = max(now - self._last_refill, 0.0)
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_second)
        self._last_refill = now

    def wait_time(self, n: int = 1) -> float:
        """在不睡眠的前提下，估算拿到 ``n`` 个令牌需要等多久（秒）。"""
        if not self.enabled:
            return 0.0
        if n > self.burst:
            raise ValueError(f"一次请求 {n} 个令牌超过桶容量 {self.burst}，配置不合理")
        self._refill()
        need = max(0.0, n - self._tokens)
        wait = need / self._rate_per_second if need > 0 else 0.0
        if self.min_interval_ms > 0 and self._last_request_at is not None:
            gap = self.min_interval_ms / 1000.0 - (self._clock() - self._last_request_at)
            wait = max(wait, gap)
        return max(wait, 0.0)

    def try_acquire(self, n: int = 1) -> bool:
        """非阻塞获取；拿不到立即返回 False（不睡眠）。"""
        if n > self.burst:
            raise ValueError(f"一次请求 {n} 个令牌超过桶容量 {self.burst}，配置不合理")
        if not self.enabled:
            self._acquired += n
            self._last_request_at = self._clock()
            return True
        self._refill()
        if self._tokens < n:
            return False
        self._tokens -= n
        self._acquired += n
        self._last_request_at = self._clock()
        return True

    def acquire(self, n: int = 1) -> float:
        """阻塞式获取 ``n`` 个令牌；返回本次实际等待的秒数（0 表示无需等待）。"""
        if n > self.burst:
            raise ValueError(f"一次请求 {n} 个令牌超过桶容量 {self.burst}，配置不合理")
        if not self.enabled:
            self._acquired += n
            self._last_request_at = self._clock()
            return 0.0

        waited = 0.0
        while True:
            self._refill()
            need = max(0.0, n - self._tokens)
            delay = need / self._rate_per_second if need > 0 else 0.0
            if self.min_interval_ms > 0 and self._last_request_at is not None:
                gap = self.min_interval_ms / 1000.0 - (self._clock() - self._last_request_at)
                delay = max(delay, gap)
            if delay <= 0:
                break
            self._sleeper(delay)
            waited += delay

        self._tokens -= n
        self._acquired += n
        self._last_request_at = self._clock()
        if waited > 0:
            self._waits += 1
            self._waited_total += waited
            self._waited_max = max(self._waited_max, waited)
        return waited

    def stats(self) -> dict[str, float]:
        """已获取令牌数 / 等待次数 / 累计与最大等待时长（秒）。"""
        return {
            "enabled": 1.0 if self.enabled else 0.0,
            "requests_per_minute": float(self.requests_per_minute),
            "burst": float(self.burst),
            "acquired": float(self._acquired),
            "waits": float(self._waits),
            "waited_total": round(self._waited_total, 6),
            "waited_max": round(self._waited_max, 6),
            "tokens": round(self._tokens, 6),
        }


class _EmptyResult(Exception):
    """内部哨兵：仅用于把「空结果重试」事件喂给 ``on_retry`` 回调，不外抛。"""


def retry_call(
    fn: Callable[[], T],
    *,
    max_attempts: int = 5,
    backoff: float = 1.5,
    jitter: bool = True,
    retry_on: Sequence[type[BaseException]] = (ConnectionError, TimeoutError, OSError),
    sleeper: Callable[[float], None] = time.sleep,
    random: Callable[[], float] = _random.random,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    is_empty: Callable[[T], bool] | None = None,
    retry_on_empty: bool = False,
    on_empty: Callable[[T], None] | None = None,
) -> T:
    """带指数退避与抖动的重试。

    Args:
        fn: 无参可调用（把参数用 lambda 绑好）。
        max_attempts: 最多尝试次数（含首次）。
        backoff: 指数退避底数，第 ``k`` 次失败后等待 ``backoff**(k-1)`` 秒。
        jitter: 是否加抖动（乘上 ``[0.5, 1.0)`` 的随机因子），避免同刻重试风暴。
        retry_on: 需要重试的异常类型；**其它异常直接抛出**。
        sleeper / random: 睡眠与随机源（测试注入，避免真 sleep）。
        on_retry: 每次重试前的回调 ``(尝试序号, 异常, 本次等待秒数)``，用于写审计日志。
        is_empty: 判定「空结果」的谓词（默认 None＝不判定）。
        retry_on_empty: 空结果是否也重试。**默认 False**：空结果往往意味着
            「停牌 / 退市 / 当日无数据」，重试无意义且白耗配额，应转成 warning 进质量报告。
            置 True 仅在「空结果确实可能是瞬时故障」的场景使用。
        on_empty: 判定为空时的回调（用于记录 warning），仅在 ``retry_on_empty=False`` 时触发一次。

    Returns:
        ``fn()`` 的返回值（含「不重试的空结果」）。

    Raises:
        最后一次尝试的异常（重试耗尽时）。
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts 至少为 1，收到 {max_attempts}")

    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = fn()
        except retry_on as exc:  # type: ignore[misc]
            last_exc = exc
            if attempt >= max_attempts:
                break
            delay = float(backoff) ** (attempt - 1)
            if jitter:
                delay *= 0.5 + random() * 0.5
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleeper(delay)
            continue

        # ---- 成功返回：先判定空结果 ----
        if is_empty is not None and is_empty(result):
            if not retry_on_empty:
                if on_empty is not None:
                    on_empty(result)
                return result
            if attempt >= max_attempts:
                return result
            delay = float(backoff) ** (attempt - 1)
            if jitter:
                delay *= 0.5 + random() * 0.5
            if on_retry is not None:
                on_retry(attempt, _EmptyResult(), delay)
            sleeper(delay)
            continue
        return result

    assert last_exc is not None
    raise last_exc
