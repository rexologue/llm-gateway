"""Tests for rebuilding an assistant turn out of a backend chat response.

Every case here is a defect the previous reader had. The two shapes are checked
against the same expectations on purpose: a streamed answer and a non-streamed
answer must reduce to the same turn.
"""

from __future__ import annotations

import orjson
import pytest

from app.backend import ChatResponseReader
from tests.conftest import chunk_payload, delta_event, sse_done, sse_event


def read_stream(*chunks: bytes, drop_usage_events: bool = False):
    """Feed chunks through a reader and return the forwarded bytes and turn."""

    reader = ChatResponseReader(drop_usage_events=drop_usage_events)
    forwarded = b"".join(reader.feed(chunk) for chunk in chunks)
    forwarded += reader.flush()

    return forwarded, reader.turn()


def test_stream_joins_tool_call_name_fragments() -> None:
    """A function name split across fragments must be joined, not overwritten."""

    _forwarded, turn = read_stream(
        delta_event({"role": "assistant"}),
        delta_event(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_cur", "arguments": '{"ci'},
                    }
                ]
            }
        ),
        delta_event(
            {
                "tool_calls": [
                    {"index": 0, "function": {"name": "rent_weather", "arguments": 'ty":"Мо'}}
                ]
            }
        ),
        delta_event({"tool_calls": [{"index": 0, "function": {"arguments": 'сква"}'}}]}),
        sse_done(),
    )

    call = turn.assistant_message["tool_calls"][0]
    assert call["function"]["name"] == "get_current_weather"
    assert call["function"]["arguments"] == '{"city":"Москва"}'
    assert call["id"] == "call_1"


def test_stream_keeps_a_repeated_whole_tool_name_intact() -> None:
    """A backend that repeats the full name must not get a doubled name."""

    _forwarded, turn = read_stream(
        delta_event(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "get_weather", "arguments": "{"},
                    }
                ]
            }
        ),
        delta_event(
            {"tool_calls": [{"index": 0, "function": {"name": "get_weather", "arguments": "}"}}]}
        ),
        sse_done(),
    )

    assert turn.assistant_message["tool_calls"][0]["function"]["name"] == "get_weather"


def test_stream_keeps_parallel_tool_calls_apart() -> None:
    """Two tool calls in one turn must stay two calls, in index order."""

    _forwarded, turn = read_stream(
        delta_event(
            {
                "tool_calls": [
                    {"index": 1, "id": "call_b", "function": {"name": "get_time", "arguments": "{}"}},
                    {"index": 0, "id": "call_a", "function": {"name": "get_weather", "arguments": "{}"}},
                ]
            }
        ),
        sse_done(),
    )

    calls = turn.assistant_message["tool_calls"]
    assert [call["function"]["name"] for call in calls] == ["get_weather", "get_time"]


def test_stream_keeps_reasoning_content() -> None:
    """Reasoning deltas must survive instead of being dropped."""

    _forwarded, turn = read_stream(
        delta_event({"reasoning_content": "ду"}),
        delta_event({"reasoning_content": "маю"}),
        delta_event({"content": "Готово"}),
        sse_done(),
    )

    message = turn.assistant_message
    assert message["reasoning_content"] == "думаю"
    assert message["content"] == "Готово"


def test_stream_keeps_choices_separate() -> None:
    """With n>1 the choices must not be concatenated into one message."""

    _forwarded, turn = read_stream(
        sse_event(
            chunk_payload(
                [
                    {"index": 0, "delta": {"content": "ОТВЕТ-А"}},
                    {"index": 1, "delta": {"content": "ОТВЕТ-Б"}},
                ]
            )
        ),
        sse_done(),
    )

    assert [choice.message["content"] for choice in turn.choices] == ["ОТВЕТ-А", "ОТВЕТ-Б"]
    assert turn.assistant_message["content"] == "ОТВЕТ-А"


def test_stream_captures_usage_and_finish_reason() -> None:
    """The usage-only event and the finish reason must both be recorded."""

    _forwarded, turn = read_stream(
        delta_event({"content": "Готово"}, finish="stop"),
        sse_event(chunk_payload([], usage={"prompt_tokens": 11, "completion_tokens": 3})),
        sse_done(),
    )

    assert turn.finish_reason == "stop"
    assert turn.usage == {"prompt_tokens": 11, "completion_tokens": 3}


def test_event_split_across_chunks_is_reassembled() -> None:
    """An event cut in half by the transport must still parse and relay whole."""

    event = delta_event({"content": "склеено"}, finish="stop")
    forwarded, turn = read_stream(event[:12], event[12:], sse_done())

    assert turn.assistant_message["content"] == "склеено"
    assert forwarded == event + sse_done()


def test_dropped_usage_event_is_recorded_but_not_forwarded() -> None:
    """A caller who never asked for usage must not receive the usage event."""

    content_event = delta_event({"content": "Готово"}, finish="stop")
    usage_event = sse_event(chunk_payload([], usage={"prompt_tokens": 11}))

    # Both events arrive in one chunk: the filter has to split them apart.
    forwarded, turn = read_stream(
        content_event + usage_event,
        sse_done(),
        drop_usage_events=True,
    )

    assert turn.usage == {"prompt_tokens": 11}
    assert forwarded == content_event + sse_done()
    assert b"usage" not in forwarded


def test_usage_event_is_forwarded_when_the_caller_asked() -> None:
    """With usage requested downstream, the stream passes through untouched."""

    usage_event = sse_event(chunk_payload([], usage={"prompt_tokens": 11}))
    forwarded, _turn = read_stream(usage_event, sse_done(), drop_usage_events=False)

    assert forwarded == usage_event + sse_done()


def test_comments_and_unparsable_events_pass_through() -> None:
    """Protocol noise the gateway does not understand is relayed as-is."""

    noise = b": keepalive\n\n"
    broken = b"data: {not json\n\n"
    forwarded, turn = read_stream(noise, broken, delta_event({"content": "x"}), sse_done())

    assert noise in forwarded and broken in forwarded
    assert turn.assistant_message["content"] == "x"


def test_stream_without_done_is_marked_truncated() -> None:
    """A stream that never terminated must not look like a complete turn."""

    _forwarded, turn = read_stream(delta_event({"content": "полу"}))

    assert turn.truncated is True
    assert turn.assistant_message["content"] == "полу"


def test_completed_stream_is_not_truncated() -> None:
    """A stream ending in the sentinel is a complete turn."""

    _forwarded, turn = read_stream(delta_event({"content": "целое"}), sse_done())

    assert turn.truncated is False


def test_empty_stream_yields_an_empty_turn() -> None:
    """No events at all means no assistant message to persist."""

    _forwarded, turn = read_stream(b"")

    assert turn.assistant_message is None
    assert turn.truncated is False


@pytest.mark.parametrize("as_bytes", [False, True])
def test_body_turn_keeps_every_choice(as_bytes: bool) -> None:
    """A non-stream body must keep all choices, not only the first."""

    body = orjson.dumps(
        {
            "id": "chatcmpl-test",
            "model": "test-model",
            "created": 1757500000,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "ОТВЕТ-А"}, "finish_reason": "stop"},
                {"index": 1, "message": {"role": "assistant", "content": "ОТВЕТ-Б"}, "finish_reason": "stop"},
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2},
        }
    )

    turn = ChatResponseReader.turn_from_body(body if as_bytes else body.decode("utf-8"))

    assert [choice.message["content"] for choice in turn.choices] == ["ОТВЕТ-А", "ОТВЕТ-Б"]
    assert turn.assistant_message["content"] == "ОТВЕТ-А"
    assert turn.finish_reason == "stop"
    assert turn.usage == {"prompt_tokens": 7, "completion_tokens": 2}
    assert turn.model == "test-model"
    assert turn.truncated is False


def test_body_turn_keeps_tool_calls_verbatim() -> None:
    """A non-stream tool call is already whole and must be kept as it came."""

    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_current_weather", "arguments": '{"city":"Москва"}'},
        }
    ]
    body = orjson.dumps(
        {
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": None, "tool_calls": tool_calls},
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )

    turn = ChatResponseReader.turn_from_body(body)

    assert turn.assistant_message["tool_calls"] == tool_calls
    assert turn.finish_reason == "tool_calls"


def test_body_turn_of_an_error_response_is_empty() -> None:
    """A backend error body carries no turn to append to the dialog."""

    turn = ChatResponseReader.turn_from_body(orjson.dumps({"error": {"message": "boom"}}))

    assert turn.assistant_message is None
    assert turn.choices == []


def test_stream_and_body_agree_on_the_same_answer() -> None:
    """The same answer must produce the same message in either shape."""

    _forwarded, streamed = read_stream(
        delta_event({"role": "assistant"}),
        delta_event({"content": "Здравствуйте"}, finish="stop"),
        sse_done(),
    )
    from_body = ChatResponseReader.turn_from_body(
        orjson.dumps(
            {
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Здравствуйте"},
                        "finish_reason": "stop",
                    }
                ]
            }
        )
    )

    assert streamed.assistant_message == from_body.assistant_message
    assert streamed.finish_reason == from_body.finish_reason
