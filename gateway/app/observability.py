"""Prometheus metrics used by the gateway."""

from __future__ import annotations

import time

from prometheus_client import Counter, Histogram

APP_START_TS = time.time()

REQUEST_COUNTER = Counter(
    "gateway_proxy_requests_total",
    "Total number of requests processed by the gateway",
    ["route", "method", "stream"],
)

REQUEST_LATENCY = Histogram(
    "gateway_proxy_request_latency_seconds",
    "End-to-end request latency through the gateway",
    ["route", "method", "stream"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 900),
)

LOKI_PUSH_COUNTER = Counter(
    "gateway_proxy_loki_push_total",
    "Log push attempts to Loki",
    ["status"],
)

LOKI_EVENTS_DROPPED_COUNTER = Counter(
    "gateway_proxy_loki_events_dropped_total",
    "Loki events dropped before delivery",
    ["reason"],
)

SESSION_INIT_TTFT = Histogram(
    "gateway_session_init_ttft_seconds",
    "TTFT of the first observed chat completion request in a session",
    ["route", "method", "stream", "model", "status_family", "result"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)

SESSION_INIT_E2E_LATENCY = Histogram(
    "gateway_session_init_e2e_latency_seconds",
    "End-to-end latency of the first observed chat completion request in a session",
    ["route", "method", "stream", "model", "status_family", "result"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 900),
)

SESSION_REQUEST_COUNTER = Counter(
    "gateway_session_requests_total",
    "Total session-aware requests processed by the gateway",
    ["route", "method", "stream", "session_present", "session_first_request"],
)

SESSION_ID_MISSING_COUNTER = Counter(
    "gateway_session_id_missing_total",
    "Requests without X-Session-ID header",
    ["route", "method", "stream"],
)

SESSION_TRACKER_ERRORS_COUNTER = Counter(
    "gateway_session_tracker_errors_total",
    "Session tracker failures while checking or refreshing session state",
    ["operation", "error_type"],
)

SESSION_INIT_TTFT_MISSING_COUNTER = Counter(
    "gateway_session_init_ttft_missing_total",
    "First session requests where TTFT could not be observed",
    ["route", "method", "stream", "model", "reason", "result"],
)


def uptime_seconds() -> float:
    """Return the process uptime in seconds."""

    return time.time() - APP_START_TS
