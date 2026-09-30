"""M4-4：限流（令牌桶）与重试（指数退避 + 抖动 + 空结果不重试）。

对应 `docs/18_m4_dataprovider.md` §4.3、§5.2、§5.3 与硬约束 C4。

**测试不真 sleep**：时钟与睡眠函数全部注入假实现，
因此既能断言「等待了多久」，又能让测试瞬间完成。
"""

from __future__ import annotations

from tests.compat import approx, raises

from aqs.data.ratelimit import RateLimiter, resolve_exceptions, retry_call


class FakeClock:
    """可手动推进的假时钟。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSleeper:
    """记录睡眠请求并推进假时钟（模拟「睡过去」）。"""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)

    @property
    def total(self) -> float:
        return sum(self.calls)


def make_limiter(*, rpm: int = 60, burst: int = 2, min_interval_ms: float = 0.0):
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    limiter = RateLimiter(
        requests_per_minute=rpm, burst=burst, min_interval_ms=min_interval_ms,
        clock=clock, sleeper=sleeper,
    )
    return limiter, clock, sleeper


# --------------------------------------------------------------------------- #
# 1. 令牌桶
# --------------------------------------------------------------------------- #
def test_burst_is_immediate_and_then_rate_limited():
    """桶容量内的请求零等待；超出后按速率等待。"""
    limiter, _clock, sleeper = make_limiter(rpm=60, burst=2)  # 1 秒 1 个令牌

    assert limiter.acquire() == approx(0.0)
    assert limiter.acquire() == approx(0.0)
    assert sleeper.calls == [], "桶容量内不应睡眠"

    # 第 3 个请求需要等 1 秒补一个令牌
    waited = limiter.acquire()
    assert waited == approx(1.0)
    assert sleeper.calls == [approx(1.0)]

    stats = limiter.stats()
    assert stats["acquired"] == approx(3.0)
    assert stats["waits"] == approx(1.0)
    assert stats["waited_total"] == approx(1.0)
    assert stats["waited_max"] == approx(1.0)


def test_tokens_refill_over_time_without_sleeping():
    """时间自然流逝即可补令牌：不需要睡眠。"""
    limiter, clock, sleeper = make_limiter(rpm=60, burst=1)
    assert limiter.acquire() == approx(0.0)

    clock.advance(5.0)  # 自然过去 5 秒 → 补满 1 个
    assert limiter.acquire() == approx(0.0)
    assert sleeper.calls == []
    assert limiter.stats()["acquired"] == approx(2.0)


def test_wait_time_and_try_acquire_are_non_blocking():
    limiter, _clock, _sleeper = make_limiter(rpm=120, burst=1)  # 2 秒 1 个

    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False, "令牌不足时 try_acquire 必须立即返回 False"

    assert limiter.wait_time() == approx(0.5)
    assert limiter.wait_time(1) == approx(0.5)

    # 估算不应改变状态
    assert limiter.try_acquire() is False


def test_min_interval_enforces_gap_between_requests():
    limiter, _clock, sleeper = make_limiter(rpm=6000, burst=10, min_interval_ms=100.0)

    assert limiter.acquire() == approx(0.0)
    # 令牌足够，但最小间隔要求再等 100ms
    waited = limiter.acquire()
    assert waited == approx(0.1)
    assert sleeper.total == approx(0.1)


def test_disabled_rate_limit_never_waits():
    """rpm=0 表示不限流：不睡眠、不消耗时间。"""
    limiter, _clock, sleeper = make_limiter(rpm=0, burst=1)

    for _ in range(50):
        assert limiter.acquire() == approx(0.0)
    assert sleeper.calls == []
    stats = limiter.stats()
    assert stats["enabled"] == 0.0
    assert stats["acquired"] == approx(50.0)


def test_invalid_configuration_is_rejected():
    with raises(ValueError):
        RateLimiter(requests_per_minute=60, burst=0)
    with raises(ValueError):
        RateLimiter(requests_per_minute=-1, burst=1)
    with raises(ValueError):
        RateLimiter(requests_per_minute=60, burst=1, min_interval_ms=-1.0)

    limiter, _clock, _sleeper = make_limiter(rpm=60, burst=2)
    with raises(ValueError):
        limiter.acquire(3)  # 超过桶容量
    with raises(ValueError):
        limiter.wait_time(3)


# --------------------------------------------------------------------------- #
# 2. 重试
# --------------------------------------------------------------------------- #
def test_retry_succeeds_after_transient_errors_with_exponential_backoff():
    attempts: list[int] = []
    sleeps: list[float] = []

    def flaky() -> str:
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise ConnectionError("boom")
        return "ok"

    result = retry_call(
        flaky,
        max_attempts=5,
        backoff=2.0,
        jitter=False,               # 关闭抖动以便精确断言
        sleeper=sleeps.append,
    )

    assert result == "ok"
    assert len(attempts) == 3
    # 第 1 次失败后等 2**0 = 1s，第 2 次失败后等 2**1 = 2s
    assert sleeps == [approx(1.0), approx(2.0)]


def test_retry_exhausts_and_reraises_last_error():
    calls: list[int] = []
    sleeps: list[float] = []

    def always_fail() -> None:
        calls.append(1)
        raise TimeoutError("always")

    with raises(TimeoutError):
        retry_call(always_fail, max_attempts=3, backoff=1.5, jitter=False, sleeper=sleeps.append)

    assert len(calls) == 3, "应恰好尝试 max_attempts 次"
    assert len(sleeps) == 2, "最后一次失败后不再睡眠"
    assert sleeps == [approx(1.0), approx(1.5)]


def test_non_retryable_exception_is_raised_immediately():
    """不可重试的异常不能被退避掩盖（否则参数错误会被拖成 5 次超时）。"""
    calls: list[int] = []
    sleeps: list[float] = []

    def wrong_args() -> None:
        calls.append(1)
        raise TypeError("bad argument")

    with raises(TypeError):
        retry_call(
            wrong_args,
            max_attempts=5,
            retry_on=(ConnectionError,),
            sleeper=sleeps.append,
        )
    assert len(calls) == 1
    assert sleeps == []


def test_retry_jitter_stays_within_expected_band():
    """抖动应落在 [0.5, 1.0) × 退避值之间，且不改变「最多 max_attempts 次」的语义。"""
    sleeps: list[float] = []
    rng_values = iter([0.0, 0.5, 0.99])
    calls: list[int] = []

    def always_fail() -> None:
        calls.append(1)
        raise OSError("x")

    with raises(OSError):
        retry_call(
            always_fail,
            max_attempts=4,
            backoff=4.0,
            jitter=True,
            random=lambda: next(rng_values),
            sleeper=sleeps.append,
        )

    assert len(calls) == 4
    # backoff ** (k-1) = 1, 4, 16；抖动因子 0.5, 0.75, 0.995
    assert sleeps[0] == approx(0.5)
    assert sleeps[1] == approx(3.0)
    assert sleeps[2] == approx(15.92)
    for s, base in zip(sleeps, (1.0, 4.0, 16.0)):
        assert 0.5 * base <= s < 1.0 * base


def test_empty_result_is_not_retried_by_default():
    """负面用例：空结果不得触发重试（停牌/退市/无数据重试没有意义）。"""
    calls: list[int] = []
    sleeps: list[float] = []
    empties: list[object] = []

    def empty_fetch() -> list[str]:
        calls.append(1)
        return []

    result = retry_call(
        empty_fetch,
        max_attempts=5,
        sleeper=sleeps.append,
        is_empty=lambda r: len(r) == 0,
        on_empty=empties.append,
    )

    assert result == []
    assert len(calls) == 1, "空结果必须只调用一次"
    assert sleeps == [], "空结果不得睡眠重试"
    assert len(empties) == 1, "空结果应触发一次回调（供记 warning 进质量报告）"


def test_empty_result_can_opt_in_to_retry():
    """显式开启 retry_on_empty 时才重试空结果（用于「空可能是瞬时故障」的场景）。"""
    calls: list[int] = []
    sleeps: list[float] = []
    retried: list[tuple[int, float]] = []

    def empty_then_data() -> list[int]:
        calls.append(1)
        return [] if len(calls) < 3 else [1, 2]

    result = retry_call(
        empty_then_data,
        max_attempts=5,
        backoff=2.0,
        jitter=False,
        sleeper=sleeps.append,
        is_empty=lambda r: len(r) == 0,
        retry_on_empty=True,
        on_retry=lambda attempt, exc, delay: retried.append((attempt, delay)),
    )

    assert result == [1, 2]
    assert len(calls) == 3
    assert sleeps == [approx(1.0), approx(2.0)]
    assert [a for a, _ in retried] == [1, 2]


def test_empty_retry_exhaustion_returns_empty_instead_of_raising():
    """开启空重试后仍为空 → 返回空值（不是异常），由调用方决定如何披露。"""
    calls: list[int] = []
    result = retry_call(
        lambda: (calls.append(1), [])[1],
        max_attempts=3,
        sleeper=lambda _s: None,
        is_empty=lambda r: len(r) == 0,
        retry_on_empty=True,
    )
    assert result == []
    assert len(calls) == 3


def test_retry_rejects_zero_max_attempts():
    with raises(ValueError):
        retry_call(lambda: 1, max_attempts=0)


# --------------------------------------------------------------------------- #
# 3. 配置名字解析
# --------------------------------------------------------------------------- #
def test_resolve_exceptions_maps_names_and_flags_unknown():
    resolved = resolve_exceptions(["ConnectionError", "TimeoutError", "OSError"])
    assert set(resolved) == {ConnectionError, TimeoutError, OSError}

    # 未知名字被跳过，但已有可用项时不会退化为默认集合
    partial = resolve_exceptions(["ConnectionError", "NoSuchError"])
    assert partial == (ConnectionError,)

    # 全部无法解析时退化为默认集合（保证重试仍然生效）
    assert set(resolve_exceptions(["NoSuchError"])) == {ConnectionError, TimeoutError, OSError}
