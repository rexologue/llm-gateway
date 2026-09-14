"""Shared helpers for gateway unit tests.

The gateway is exercised here without a live backend or Valkey: response
reading is a synchronous state machine and transcript merging is pure, so both
are testable from plain functions. Anything that needs a real backend or a real
Valkey lives in the smoke suites under ``deploy/tests/smoke``.
"""

from __future__ import annotations

from typing import Any

import orjson


def sse_event(payload: Any) -> bytes:
    """Return one SSE ``data:`` event carrying a JSON payload."""

    return b"data: " + orjson.dumps(payload) + b"\n\n"


def sse_done() -> bytes:
    """Return the OpenAI-compatible end-of-stream sentinel event."""

    return b"data: [DONE]\n\n"


def chunk_payload(
    choices: list[dict[str, Any]],
    **extra: Any,
) -> dict[str, Any]:
    """Return a chat completion chunk payload with the given choices."""

    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1757500000,
        "model": "test-model",
        "choices": choices,
        **extra,
    }


def delta_event(delta: dict[str, Any], *, index: int = 0, finish: str | None = None) -> bytes:
    """Return one streamed chunk event carrying a single choice delta."""

    choice: dict[str, Any] = {"index": index, "delta": delta}

    if finish is not None:
        choice["finish_reason"] = finish

    return sse_event(chunk_payload([choice]))
