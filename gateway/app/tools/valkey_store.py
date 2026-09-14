"""Async Valkey storage helpers for JSON values."""

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
    """Async Valkey storage for JSON-compatible values.

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
        """Initialize JSON storage backed by Valkey."""

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

        A ``WatchError`` is not counted: it means Valkey answered and the record
        changed underneath an optimistic write, which is contention, not an
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


    def record_id_from_key(self, key: bytes | str) -> str:
        """Return the unprefixed record id encoded in a Valkey key."""

        key_text = key.decode("utf-8") if isinstance(key, bytes) else key
        return key_text.removeprefix(self.prefix)


    async def close(self) -> None:
        """Close the underlying Valkey client."""

        await self.redis.aclose()


    @_guarded
    async def get(self, record_id: str) -> JsonValue | None:
        """Read and parse one JSON value by its unprefixed id."""

        raw = await self.redis.get(self.key(record_id))
        if raw is None:
            return None

        return orjson.loads(raw)


    @_guarded
    async def set(
        self,
        record_id: str,
        value: JsonValue,
        *,
        ttl_sec: int | None = None,
        keep_ttl: bool = False,
        no_ttl: bool = False,
    ) -> None:
        """Store a JSON value under the given unprefixed id."""

        if ttl_sec is not None and keep_ttl:
            raise ValueError("ttl_sec and keep_ttl cannot be used together")

        if no_ttl and (ttl_sec is not None or keep_ttl):
            raise ValueError("no_ttl cannot be used with ttl_sec or keep_ttl")

        payload = orjson.dumps(value)
        key = self.key(record_id)

        if keep_ttl:
            await self.redis.set(key, payload, keepttl=True)
        elif no_ttl:
            await self.redis.set(key, payload)
        else:
            effective_ttl = ttl_sec if ttl_sec is not None else self.default_ttl_sec
            await self.redis.set(key, payload, ex=effective_ttl)


    @_guarded
    async def update(
        self,
        record_id: str,
        mutator: Callable[[JsonValue | None], JsonValue],
        *,
        ttl_sec: int | None = None,
        max_attempts: int = 5,
    ) -> bool:
        """Apply a mutator to one JSON value atomically.

        Read-modify-write over a plain GET/SET loses concurrent writes: two
        writers both read the old value and the later SET overwrites the
        earlier one. This runs the mutator inside WATCH/MULTI/EXEC, so a value
        that changed underneath us aborts the transaction and the mutator is
        replayed on the fresh value. The mutator must therefore be free of side
        effects outside the value it returns.
        """

        key = self.key(record_id)
        effective_ttl = ttl_sec if ttl_sec is not None else self.default_ttl_sec

        for _attempt in range(max(1, max_attempts)):
            async with self.redis.pipeline(transaction=True) as pipe:
                await pipe.watch(key)

                raw = await pipe.get(key)
                current = orjson.loads(raw) if raw is not None else None
                updated = mutator(current)

                pipe.multi()
                pipe.set(key, orjson.dumps(updated), ex=effective_ttl)

                try:
                    await pipe.execute()
                    return True

                except WatchError:
                    continue

        return False


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
    async def delete(self, record_id: str) -> bool:
        """Delete one record by id."""

        return bool(await self.redis.delete(self.key(record_id)))


    @_guarded
    async def persist(self, record_id: str) -> bool:
        """Remove expiration from one record."""

        return bool(await self.redis.persist(self.key(record_id)))


    @_guarded
    async def exists(self, record_id: str) -> bool:
        """Return whether a record exists."""

        return bool(await self.redis.exists(self.key(record_id)))


    @_guarded
    async def ttl(self, record_id: str) -> int | None:
        """Return remaining TTL in seconds, or None when absent or persistent."""

        value = await self.redis.ttl(self.key(record_id))
        if value is None or value < 0:
            return None

        return int(value)


    @_guarded
    async def count_all(self) -> int:
        """Count all keys in the current logical database."""

        return int(await self.redis.dbsize())


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


    @_guarded_iter
    async def iter_states(
        self,
        pattern: str | None = None,
        *,
        count: int = 100,
    ) -> AsyncIterator[tuple[bytes | str, JsonValue]]:
        """Iterate over matching keys and parsed JSON values."""

        cursor = 0
        match = pattern or f"{self.prefix}*"

        while True:
            cursor, keys = await self.redis.scan(cursor=cursor, match=match, count=count)
            if keys:
                values = await self.redis.mget(keys)
                for key, raw in zip(keys, values):
                    if raw is not None:
                        yield key, orjson.loads(raw)

            if cursor == 0:
                break


    async def iter_values(
        self,
        pattern: str | None = None,
        *,
        count: int = 100,
    ) -> AsyncIterator[tuple[bytes | str, JsonValue]]:
        """Alias for iter_states."""

        async for item in self.iter_states(pattern=pattern, count=count):
            yield item
