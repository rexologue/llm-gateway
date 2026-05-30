"""Shared HTTP route paths used by the gateway."""

from __future__ import annotations

ROOT_ROUTE = "/"
HEALTH_ROUTE = "/health"
GATEWAY_METRICS_ROUTE = "/gateway/metrics"
GATEWAY_SESSION_LIST_ROUTE = "/gateway/session_list"
GATEWAY_SESSION_DETAIL_ROUTE = "/gateway/session/{session_id}"

V1_ROUTE_PREFIX = "/v1"
CHAT_COMPLETIONS_ROUTE = f"{V1_ROUTE_PREFIX}/chat/completions"
GENERIC_V1_PROXY_ROUTE = f"{V1_ROUTE_PREFIX}/{{full_path:path}}"

DEFAULT_OTEL_FASTAPI_EXCLUDED_URLS = f"{GATEWAY_METRICS_ROUTE},{HEALTH_ROUTE},/$"
