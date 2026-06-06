"""Gateway utility helpers shared by routes, metrics, and tracing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any

import orjson

from app.http_utils import strip_hop_by_hop_headers

MAX_MODEL_LABEL_LENGTH = 128
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


@dataclass(frozen=True, slots=True)
class SamplingFallbackRule:
    """Validation rule and fallback value for one supported sampling parameter."""

    name: str
    fallback: float | int | None
    accepted_type: type
    minimum: float | int | None = None
    maximum: float | int | None = None
    minimum_inclusive: bool = True
    maximum_inclusive: bool = True


# Sampling fallback policy.
#
# The gateway only rewrites values that are clearly invalid for vLLM/SGLang
# OpenAI-compatible generation requests. Keep every supported parameter, range,
# and fallback in this table so the operational policy is visible in one place.
SAMPLING_FALLBACK_RULES = (
    SamplingFallbackRule(
        name="temperature",
        accepted_type=float,
        minimum=0.0,
        fallback=0.3,
    ),
    SamplingFallbackRule(
        name="top_p",
        accepted_type=float,
        minimum=0.0,
        maximum=1.0,
        minimum_inclusive=False,
        fallback=1.0,
    ),
    SamplingFallbackRule(
        name="min_p",
        accepted_type=float,
        minimum=0.0,
        maximum=1.0,
        fallback=0.0,
    ),
    SamplingFallbackRule(
        name="presence_penalty",
        accepted_type=float,
        minimum=-2.0,
        maximum=2.0,
        fallback=0.0,
    ),
    SamplingFallbackRule(
        name="frequency_penalty",
        accepted_type=float,
        minimum=-2.0,
        maximum=2.0,
        fallback=0.0,
    ),
    SamplingFallbackRule(
        name="seed",
        accepted_type=int,
        minimum=INT64_MIN,
        maximum=INT64_MAX,
        fallback=None,
    ),
)


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
    enable_sampling_fallback_override: bool,
) -> tuple[dict[str, Any], bytes, str, dict[str, Any] | None]:
    """Apply configured overrides before sending a chat request to the backend."""

    patched = dict(payload)
    fallback_params = None

    if forced_max_completion_tokens is not None:
        patched["max_completion_tokens"] = forced_max_completion_tokens
        patched.pop("max_tokens", None)

    if forced_thinking_disabled:
        disable_thinking(patched)

    if enable_sampling_fallback_override:
        fallback_params = apply_sampling_fallback_overrides(patched)

    raw_body, decoded_body = encode_payload(patched)

    return patched, raw_body, decoded_body, fallback_params


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
    """Set the common vLLM/SGLang chat-template knob that disables thinking."""

    chat_template_kwargs = payload.get("chat_template_kwargs")
    if isinstance(chat_template_kwargs, dict):
        payload["chat_template_kwargs"] = {
            **chat_template_kwargs,
            "enable_thinking": False,
        }
        return

    payload["chat_template_kwargs"] = {"enable_thinking": False}


def apply_sampling_fallback_overrides(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Replace invalid sampling values and return the original bad values."""

    fallback_params: dict[str, Any] = {}

    for rule in SAMPLING_FALLBACK_RULES:
        if rule.name not in payload:
            continue

        value = payload[rule.name]
        reason = sampling_value_invalid_reason(value, rule)
        if reason is None:
            continue

        fallback_params[rule.name] = {
            "received": value,
            "fallback": rule.fallback,
            "reason": reason,
        }

        if rule.fallback is None:
            payload.pop(rule.name, None)
        else:
            payload[rule.name] = rule.fallback

    return fallback_params or None


def sampling_value_invalid_reason(
    value: Any,
    rule: SamplingFallbackRule,
) -> str | None:
    """Return why a sampling value is invalid, or None when it is acceptable."""

    if rule.accepted_type is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return "invalid_type"

        numeric_value = float(value)
        if not isfinite(numeric_value):
            return "not_finite"

        return sampling_range_invalid_reason(numeric_value, rule)

    if rule.accepted_type is int:
        if isinstance(value, bool) or not isinstance(value, int):
            return "invalid_type"

        return sampling_range_invalid_reason(value, rule)

    return None


def sampling_range_invalid_reason(
    value: float | int,
    rule: SamplingFallbackRule,
) -> str | None:
    """Return whether a numeric sampling value violates the configured range."""

    if rule.minimum is not None:
        if rule.minimum_inclusive and value < rule.minimum:
            return "below_minimum"

        if not rule.minimum_inclusive and value <= rule.minimum:
            return "below_or_equal_minimum"

    if rule.maximum is not None:
        if rule.maximum_inclusive and value > rule.maximum:
            return "above_maximum"

        if not rule.maximum_inclusive and value >= rule.maximum:
            return "above_or_equal_maximum"

    return None


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
