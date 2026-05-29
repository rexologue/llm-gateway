"""Async Valkey storage helpers for JSON values."""

from __future__ import annotations

from typing import Any, AsyncIterator, TypeAlias

import orjson
import redis.asyncio as redis

JsonValue: TypeAlias = dict[str, Any] | list[Any] | str | int | float | bool | None


class ValkeyJsonStore:
    """Async Valkey storage for JSON-compatible values."""

    def __init__(
        self,
        *,
        api_url: str,
        prefix: str,
        default_ttl_sec: int,
        max_connections: int = 256,
    ) -> None:
        """Initialize JSON storage backed by Valkey."""

        self.prefix = prefix
        self.default_ttl_sec = max(1, int(default_ttl_sec))
        self.pool = redis.ConnectionPool.from_url(
            api_url,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
            health_check_interval=30,
            max_connections=max_connections,
        )
        self.redis = redis.Redis(connection_pool=self.pool)


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


    async def get(self, record_id: str) -> JsonValue | None:
        """Read and parse one JSON value by its unprefixed id."""

        raw = await self.redis.get(self.key(record_id))
        if raw is None:
            return None

        return orjson.loads(raw)


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


    async def touch(self, record_id: str, ttl_sec: int | None = None) -> bool:
        """Refresh TTL for an existing record."""

        effective_ttl = ttl_sec if ttl_sec is not None else self.default_ttl_sec
        return bool(await self.redis.expire(self.key(record_id), effective_ttl))


    async def delete(self, record_id: str) -> bool:
        """Delete one record by id."""

        return bool(await self.redis.delete(self.key(record_id)))


    async def persist(self, record_id: str) -> bool:
        """Remove expiration from one record."""

        return bool(await self.redis.persist(self.key(record_id)))


    async def exists(self, record_id: str) -> bool:
        """Return whether a record exists."""

        return bool(await self.redis.exists(self.key(record_id)))


    async def ttl(self, record_id: str) -> int | None:
        """Return remaining TTL in seconds, or None when absent or persistent."""

        value = await self.redis.ttl(self.key(record_id))
        if value is None or value < 0:
            return None

        return int(value)


    async def count_all(self) -> int:
        """Count all keys in the current logical database."""

        return int(await self.redis.dbsize())


    async def count_matching(self, pattern: str | None = None) -> int:
        """Count keys matching a Valkey pattern."""

        count = 0
        match = pattern or f"{self.prefix}*"
        async for _key in self.redis.scan_iter(match=match, count=1000):
            count += 1

        return count


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
