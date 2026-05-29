"""Helpers for safe HTTP proxying and payload inspection."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any, Mapping

import httpx
import orjson

REDACTED_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-api-key",
}

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""

    return datetime.now(UTC).isoformat()


def sanitize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return headers with sensitive values redacted for logging.

    The gateway logs raw request and response metadata, so masking secrets in a
    single helper is safer than relying on each route to remember the policy.
    """

    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        sanitized[key] = "***REDACTED***" if key.lower() in REDACTED_HEADERS else value

    return sanitized


def strip_hop_by_hop_headers(headers: httpx.Headers | Mapping[str, str]) -> dict[str, str]:
    """Drop hop-by-hop headers that must not be forwarded by an HTTP proxy."""

    return {key: value for key, value in headers.items() if key.lower() not in HOP_BY_HOP_HEADERS}


def parse_json_maybe(text: str) -> Any | None:
    """Parse JSON text and return ``None`` instead of raising on invalid input."""

    try:
        return orjson.loads(text)
    
    except Exception:
        return None


def sha256_hexdigest(data: bytes) -> str:
    """Return a body hash for correlation and deduplication."""

    return hashlib.sha256(data).hexdigest()


def request_id_from_headers(headers: Mapping[str, str]) -> str:
    """Reuse an incoming ``X-Request-Id`` header or generate a new opaque id."""

    for key, value in headers.items():
        if key.lower() == "x-request-id" and value:
            return value
    return uuid.uuid4().hex


def session_id_from_headers(headers: Mapping[str, str]) -> str | None:
    """Return a non-empty ``X-Session-ID`` header value when present."""

    for key, value in headers.items():
        if key.lower() == "x-session-id" and value.strip():
            return value.strip()
        
    return None
