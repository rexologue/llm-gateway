"""Tests for reconciling a client's message history with the stored transcript.

The named cases are the ones the design was approved against. The rest guard
the fingerprint: if a stored assistant turn stops matching the client's own
copy of it, the merge sees a divergence that is not there and archives a tail
it should have kept.
"""

from __future__ import annotations

from typing import Any

from app.session_store import SessionStore


def user(text: str) -> dict[str, Any]:
    """Return a user message."""

    return {"role": "user", "content": text}


def assistant(text: str) -> dict[str, Any]:
    """Return an assistant message."""

    return {"role": "assistant", "content": text}


def merge(stored: list[Any], incoming: list[Any]):
    """Merge two histories."""

    return SessionStore.merge_messages(stored, incoming)


def contents(messages: list[Any]) -> list[Any]:
    """Return the content of each message, for compact assertions."""

    return [message.get("content") for message in messages]


def test_seeds_an_empty_transcript() -> None:
    """The first request of a session is stored as it came."""

    result = merge([], [user("u1")])

    assert contents(result.messages) == ["u1"]
    assert result.appended_cnt == 1
    assert result.dropped == []


def test_appends_the_next_turn() -> None:
    """A client resending the whole history only appends what is new."""

    result = merge([user("u1"), assistant("a1")], [user("u1"), assistant("a1"), user("u2")])

    assert contents(result.messages) == ["u1", "a1", "u2"]
    assert result.appended_cnt == 1
    assert result.dropped == []


def test_keeps_the_head_a_sliding_window_dropped() -> None:
    """A client trimming its context must not trim the recorded dialog."""

    stored = [user("u1"), assistant("a1"), user("u2"), assistant("a2")]
    result = merge(stored, [user("u2"), assistant("a2"), user("u3")])

    assert contents(result.messages) == ["u1", "a1", "u2", "a2", "u3"]
    assert result.appended_cnt == 1
    assert result.dropped == []


def test_regeneration_archives_the_superseded_tail() -> None:
    """A rewritten turn replaces the tail, and the old tail is handed back."""

    stored = [user("u1"), assistant("a1"), user("u2"), assistant("a2")]
    result = merge(stored, [user("u1"), assistant("a1"), user("u2-fixed")])

    assert contents(result.messages) == ["u1", "a1", "u2-fixed"]
    assert result.appended_cnt == 1
    assert contents(result.dropped) == ["u2", "a2"]


def test_repeating_the_same_request_changes_nothing() -> None:
    """A retry of an identical request must not duplicate the dialog."""

    stored = [user("u1"), assistant("a1"), user("u2")]
    result = merge(stored, [user("u1"), assistant("a1"), user("u2")])

    assert contents(result.messages) == ["u1", "a1", "u2"]
    assert result.appended_cnt == 0
    assert result.dropped == []


def test_tool_result_lands_after_the_call_that_produced_it() -> None:
    """A tool result arriving on the next request keeps its position."""

    call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_current_weather", "arguments": '{"city":"Москва"}'},
            }
        ],
    }
    result_message = {"role": "tool", "tool_call_id": "call_1", "content": "-3°C"}
    stored = [user("u1"), call]

    result = merge(stored, [user("u1"), call, result_message])

    assert [message["role"] for message in result.messages] == ["user", "assistant", "tool"]
    assert result.messages[2] is result_message
    assert result.dropped == []


def test_an_unrelated_history_is_appended_not_substituted() -> None:
    """A session id reused by a different dialog must not erase the first one."""

    result = merge([user("u1"), assistant("a1")], [user("other")])

    assert contents(result.messages) == ["u1", "a1", "other"]
    assert result.dropped == []


def test_reasoning_content_does_not_break_alignment() -> None:
    """A stored reasoning turn still matches the client's copy without it."""

    stored_turn = {"role": "assistant", "content": "a1", "reasoning_content": "думаю"}
    result = merge([user("u1"), stored_turn], [user("u1"), assistant("a1"), user("u2")])

    assert contents(result.messages) == ["u1", "a1", "u2"]
    assert result.dropped == []
    # The stored turn keeps its reasoning: alignment must not rewrite history.
    assert result.messages[1]["reasoning_content"] == "думаю"


def test_missing_content_key_matches_explicit_null() -> None:
    """``content: null`` and an absent content key are the same turn."""

    stored_call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}],
    }
    echoed_call = {
        "role": "assistant",
        "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}],
    }

    result = merge([user("u1"), stored_call], [user("u1"), echoed_call, user("u2")])

    assert contents(result.messages) == ["u1", None, "u2"]
    assert result.dropped == []


def test_respaced_tool_arguments_still_match() -> None:
    """A client re-serializing tool arguments must not look like a divergence."""

    stored_call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "function": {"name": "f", "arguments": '{"a":1,"b":2}'}}
        ],
    }
    echoed_call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "function": {"name": "f", "arguments": '{"b": 2, "a": 1}'}}
        ],
    }

    result = merge([user("u1"), stored_call], [user("u1"), echoed_call, user("u2")])

    assert result.dropped == []
    assert contents(result.messages) == ["u1", None, "u2"]


def test_repeated_identical_messages_are_not_collapsed() -> None:
    """The same user text twice is two turns, not one."""

    stored = [user("u1"), assistant("a1"), user("да"), assistant("a2")]
    result = merge(stored, [*stored, user("да")])

    assert contents(result.messages) == ["u1", "a1", "да", "a2", "да"]
    assert result.appended_cnt == 1
    assert result.dropped == []


def test_empty_incoming_history_leaves_the_record_alone() -> None:
    """A request without messages must not wipe the transcript."""

    stored = [user("u1"), assistant("a1")]
    result = merge(stored, [])

    assert contents(result.messages) == ["u1", "a1"]
    assert result.appended_cnt == 0
    assert result.dropped == []


def test_multimodal_content_is_compared_structurally() -> None:
    """A message whose content is a content-part list aligns like any other."""

    parts = {
        "role": "user",
        "content": [
            {"type": "text", "text": "что на фото?"},
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
        ],
    }

    result = merge([parts], [parts, assistant("это кот")])

    assert result.appended_cnt == 1
    assert result.dropped == []
    assert contents(result.messages)[1] == "это кот"
