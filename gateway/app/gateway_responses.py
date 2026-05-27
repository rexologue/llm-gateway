"""HTTP response helpers for gateway-managed responses."""

from __future__ import annotations

from collections.abc import Mapping

from app.http_utils import strip_hop_by_hop_headers


def gateway_response_headers(
    headers: Mapping[str, str],
    *,
    request_id: str,
    session_id: str | None,
) -> dict[str, str]:
    """Build response headers returned by the gateway to the caller."""

    response_headers = strip_hop_by_hop_headers(headers)
    response_headers["x-request-id"] = request_id
    if session_id is not None:
        response_headers["x-session-id"] = session_id
    return response_headers
