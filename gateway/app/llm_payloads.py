"""Small LLM request payload extractors used by metrics and traces."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MAX_MODEL_LABEL_LENGTH = 128


def model_label(payload: Mapping[str, Any] | None) -> str:
    """Return a bounded model label for metrics and traces."""

    model = payload.get("model") if payload is not None else None
    if not isinstance(model, str):
        return "unknown"

    model = model.strip()
    if not model or len(model) > MAX_MODEL_LABEL_LENGTH:
        return "unknown"

    return model


def message_count(payload: Mapping[str, Any] | None) -> int | None:
    """Return the chat message count when the payload has a messages array."""

    messages = payload.get("messages") if payload is not None else None
    return len(messages) if isinstance(messages, list) else None


def max_completion_tokens(payload: Mapping[str, Any] | None) -> int | None:
    """Return the requested max completion token limit when present."""

    if payload is None:
        return None

    value = payload.get("max_completion_tokens", payload.get("max_tokens"))
    if isinstance(value, int):
        return value
    return None
