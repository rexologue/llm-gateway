"""Request payload shaping for OpenAI-compatible backend calls."""

from __future__ import annotations

from typing import Any

import orjson


def encode_payload(payload: dict[str, Any]) -> tuple[bytes, str]:
    """Serialize a JSON object payload into bytes and decoded text."""

    raw_body = orjson.dumps(payload)
    decoded_body = raw_body.decode("utf-8")
    return raw_body, decoded_body


def apply_chat_payload_overrides(
    payload: dict[str, Any],
    *,
    forced_max_completion_tokens: int | None,
    forced_thinking_disabled: bool,
) -> tuple[dict[str, Any], bytes, str]:
    """Apply configured chat-completion payload overrides."""

    patched = dict(payload)

    if forced_max_completion_tokens is not None:
        patched["max_completion_tokens"] = forced_max_completion_tokens
        patched.pop("max_tokens", None)

    if forced_thinking_disabled:
        patched["enable_thinking"] = False

    raw_body, decoded_body = encode_payload(patched)
    return patched, raw_body, decoded_body


def apply_generic_payload_overrides(
    payload: dict[str, Any],
    *,
    forced_thinking_disabled: bool,
) -> tuple[dict[str, Any], bytes, str]:
    """Apply configured payload overrides for non-chat OpenAI routes."""

    patched = dict(payload)

    if forced_thinking_disabled:
        patched["enable_thinking"] = False

    raw_body, decoded_body = encode_payload(patched)
    return patched, raw_body, decoded_body
