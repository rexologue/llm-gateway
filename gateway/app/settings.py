"""Application settings for the OpenAI-compatible gateway.

This module keeps all environment parsing in one place so the rest of the
application can depend on a typed configuration object instead of reading
environment variables ad hoc.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _get_bool_env(name: str, default: bool) -> bool:
    """Return a boolean environment variable using common true/false strings."""

    value = os.getenv(name)
    if value is None:
        return default

    return value.strip().lower() == "true"


@dataclass(frozen=True, slots=True)
class Settings:
    """Typed runtime configuration for the gateway process.

    Attributes map one-to-one to environment variables used by Docker Compose.
    The object is intentionally immutable because settings are read once during
    startup and should stay stable for the lifetime of the process.
    """

    # Backend routing and request shaping.
    backend_base_url: str
    enable_max_completion_tokens_override: bool
    forced_max_completion_tokens: int

    # Gateway HTTP client limits.
    connect_timeout: float
    read_timeout: float
    write_timeout: float
    pool_timeout: float
    http_max_connections: int
    http_max_keepalive_connections: int

    # Loki request/response event delivery.
    loki_app_name: str
    loki_enabled: bool
    loki_push_url: str
    loki_batch_size: int
    loki_flush_interval_sec: float
    loki_queue_max_size: int

    # OpenTelemetry trace export.
    otel_enabled: bool
    otel_service_name: str
    otel_exporter_otlp_endpoint: str
    otel_exporter_otlp_protocol: str
    otel_sample_ratio: float
    otel_fastapi_excluded_urls: str

    # Session state.
    session_valkey_url: str
    session_key_prefix: str
    session_ttl_sec: int
    session_tracker_max_connections: int

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables with production defaults."""

        otel_sample_ratio = max(
            0.0,
            min(1.0, float(os.getenv("GATEWAY_OTEL_SAMPLE_RATIO", "1.0"))),
        )

        return cls(
            # Backend routing and request shaping.
            backend_base_url=os.getenv(
                "GATEWAY_BACKEND_BASE_URL",
                "http://backend:8000",
            ).rstrip("/"),
            enable_max_completion_tokens_override=_get_bool_env(
                "GATEWAY_ENABLE_MAX_COMPLETION_TOKENS_OVERRIDE",
                False,
            ),
            forced_max_completion_tokens=int(
                os.getenv("GATEWAY_FORCED_MAX_COMPLETION_TOKENS", "1024")
            ),

            # Gateway HTTP client limits.
            connect_timeout=float(os.getenv("GATEWAY_TIMEOUT_CONNECT_SEC", "30")),
            read_timeout=float(os.getenv("GATEWAY_TIMEOUT_READ_SEC", "1800")),
            write_timeout=float(os.getenv("GATEWAY_TIMEOUT_WRITE_SEC", "1800")),
            pool_timeout=float(os.getenv("GATEWAY_TIMEOUT_POOL_SEC", "30")),
            http_max_connections=int(os.getenv("GATEWAY_HTTP_MAX_CONNECTIONS", "200")),
            http_max_keepalive_connections=int(
                os.getenv("GATEWAY_HTTP_MAX_KEEPALIVE_CONNECTIONS", "100")
            ),

            # Loki request/response event delivery.
            loki_app_name=os.getenv("GATEWAY_LOKI_APP_NAME", "llm-gateway"),
            loki_enabled=_get_bool_env("GATEWAY_LOKI_ENABLED", True),
            loki_push_url=os.getenv(
                "GATEWAY_LOKI_PUSH_URL",
                "http://llm-gateway-loki:3100/loki/api/v1/push",
            ),
            loki_batch_size=int(os.getenv("GATEWAY_LOKI_BATCH_SIZE", "200")),
            loki_flush_interval_sec=float(
                os.getenv("GATEWAY_LOKI_FLUSH_INTERVAL_SEC", "1.0")
            ),
            loki_queue_max_size=int(os.getenv("GATEWAY_LOKI_QUEUE_MAX_SIZE", "10000")),

            # OpenTelemetry trace export.
            otel_enabled=_get_bool_env("GATEWAY_OTEL_ENABLED", False),
            otel_service_name=os.getenv("GATEWAY_OTEL_SERVICE_NAME", "llm-gateway"),
            otel_exporter_otlp_endpoint=os.getenv(
                "GATEWAY_OTEL_EXPORTER_OTLP_ENDPOINT",
                "http://otel-collector:4317",
            ),
            otel_exporter_otlp_protocol=os.getenv(
                "GATEWAY_OTEL_EXPORTER_OTLP_PROTOCOL",
                "grpc",
            ),
            otel_sample_ratio=otel_sample_ratio,
            otel_fastapi_excluded_urls=os.getenv(
                "GATEWAY_OTEL_FASTAPI_EXCLUDED_URLS",
                "/gateway/metrics,/healthz,/$",
            ),

            # Session state.
            session_valkey_url=os.getenv(
                "GATEWAY_SESSION_VALKEY_URL",
                "redis://llm-gateway-valkey:6379/0",
            ),
            session_key_prefix=os.getenv(
                "GATEWAY_SESSION_KEY_PREFIX",
                "llm-gateway:session:",
            ),
            session_ttl_sec=int(os.getenv("GATEWAY_SESSION_TTL", "21600")),
            session_tracker_max_connections=int(
                os.getenv("GATEWAY_SESSION_TRACKER_MAX_CONNECTIONS", "256")
            ),
        )
