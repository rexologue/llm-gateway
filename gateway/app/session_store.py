"""Persist chat session messages in Valkey for inspection endpoints."""

from __future__ import annotations

import logging
from typing import Any

import orjson
import redis.asyncio as redis
from redis.exceptions import RedisError

from app.http_utils import utc_now_iso

logger = logging.getLogger(__name__)


class SessionStore:
    """Store the latest observed chat messages for each external session id."""

    def __init__(
        self,
        *,
        api_url: str,
        prefix: str,
        ttl_sec: int,
        max_connections: int,
    ) -> None:
        self.prefix = prefix
        self.ttl_sec = max(1, int(ttl_sec))
        self.pool = redis.ConnectionPool.from_url(
            api_url,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
            health_check_interval=30,
            max_connections=max_connections,
        )
        self.redis = redis.Redis(connection_pool=self.pool)

    def _key(self, session_id: str) -> str:
        return f"{self.prefix}{session_id}"

    def _session_id_from_key(self, key: bytes | str) -> str:
        key_text = key.decode("utf-8") if isinstance(key, bytes) else key
        return key_text.removeprefix(self.prefix)

    async def close(self) -> None:
        """Close the underlying Valkey client."""

        await self.redis.aclose()

    async def save_messages(self, session_id: str | None, messages: Any) -> None:
        """Persist a session's current messages block when it is available."""

        if session_id is None or not isinstance(messages, list):
            return

        record = {
            "session_id": session_id,
            "updated_at": utc_now_iso(),
            "message_cnt": len(messages),
            "messages": messages,
        }

        try:
            await self.redis.set(
                self._key(session_id),
                orjson.dumps(record),
                ex=self.ttl_sec,
            )
        except RedisError as exc:
            logger.warning("Session store save_messages failed: %s", exc)

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Return one stored session record, or None when absent."""

        raw_record = await self.redis.get(self._key(session_id))
        if raw_record is None:
            return None

        record = orjson.loads(raw_record)
        return record if isinstance(record, dict) else None

    async def list_session_ids(self) -> list[str]:
        """Return all stored session ids sorted lexicographically."""

        session_ids: list[str] = []
        cursor = 0
        pattern = f"{self.prefix}*"

        while True:
            cursor, keys = await self.redis.scan(cursor=cursor, match=pattern, count=100)
            session_ids.extend(self._session_id_from_key(key) for key in keys)

            if cursor == 0:
                break

        return sorted(session_ids)
