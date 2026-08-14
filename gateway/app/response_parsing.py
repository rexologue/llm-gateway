"""Reconstruct the assistant message from an OpenAI-compatible chat response.

The gateway persists the request ``messages`` before generation so a dialog is
captured even when generation fails. To make the stored session a *complete*
transcript, we also need the assistant turn the backend just produced. For
non-streaming responses this is simply ``choices[0].message``; for streaming
responses it must be reassembled from SSE deltas, including any ``tool_calls``
whose ``arguments`` arrive as fragments across chunks.
"""

from __future__ import annotations

from typing import Any

from app.http_utils import parse_json_maybe


def assistant_message_from_response(
    *,
    stream: bool,
    response_text: str,
) -> dict[str, Any] | None:
    """Return the assistant message a chat response produced, or None.

    The returned value is an OpenAI-style chat message (``role: "assistant"``
    with ``content`` and/or ``tool_calls``) suitable for appending to a stored
    ``messages`` array.
    """

    if stream:
        return _assistant_message_from_stream(response_text)

    return _assistant_message_from_json(parse_json_maybe(response_text))


def _assistant_message_from_json(payload: Any) -> dict[str, Any] | None:
    """Return the assistant message from a non-stream chat completion body."""

    if not isinstance(payload, dict):
        return None

    choices = payload.get("choices")
    if not isinstance(choices, list):
        return None

    for choice in choices:
        if not isinstance(choice, dict):
            continue

        message = choice.get("message")
        if isinstance(message, dict):
            return message

    return None


def _assistant_message_from_stream(response_text: str) -> dict[str, Any] | None:
    """Reassemble the assistant message from an SSE chat completion stream."""

    content_fragments: list[str] = []
    # Tool call fragments accumulate per streamed index; dict preserves the
    # first-seen order which mirrors the backend's tool call ordering.
    tool_calls: dict[int, dict[str, Any]] = {}
    data_lines: list[str] = []
    saw_message = False

    def flush_event() -> None:
        nonlocal saw_message

        if not data_lines:
            return

        payload_text = "\n".join(data_lines).strip()
        data_lines.clear()

        if not payload_text or payload_text == "[DONE]":
            return

        payload = parse_json_maybe(payload_text)
        if not isinstance(payload, dict):
            return

        choices = payload.get("choices")
        if not isinstance(choices, list):
            return

        for choice in choices:
            if not isinstance(choice, dict):
                continue

            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue

            saw_message = True

            content = delta.get("content")
            if isinstance(content, str) and content:
                content_fragments.append(content)

            _accumulate_tool_calls(tool_calls, delta.get("tool_calls"))

    for line in response_text.splitlines():
        if not line.strip():
            flush_event()
            continue

        if line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())

    flush_event()

    if not saw_message:
        return None

    content = "".join(content_fragments)
    message: dict[str, Any] = {"role": "assistant", "content": content or None}

    if tool_calls:
        message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]

    return message


def _accumulate_tool_calls(
    tool_calls: dict[int, dict[str, Any]],
    delta_tool_calls: Any,
) -> None:
    """Merge streamed tool call fragments into per-index accumulators."""

    if not isinstance(delta_tool_calls, list):
        return

    for position, fragment in enumerate(delta_tool_calls):
        if not isinstance(fragment, dict):
            continue

        raw_index = fragment.get("index")
        index = raw_index if isinstance(raw_index, int) else position

        entry = tool_calls.setdefault(
            index,
            {"type": "function", "function": {"name": "", "arguments": ""}},
        )

        call_id = fragment.get("id")
        if isinstance(call_id, str) and call_id:
            entry["id"] = call_id

        call_type = fragment.get("type")
        if isinstance(call_type, str) and call_type:
            entry["type"] = call_type

        function_fragment = fragment.get("function")
        if isinstance(function_fragment, dict):
            function = entry["function"]

            name = function_fragment.get("name")
            if isinstance(name, str) and name:
                function["name"] = name

            arguments = function_fragment.get("arguments")
            if isinstance(arguments, str) and arguments:
                function["arguments"] += arguments
