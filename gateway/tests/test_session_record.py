"""Tests for how exchanges become session blocks and how a session reads back.

Splitting an exchange into blocks and assembling a session from blocks are both
pure, so this suite needs no Valkey. The Valkey commands themselves are covered
by ``test_session_store_valkey.py`` when a test Valkey is available.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import orjson

from app.backend import ChatResponseReader, ChatTurn
from app.session_store import (
    DELIVERY_DRAINED,
    SCHEMA_VERSION,
    SessionExchange,
    assemble_session,
    build_turn,
    compact_message,
    content_hash,
    session_in_period,
    turn_totals,
)
from tests.conftest import delta_event

NOW = datetime(2026, 9, 10, 9, 10, tzinfo=UTC)


def user(text: str) -> dict[str, Any]:
    """Return a user message."""

    return {"role": "user", "content": text}


def assistant(text: str) -> dict[str, Any]:
    """Return an assistant message."""

    return {"role": "assistant", "content": text}


def turn_of(content: str, *, finish: str = "stop", usage: dict[str, Any] | None = None) -> ChatTurn:
    """Return a turn as a non-stream backend answer would produce it."""

    body: dict[str, Any] = {
        "model": "test-model",
        "choices": [
            {"index": 0, "message": assistant(content), "finish_reason": finish}
        ],
    }

    if usage is not None:
        body["usage"] = usage

    return ChatResponseReader.turn_from_body(orjson.dumps(body))


def exchange(
    messages: list[Any],
    *,
    turn: ChatTurn | None = None,
    request_id: str = "req-1",
    **overrides: Any,
) -> SessionExchange:
    """Return an exchange with sensible defaults for the fields under test."""

    fields: dict[str, Any] = {
        "session_id": "s-1",
        "request_id": request_id,
        "messages": messages,
        "started_at": "2026-09-10T09:00:00+00:00",
        "finished_at": "2026-09-10T09:00:02+00:00",
        "model": "test-model",
        "status_code": 200,
        "turn": turn,
    }
    fields.update(overrides)

    return SessionExchange(**fields)


def h(value: Any) -> str:
    """Return the hash a value is stored under."""

    return content_hash(compact_message(value))[0]


def stored(*exchanges: SessionExchange) -> dict[str, Any]:
    """Return the session a reader gets after these exchanges were written."""

    messages: dict[str, Any] = {}
    tools: dict[str, Any] = {}
    turns: list[dict[str, Any]] = []
    meta: dict[str, str] = {}

    for item in exchanges:
        blocks = build_turn(item)
        messages.update({key: orjson.loads(value) for key, value in blocks.messages.items()})
        tools.update({key: orjson.loads(value) for key, value in blocks.tools.items()})
        turns.append(orjson.loads(orjson.dumps(blocks.entry)))
        meta.setdefault("created_at", item.started_at)
        meta["updated_at"] = item.finished_at

        for name, value in turn_totals(item).items():
            meta[name] = str(int(meta.get(name, "0")) + value)

    return assemble_session(
        "s-1",
        meta=meta,
        messages=messages,
        tools=tools,
        turns=turns,
        ttl_sec=600,
        now=NOW,
    )


def test_hash_ignores_key_order_but_not_content() -> None:
    """The same message hashes the same however it was serialized."""

    assert h({"role": "user", "content": "да"}) == h({"content": "да", "role": "user"})
    assert h(user("да")) != h(user("Да"))
    assert h(assistant("a1")) != h({**assistant("a1"), "reasoning_content": "думаю"})


def test_empty_fields_do_not_split_one_message_in_two() -> None:
    """A backend answer full of nulls and its echo without them are one message."""

    from_backend = {
        "role": "assistant",
        "content": "a1",
        "refusal": None,
        "tool_calls": [],
        "reasoning_content": None,
    }
    echoed = {"content": "a1", "role": "assistant"}

    first = build_turn(exchange([user("u1")], turn=ChatResponseReader.turn_from_body(
        orjson.dumps({"choices": [{"index": 0, "message": from_backend, "finish_reason": "stop"}]})
    )))
    second = build_turn(exchange([user("u1"), echoed, user("u2")], turn=turn_of("a2")))

    assert first.entry["output"] == second.entry["input"][1]
    assert orjson.loads(first.messages[first.entry["output"]]) == echoed


def test_turn_references_input_and_output_by_hash() -> None:
    """A turn is the ordered input hashes plus the hash of the answer."""

    history = [user("u1"), assistant("a1"), user("u2")]
    blocks = build_turn(exchange(history, turn=turn_of("a2")))

    assert blocks.entry["input"] == [h(message) for message in history]
    assert blocks.entry["output"] == h(assistant("a2"))
    assert set(blocks.messages) == {h(message) for message in [*history, assistant("a2")]}
    assert blocks.entry["finish_reason"] == "stop"
    assert blocks.entry["tools"] is None
    assert blocks.tools == {}


def test_repeated_messages_are_stored_once_and_referenced_twice() -> None:
    """Deduplication is about storage only; the order lives in the turn."""

    history = [user("да"), assistant("a1"), user("да")]
    blocks = build_turn(exchange(history, turn=turn_of("a2")))

    assert blocks.entry["input"] == [h(user("да")), h(assistant("a1")), h(user("да"))]
    assert len(blocks.messages) == 3


def test_tools_are_stored_once_under_one_hash() -> None:
    """A tools declaration is one block, referenced by every turn that sent it."""

    tools = [{"type": "function", "function": {"name": "get_current_weather"}}]
    first = exchange([user("погода?")], turn=turn_of("a1"), tools=tools)
    second = exchange([user("погода?"), assistant("a1"), user("а завтра?")], turn=turn_of("a2"), tools=tools)

    session = stored(first, second)

    assert list(session["tools"].values()) == [tools]
    assert {turn["tools"] for turn in session["turns"]} == {h(tools)}


def test_failed_exchange_has_input_but_no_output() -> None:
    """A dialog that broke is in the record, with its error and no answer."""

    failed = exchange(
        [user("привет")],
        turn=None,
        status_code=500,
        error={"type": "backend_status", "status_code": 500, "message": "boom"},
    )

    session = stored(failed)
    turn = session["turns"][0]

    assert turn["output"] is None
    assert turn["error"]["status_code"] == 500
    assert list(session["messages"].values()) == [user("привет")]
    assert session["metadata"]["totals"]["failed_requests"] == 1


def test_extra_choices_are_addressable_but_not_the_output() -> None:
    """With n>1 the first choice is the output; the rest stay apart."""

    turn = ChatResponseReader.turn_from_body(
        orjson.dumps(
            {
                "choices": [
                    {"index": 0, "message": assistant("А"), "finish_reason": "stop"},
                    {"index": 1, "message": assistant("Б"), "finish_reason": "length"},
                ]
            }
        )
    )

    blocks = build_turn(exchange([user("u1")], turn=turn))

    assert blocks.entry["output"] == h(assistant("А"))
    assert blocks.entry["extra_outputs"] == [
        {"index": 1, "output": h(assistant("Б")), "finish_reason": "length"}
    ]
    assert h(assistant("Б")) in blocks.messages


def test_truncated_and_delivery_are_recorded() -> None:
    """How a turn ended is part of the audit, not something to infer later."""

    reader = ChatResponseReader()
    reader.feed(delta_event({"content": "полу"}))
    reader.flush()

    blocks = build_turn(exchange([user("u1")], turn=reader.turn(), delivery=DELIVERY_DRAINED))

    assert blocks.entry["truncated"] is True
    assert blocks.entry["delivery"] == DELIVERY_DRAINED


def test_rewritten_system_prompt_adds_one_message_not_a_copy() -> None:
    """The case that duplicated whole dialogs: only the new prompt is new."""

    head = [user("u1"), assistant("a1"), user("u2")]
    first = exchange([{"role": "system", "content": "v1"}, *head], turn=turn_of("a2"))
    second = exchange(
        [{"role": "system", "content": "v2"}, *head, assistant("a2"), user("u3")],
        turn=turn_of("a3"),
        request_id="req-2",
        started_at="2026-09-10T09:01:00+00:00",
    )

    session = stored(first, second)

    assert session["metadata"]["message_cnt"] == 8
    assert session["turns"][1]["input"][1:4] == session["turns"][0]["input"][1:4]


def test_tool_calls_the_client_dropped_stay_in_the_session() -> None:
    """A client that strips handled tool calls cannot erase them."""

    call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "book", "arguments": "{}"}}
        ],
    }
    result = {"role": "tool", "tool_call_id": "c1", "content": "ok"}
    call_turn = ChatResponseReader.turn_from_body(
        orjson.dumps({"choices": [{"index": 0, "message": call, "finish_reason": "tool_calls"}]})
    )

    first = exchange([user("запиши")], turn=call_turn)
    second = exchange([user("запиши"), call, result], turn=turn_of("готово"), request_id="req-2")
    third = exchange(
        [user("запиши"), assistant("готово"), user("спасибо")],
        turn=turn_of("пожалуйста"),
        request_id="req-3",
    )

    session = stored(first, second, third)

    assert h(call) in session["messages"]
    assert h(result) in session["messages"]
    assert session["turns"][0]["output"] == h(call)


def test_turns_read_back_in_start_order() -> None:
    """A drained answer written late still reads where it started."""

    early = exchange([user("u1")], turn=turn_of("late"), request_id="early",
                     started_at="2026-09-10T09:00:00+00:00", finished_at="2026-09-10T09:00:09+00:00")
    later = exchange([user("u1"), user("u2")], turn=turn_of("quick"), request_id="later",
                     started_at="2026-09-10T09:00:01+00:00", finished_at="2026-09-10T09:00:02+00:00")

    session = stored(later, early)

    assert [turn["request_id"] for turn in session["turns"]] == ["early", "later"]
    assert list(session["messages"].values())[:2] == [user("u1"), assistant("late")]


def test_metadata_counts_and_totals() -> None:
    """Counts come from the blocks; totals are a running sum."""

    first = exchange(
        [user("u1")],
        turn=turn_of("a1", usage={"prompt_tokens": 10, "completion_tokens": 3}),
    )
    second = exchange(
        [user("u1"), assistant("a1"), user("u2")],
        turn=turn_of("a2", usage={"prompt_tokens": 20, "completion_tokens": 5}),
        request_id="req-2",
        started_at="2026-09-10T09:05:00+00:00",
        finished_at="2026-09-10T09:05:03+00:00",
    )

    session = stored(first, second)
    metadata = session["metadata"]

    assert session["schema_version"] == SCHEMA_VERSION
    assert metadata["created_at"] == "2026-09-10T09:00:00+00:00"
    assert metadata["updated_at"] == "2026-09-10T09:05:03+00:00"
    assert metadata["age_sec"] == 600
    assert metadata["idle_sec"] == 297
    assert metadata["message_cnt"] == 4
    assert metadata["turn_cnt"] == 2
    assert metadata["totals"] == {
        "requests": 2,
        "failed_requests": 0,
        "prompt_tokens": 30,
        "completion_tokens": 8,
    }


def test_period_filter_keeps_sessions_that_overlap_it() -> None:
    """A session is in a period when its lifetime overlaps it, bounds inclusive."""

    created, updated = "2026-09-10T09:00:00+00:00", "2026-09-10T10:00:00+00:00"

    def within(since: str | None, until: str | None) -> bool:
        return session_in_period(
            created,
            updated,
            since=datetime.fromisoformat(since) if since else None,
            until=datetime.fromisoformat(until) if until else None,
        )

    assert within(None, None)
    assert within("2026-09-10T09:30:00+00:00", "2026-09-10T09:40:00+00:00")
    assert within("2026-09-10T08:00:00+00:00", "2026-09-10T11:00:00+00:00")
    assert within("2026-09-10T10:00:00+00:00", None)
    assert within(None, "2026-09-10T09:00:00+00:00")
    assert not within("2026-09-10T10:00:01+00:00", None)
    assert not within(None, "2026-09-10T08:59:59+00:00")


def test_period_filter_copes_with_missing_timestamps() -> None:
    """One timestamp stands in for the other; none at all only passes unfiltered."""

    since = datetime(2026, 9, 10, 9, 30, tzinfo=UTC)

    assert session_in_period(None, "2026-09-10T10:00:00+00:00", since=since, until=None)
    assert session_in_period("2026-09-10T10:00:00+00:00", None, since=since, until=None)
    assert not session_in_period("2026-09-10T09:00:00+00:00", None, since=since, until=None)
    assert not session_in_period(None, None, since=since, until=None)
    assert not session_in_period("garbage", None, since=since, until=None)
    assert session_in_period(None, None, since=None, until=None)
