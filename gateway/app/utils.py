"""Gateway utility helpers shared by routes, metrics, and tracing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import orjson

from app.http_utils import strip_hop_by_hop_headers

MAX_MODEL_LABEL_LENGTH = 128


###################
# PAYLOAD SHAPING #
###################


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
    """Apply configured overrides before sending a chat request to the backend."""

    patched = dict(payload)

    if forced_max_completion_tokens is not None:
        patched["max_completion_tokens"] = forced_max_completion_tokens
        patched.pop("max_tokens", None)

    if forced_thinking_disabled:
        disable_thinking(patched)

    raw_body, decoded_body = encode_payload(patched)

    return patched, raw_body, decoded_body


def apply_generic_payload_overrides(
    payload: dict[str, Any],
    *,
    forced_thinking_disabled: bool,
) -> tuple[dict[str, Any], bytes, str]:
    """Apply configured overrides before sending a non-chat request to the backend."""

    patched = dict(payload)

    if forced_thinking_disabled:
        disable_thinking(patched)

    raw_body, decoded_body = encode_payload(patched)

    return patched, raw_body, decoded_body


def disable_thinking(payload: dict[str, Any]) -> None:
    """Set common backend knobs that disable thinking in chat templates."""

    payload["enable_thinking"] = False

    chat_template_kwargs = payload.get("chat_template_kwargs")
    if isinstance(chat_template_kwargs, dict):
        payload["chat_template_kwargs"] = {
            **chat_template_kwargs,
            "enable_thinking": False,
        }
        return

    payload["chat_template_kwargs"] = {"enable_thinking": False}


###################
# PAYLOAD GETTERS #
###################


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


####################
# RESPONSE HELPERS #
####################


def gateway_response_headers(
    headers: Mapping[str, str],
    *,
    request_id: str,
    session_id: str | None,
) -> dict[str, str]:
    """Build gateway response headers for a proxied or gateway-owned response."""

    response_headers = strip_hop_by_hop_headers(headers)
    response_headers["x-request-id"] = request_id

    if session_id is not None:
        response_headers["x-session-id"] = session_id

    return response_headers
