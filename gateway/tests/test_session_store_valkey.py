"""Session store round trips against a real Valkey.

These run only when ``GATEWAY_TEST_VALKEY_URL`` points at a disposable Valkey,
for example ``redis://127.0.0.1:6390/15``. Every test uses its own key prefix
and removes its keys afterwards, but the database should still be one nothing
else uses.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from typing import Any, Coroutine, TypeVar

import orjson
import pytest
import redis.asyncio as redis

from app.backend import ChatResponseReader
from app.session_store import SessionExchange, SessionStore, content_hash

T = TypeVar("T")

VALKEY_URL = os.getenv("GATEWAY_TEST_VALKEY_URL", "")

pytestmark = pytest.mark.skipif(
    not VALKEY_URL,
    reason="GATEWAY_TEST_VALKEY_URL is not set: no disposable Valkey to test against",
)


def run(coro: Coroutine[Any, Any, T]) -> T:
    """Run one coroutine to completion."""

    return asyncio.run(coro)


def answer(content: str) -> Any:
    """Return a backend turn that answers with ``content``."""

    return ChatResponseReader.turn_from_body(
        orjson.dumps(
            {
                "model": "test-model",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2},
            }
        )
    )


def exchange(session_id: str, messages: list[Any], content: str, **overrides: Any) -> SessionExchange:
    """Return one successful exchange."""

    fields: dict[str, Any] = {
        "session_id": session_id,
        "request_id": uuid.uuid4().hex,
        "messages": messages,
        "started_at": "2026-09-10T09:00:00+00:00",
        "finished_at": "2026-09-10T09:00:01+00:00",
        "status_code": 200,
        "turn": answer(content),
    }
    fields.update(overrides)

    return SessionExchange(**fields)


@pytest.fixture()
def prefix() -> Any:
    """Yield a key prefix unique to one test, and delete its keys afterwards."""

    value = f"test:{uuid.uuid4().hex}:"
    yield value

    async def cleanup() -> None:
        client = redis.Redis.from_url(VALKEY_URL)
        keys = [key async for key in client.scan_iter(match=f"{value}*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()

    run(cleanup())


def make_store(prefix: str) -> SessionStore:
    """Return a session store on the test Valkey."""

    return SessionStore(api_url=VALKEY_URL, prefix=prefix, ttl_sec=600, max_connections=4)


def test_written_turns_read_back_as_one_session(prefix: str) -> None:
    """Two exchanges become one session with shared messages."""

    store = make_store(prefix)
    system = {"role": "system", "content": "ты помощник"}
    tools = [{"type": "function", "function": {"name": "f"}}]

    async def scenario() -> tuple[Any, Any, Any]:
        first = await store.record_exchange(
            exchange("s1", [system, {"role": "user", "content": "u1"}], "a1", tools=tools)
        )
        second = await store.record_exchange(
            exchange(
                "s1",
                [system, {"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"},
                 {"role": "user", "content": "u2"}],
                "a2",
                tools=tools,
                started_at="2026-09-10T09:01:00+00:00",
                finished_at="2026-09-10T09:01:01+00:00",
            )
        )
        session = await store.get_session("s1")
        await store.close()
        return first, second, session

    first, second, session = run(scenario())

    assert first.saved and first.turn_cnt == 1
    assert second.saved and second.turn_cnt == 2
    assert session["metadata"]["message_cnt"] == 5
    assert session["metadata"]["tools_cnt"] == 1
    assert session["metadata"]["turn_cnt"] == 2
    assert session["metadata"]["totals"]["prompt_tokens"] == 14
    assert session["metadata"]["created_at"] == "2026-09-10T09:00:00+00:00"
    assert 0 < session["metadata"]["expires_in_sec"] <= 600
    assert session["messages"][session["turns"][1]["output"]]["content"] == "a2"
    assert session["turns"][0]["tools"] == content_hash(tools)[0]


def test_concurrent_writes_keep_every_turn(prefix: str) -> None:
    """Writes never read, so racing requests cannot drop each other."""

    store = make_store(prefix)

    async def scenario() -> Any:
        await asyncio.gather(
            *(
                store.record_exchange(exchange("race", [{"role": "user", "content": f"u{index}"}], f"a{index}"))
                for index in range(20)
            )
        )
        session = await store.get_session("race")
        await store.close()
        return session

    session = run(scenario())

    assert session["metadata"]["turn_cnt"] == 20
    assert session["metadata"]["totals"]["requests"] == 20
    assert session["metadata"]["message_cnt"] == 40


def test_session_list_and_missing_session(prefix: str) -> None:
    """The list finds every session; an unknown id reads as absent."""

    store = make_store(prefix)

    async def scenario() -> tuple[Any, Any]:
        for session_id in ("one", "two"):
            await store.record_exchange(exchange(session_id, [{"role": "user", "content": "u"}], "a"))

        listing = await store.list_sessions()
        missing = await store.get_session("nope")
        await store.close()
        return listing, missing

    listing, missing = run(scenario())

    assert {entry["session_id"] for entry in listing} == {"one", "two"}
    assert all(entry["turn_cnt"] == 1 and entry["message_cnt"] == 2 for entry in listing)
    assert missing is None


def test_session_list_filters_by_period(prefix: str) -> None:
    """Only sessions whose lifetime overlaps the period are listed."""

    store = make_store(prefix)
    spans = {
        "morning": ("2026-09-10T09:00:00+00:00", "2026-09-10T09:10:00+00:00"),
        "noon": ("2026-09-10T12:00:00+00:00", "2026-09-10T12:30:00+00:00"),
        "evening": ("2026-09-10T18:00:00+00:00", "2026-09-10T18:05:00+00:00"),
    }

    def at(hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 9, 10, hour, minute, tzinfo=UTC)

    async def scenario() -> dict[str, set[str]]:
        for session_id, (started, finished) in spans.items():
            await store.record_exchange(
                exchange(session_id, [{"role": "user", "content": "u"}], "a",
                         started_at=started, finished_at=finished)
            )

        windows = {
            "all": {},
            "midday": {"since": at(9, 5), "until": at(12, 15)},
            "after_noon": {"since": at(12, 31)},
            "before_noon": {"until": at(11)},
            "night": {"since": at(20), "until": at(23)},
        }
        found = {
            name: {entry["session_id"] for entry in await store.list_sessions(**bounds)}
            for name, bounds in windows.items()
        }
        await store.close()
        return found

    found = run(scenario())

    assert found["all"] == set(spans)
    assert found["midday"] == {"morning", "noon"}
    assert found["after_noon"] == {"evening"}
    assert found["before_noon"] == {"morning"}
    assert found["night"] == set()


def test_legacy_record_is_served_untouched(prefix: str) -> None:
    """A pre-schema-3 record under the bare key reads as it was stored."""

    store = make_store(prefix)
    legacy = {"schema_version": 2, "metadata": {"session_id": "old"}, "messages": [], "turns": []}

    async def scenario() -> Any:
        client = redis.Redis.from_url(VALKEY_URL)
        await client.set(f"{prefix}old", orjson.dumps(legacy), ex=60)
        await client.aclose()
        session = await store.get_session("old")
        await store.close()
        return session

    assert run(scenario()) == legacy
