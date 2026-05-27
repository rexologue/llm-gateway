"""Session-oriented gateway metric helpers."""

from __future__ import annotations

from app.observability import SESSION_INIT_E2E_LATENCY, SESSION_REQUEST_COUNTER


def bool_label(value: bool) -> str:
    """Return a Prometheus-friendly boolean label value."""

    return str(value).lower()


def status_family(status_code: int | None) -> str:
    """Return the coarse HTTP status family label."""

    if status_code is None:
        return "unknown"
    return f"{status_code // 100}xx"


def result_from_status(status_code: int | None, cancelled: bool) -> str:
    """Return the request result label from status and cancellation state."""

    if cancelled:
        return "cancelled"
    if status_code is None or status_code >= 400:
        return "error"
    return "success"


def session_metric_labels(
    *,
    route: str,
    method: str,
    stream: bool,
    model: str,
    status_code: int | None,
    cancelled: bool,
) -> dict[str, str]:
    """Build shared labels for session-init latency metrics."""

    return {
        "route": route,
        "method": method,
        "stream": bool_label(stream),
        "model": model,
        "status_family": status_family(status_code),
        "result": result_from_status(status_code, cancelled),
    }


def record_session_request(
    *,
    route: str,
    method: str,
    stream: bool,
    session_id: str | None,
    session_first_request: bool,
) -> None:
    """Increment the session request counter for one chat request."""

    SESSION_REQUEST_COUNTER.labels(
        route=route,
        method=method,
        stream=bool_label(stream),
        session_present=bool_label(session_id is not None),
        session_first_request=bool_label(session_first_request),
    ).inc()


def observe_session_e2e(
    *,
    session_first_request: bool,
    route: str,
    method: str,
    stream: bool,
    model: str,
    status_code: int | None,
    cancelled: bool,
    duration_sec: float,
) -> None:
    """Observe E2E latency for the first request in a session."""

    if not session_first_request:
        return

    SESSION_INIT_E2E_LATENCY.labels(
        **session_metric_labels(
            route=route,
            method=method,
            stream=stream,
            model=model,
            status_code=status_code,
            cancelled=cancelled,
        )
    ).observe(duration_sec)
