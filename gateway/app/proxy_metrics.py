"""Gateway-wide proxy metric helpers."""

from __future__ import annotations

from app.observability import PROXY_RESPONSE_COUNTER
from app.session_metrics import bool_label, result_from_status, status_family


def record_proxy_response(
    *,
    route: str,
    method: str,
    stream: bool,
    status_code: int | None,
    cancelled: bool,
) -> None:
    """Increment the gateway response counter for one completed proxy outcome."""

    PROXY_RESPONSE_COUNTER.labels(
        route=route,
        method=method,
        stream=bool_label(stream),
        status_family=status_family(status_code),
        result=result_from_status(status_code, cancelled),
    ).inc()
