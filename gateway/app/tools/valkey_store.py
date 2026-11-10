"""Async Valkey access guarded by a circuit breaker."""

from __future__ import annotations

import time
from functools import wraps
from typing import Any, AsyncIterator, Callable, TypeAlias

import orjson
import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError, WatchError

JsonValue: TypeAlias = dict[str, Any] | list[Any] | str | int | float | bool | None

SOCKET_CONNECT_TIMEOUT_SEC = 2.0
SOCKET_TIMEOUT_SEC = 2.0


class ValkeyUnavailable(RedisConnectionError):
    """Raised instead of dialing Valkey while the breaker is open.

    It subclasses the driver's own connection error on purpose: every caller
    already degrades on ``RedisError``, so an open breaker needs no new handling
    anywhere - it only makes the same degradation arrive in microseconds.
    """


def _guarded(method: Callable[..., Any]) -> Callable[..., Any]:
    """Run one store coroutine through the breaker."""

    @wraps(method)
    async def wrapper(self: "ValkeyJsonStore", *args: Any, **kwargs: Any) -> Any:
        self._breaker_check()

        try:
            result = await method(self, *args, **kwargs)

        except RedisError as exc:
            self._breaker_record(failed=not isinstance(exc, WatchError))
            raise

        self._breaker_record(failed=False)
        return result

    return wrapper


def _guarded_iter(method: Callable[..., Any]) -> Callable[..., Any]:
    """Run one store async generator through the breaker."""

    @wraps(method)
    async def wrapper(self: "ValkeyJsonStore", *args: Any, **kwargs: Any) -> Any:
        self._breaker_check()

        try:
            async for item in method(self, *args, **kwargs):
                yield item

        except RedisError as exc:
            self._breaker_record(failed=not isinstance(exc, WatchError))
            raise

        self._breaker_record(failed=False)

    return wrapper


class ValkeyJsonStore:
    """Async Valkey access for the gateway's session keys.

    Every call is guarded by a breaker. Without it an unreachable Valkey costs
    ``socket_connect_timeout`` seconds *per call* - and the gateway makes two of
    them on the chat path, one of which runs before the response is returned.
    The gateway would still answer, two seconds late, on every request, for as
    long as the outage lasts. Failing fast after a few confirmed failures turns
    that back into a working gateway that simply records nothing.
    """

    def __init__(
        self,
        *,
        api_url: str,
        prefix: str,
        default_ttl_sec: int,
        max_connections: int = 256,
        breaker_failures: int = 3,
        breaker_cooldown_sec: float = 30.0,
    ) -> None:
        """Initialize Valkey access."""

        self.prefix = prefix
        self.default_ttl_sec = max(1, int(default_ttl_sec))
        self.pool = redis.ConnectionPool.from_url(
            api_url,
            socket_connect_timeout=SOCKET_CONNECT_TIMEOUT_SEC,
            socket_timeout=SOCKET_TIMEOUT_SEC,
            health_check_interval=30,
            max_connections=max_connections,
        )
        self.redis = redis.Redis(connection_pool=self.pool)

        self.breaker_failures = max(1, breaker_failures)
        self.breaker_cooldown_sec = max(0.0, breaker_cooldown_sec)
        self._failures = 0
        self._open_until = 0.0


    @property
    def available(self) -> bool:
        """Return whether calls are currently being attempted at all."""

        return self._failures < self.breaker_failures


    def _breaker_check(self) -> None:
        """Raise without dialing Valkey while the breaker is open."""

        if self.available:
            return

        # One probe is let through once the cooldown has passed: a breaker that
        # never retries would keep a recovered Valkey shut out forever.
        if time.monotonic() >= self._open_until:
            self._failures = self.breaker_failures - 1
            return

        raise ValkeyUnavailable(
            f"Valkey breaker open for prefix {self.prefix!r}; "
            f"{self.breaker_failures} consecutive failures"
        )


    def _breaker_record(self, *, failed: bool) -> None:
        """Count one call outcome, opening the breaker on repeated failure.

        A ``WatchError`` is not counted: it means Valkey answered and a watched
        key changed underneath a transaction, which is contention, not an
        outage.
        """

        if not failed:
            self._failures = 0
            return

        self._failures += 1

        if self._failures >= self.breaker_failures:
            self._open_until = time.monotonic() + self.breaker_cooldown_sec


    def key(self, record_id: str) -> str:
        """Build a fully qualified Valkey key."""

        return f"{self.prefix}{record_id}"


    async def close(self) -> None:
        """Close the underlying Valkey client."""

        await self.redis.aclose()


    @_guarded
    async def execute(
        self,
        build: Callable[[Any], None],
        *,
        transaction: bool = True,
    ) -> list[Any]:
        """Queue commands on one pipeline, send them in one round trip.

        With ``transaction`` the batch runs as MULTI/EXEC, so nothing else is
        interleaved and either every command applies or none does. ``build``
        receives the pipeline and only queues commands on it.
        """

        async with self.redis.pipeline(transaction=transaction) as pipe:
            build(pipe)
            return await pipe.execute()


    @_guarded
    async def get(self, record_id: str) -> JsonValue | None:
        """Read and parse one JSON value by its unprefixed id."""

        raw = await self.redis.get(self.key(record_id))
        if raw is None:
            return None

        return orjson.loads(raw)


    @_guarded
    async def set_if_absent(
        self,
        record_id: str,
        value: JsonValue,
        *,
        ttl_sec: int | None = None,
    ) -> bool:
        """Store a JSON value only when the key does not already exist."""

        effective_ttl = ttl_sec if ttl_sec is not None else self.default_ttl_sec
        payload = orjson.dumps(value)

        return bool(
            await self.redis.set(
                self.key(record_id),
                payload,
                ex=effective_ttl,
                nx=True,
            )
        )


    @_guarded
    async def touch(self, record_id: str, ttl_sec: int | None = None) -> bool:
        """Refresh TTL for an existing record."""

        effective_ttl = ttl_sec if ttl_sec is not None else self.default_ttl_sec
        return bool(await self.redis.expire(self.key(record_id), effective_ttl))


    @_guarded
    async def count_matching(self, pattern: str | None = None) -> int:
        """Count keys matching a Valkey pattern."""

        count = 0
        match = pattern or f"{self.prefix}*"
        async for _key in self.redis.scan_iter(match=match, count=1000):
            count += 1

        return count


    @_guarded_iter
    async def iter_keys(
        self,
        pattern: str | None = None,
        *,
        count: int = 100,
    ) -> AsyncIterator[bytes | str]:
        """Iterate over matching keys using SCAN."""

        cursor = 0
        match = pattern or f"{self.prefix}*"

        while True:
            cursor, keys = await self.redis.scan(cursor=cursor, match=match, count=count)
            for key in keys:
                yield key

            if cursor == 0:
                break
