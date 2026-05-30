"""Prometheus metrics and gateway-domain metric helpers."""

from __future__ import annotations

from dataclasses import dataclass

from prometheus_client import Counter, Gauge, Histogram

from app.route_paths import CHAT_COMPLETIONS_ROUTE

REQUEST_COUNTER = Counter(
    "gateway_requests_total",
    "Total number of requests processed by the gateway",
    ["route", "method", "stream"],
)

REQUEST_E2E_LATENCY = Histogram(
    "gateway_request_e2e_seconds",
    "End-to-end request latency through the gateway",
    ["route", "method", "stream", "model", "status_family", "result"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 900),
)

REQUEST_TTFT = Histogram(
    "gateway_request_ttft_seconds",
    "Time to first non-empty streamed backend chunk",
    ["route", "method", "stream", "model", "status_family", "result"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)

RESPONSE_COUNTER = Counter(
    "gateway_responses_total",
    "Total number of gateway responses by status family and result",
    ["route", "method", "stream", "status_family", "result"],
)

SESSION_REQUEST_COUNTER = Counter(
    "gateway_session_requests_total",
    "Total session-aware chat completion requests processed by the gateway",
    ["route", "method", "stream", "session_present", "session_first_request"],
)

SESSION_E2E_LATENCY = Histogram(
    "gateway_session_request_e2e_seconds",
    "End-to-end latency of chat completion requests with session classification",
    [
        "route",
        "method",
        "stream",
        "model",
        "session_first_request",
        "status_family",
        "result",
    ],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 900),
)

SESSION_TTFT = Histogram(
    "gateway_session_request_ttft_seconds",
    "TTFT of streamed chat completion requests with session classification",
    [
        "route",
        "method",
        "stream",
        "model",
        "session_first_request",
        "status_family",
        "result",
    ],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)

SESSION_TRACKER_ERRORS_COUNTER = Counter(
    "gateway_session_tracker_errors_total",
    "Session tracker failures while checking or refreshing session state",
    ["operation", "error_type"],
)

LOKI_PUSH_COUNTER = Counter(
    "gateway_loki_push_total",
    "Log push attempts to Loki",
    ["status"],
)

LOKI_EVENTS_DROPPED_COUNTER = Counter(
    "gateway_loki_events_dropped_total",
    "Loki events dropped before delivery",
    ["reason"],
)

ACTIVE_SESSION_GAUGE = Gauge(
    "gateway_active_sessions",
    "Current number of runtime session keys in Valkey DB 0",
)


@dataclass(frozen=True, slots=True)
class MetricsRequestContext:
    """Bound metric labels for one logical gateway request."""

    metrics: GatewayMetrics
    route: str
    method: str
    stream: bool
    model: str
    session_id: str | None
    session_first_request: bool


    def request(self) -> None:
        """Record that this gateway request has been accepted."""

        self.metrics.record_request(self)


    def response(
        self,
        *,
        status_code: int | None,
        cancelled: bool,
        e2e_sec: float,
    ) -> None:
        """Record the terminal request outcome and E2E latency."""

        self.metrics.record_response(
            self,
            status_code=status_code,
            cancelled=cancelled,
            e2e_sec=e2e_sec,
        )


    def ttft(
        self,
        *,
        status_code: int | None,
        cancelled: bool,
        ttft_sec: float,
    ) -> None:
        """Record time to first token/chunk for this request."""

        self.metrics.record_ttft(
            self,
            status_code=status_code,
            cancelled=cancelled,
            ttft_sec=ttft_sec,
        )


class GatewayMetrics:
    """Facade for all Prometheus metric updates in the gateway."""

    def context(
        self,
        *,
        route: str,
        method: str,
        stream: bool,
        model: str = "unknown",
        session_id: str | None = None,
        session_first_request: bool = False,
    ) -> MetricsRequestContext:
        """Bind repeated metric labels for one logical gateway request."""

        return MetricsRequestContext(
            metrics=self,
            route=route,
            method=method,
            stream=stream,
            model=model,
            session_id=session_id,
            session_first_request=session_first_request,
        )


    def record_request(self, context: MetricsRequestContext) -> None:
        """Increment request counters for one accepted gateway request."""

        REQUEST_COUNTER.labels(
            route=context.route,
            method=context.method,
            stream=self.bool_label(context.stream),
        ).inc()

        if context.route == CHAT_COMPLETIONS_ROUTE:
            SESSION_REQUEST_COUNTER.labels(
                route=context.route,
                method=context.method,
                stream=self.bool_label(context.stream),
                session_present=self.bool_label(context.session_id is not None),
                session_first_request=self.bool_label(context.session_first_request),
            ).inc()


    def record_response(
        self,
        context: MetricsRequestContext,
        *,
        status_code: int | None,
        cancelled: bool,
        e2e_sec: float,
    ) -> None:
        """Record terminal response counters and E2E histograms."""

        labels = self.outcome_labels(
            route=context.route,
            method=context.method,
            stream=context.stream,
            model=context.model,
            status_code=status_code,
            cancelled=cancelled,
        )

        RESPONSE_COUNTER.labels(
            route=context.route,
            method=context.method,
            stream=self.bool_label(context.stream),
            status_family=labels["status_family"],
            result=labels["result"],
        ).inc()
        REQUEST_E2E_LATENCY.labels(**labels).observe(e2e_sec)

        if context.route == CHAT_COMPLETIONS_ROUTE:
            SESSION_E2E_LATENCY.labels(
                **labels,
                session_first_request=self.bool_label(context.session_first_request),
            ).observe(e2e_sec)


    def record_ttft(
        self,
        context: MetricsRequestContext,
        *,
        status_code: int | None,
        cancelled: bool,
        ttft_sec: float,
    ) -> None:
        """Record TTFT histograms for streamed responses."""

        labels = self.outcome_labels(
            route=context.route,
            method=context.method,
            stream=context.stream,
            model=context.model,
            status_code=status_code,
            cancelled=cancelled,
        )

        REQUEST_TTFT.labels(**labels).observe(ttft_sec)

        if context.route == CHAT_COMPLETIONS_ROUTE:
            SESSION_TTFT.labels(
                **labels,
                session_first_request=self.bool_label(context.session_first_request),
            ).observe(ttft_sec)


    def set_active_sessions(self, count: int) -> None:
        """Set the active runtime session gauge."""

        ACTIVE_SESSION_GAUGE.set(count)


    def session_tracker_error(self, operation: str, error: BaseException) -> None:
        """Record a session tracker backend failure."""

        SESSION_TRACKER_ERRORS_COUNTER.labels(
            operation=operation,
            error_type=type(error).__name__,
        ).inc()


    def loki_push(self, status: str) -> None:
        """Record one Loki push attempt outcome."""

        LOKI_PUSH_COUNTER.labels(status=status).inc()


    def loki_event_dropped(self, reason: str) -> None:
        """Record one Loki event dropped before delivery."""

        LOKI_EVENTS_DROPPED_COUNTER.labels(reason=reason).inc()


    @staticmethod
    def outcome_labels(
        *,
        route: str,
        method: str,
        stream: bool,
        model: str,
        status_code: int | None,
        cancelled: bool,
    ) -> dict[str, str]:
        """Build common outcome labels for latency histograms."""

        return {
            "route": route,
            "method": method,
            "stream": GatewayMetrics.bool_label(stream),
            "model": model,
            "status_family": GatewayMetrics.status_family(status_code),
            "result": GatewayMetrics.result_from_status(status_code, cancelled),
        }


    @staticmethod
    def bool_label(value: bool) -> str:
        """Return a Prometheus-friendly boolean label value."""

        return str(value).lower()


    @staticmethod
    def status_family(status_code: int | None) -> str:
        """Return the coarse HTTP status family label."""

        if status_code is None:
            return "unknown"

        return f"{status_code // 100}xx"


    @staticmethod
    def result_from_status(status_code: int | None, cancelled: bool) -> str:
        """Return the request result label from status and cancellation state."""

        if cancelled:
            return "cancelled"

        if status_code is None or status_code >= 400:
            return "error"

        return "success"
