"""Persist chat session messages in Valkey for inspection endpoints."""

from __future__ import annotations

import logging
from typing import Any

from redis.exceptions import RedisError

from app.http_utils import utc_now_iso
from app.tools.valkey_store import ValkeyJsonStore

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
        """Initialize the Valkey-backed persisted session store."""

        self.store = ValkeyJsonStore(
            api_url=api_url,
            prefix=prefix,
            default_ttl_sec=ttl_sec,
            max_connections=max_connections,
        )

    async def close(self) -> None:
        """Close the underlying Valkey client."""

        await self.store.close()

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
            await self.store.set(session_id, record)
        except RedisError as exc:
            logger.warning("Session store save_messages failed: %s", exc)

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Return one stored session record, or None when absent."""

        record = await self.store.get(session_id)
        return record if isinstance(record, dict) else None

    async def list_session_ids(self) -> list[str]:
        """Return all stored session ids sorted lexicographically."""

        session_ids: list[str] = []
        async for key in self.store.iter_keys():
            session_ids.append(self.store.record_id_from_key(key))

        return sorted(session_ids)
