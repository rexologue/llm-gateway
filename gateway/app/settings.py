"""Application settings for the vLLM gateway.

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

    host: str
    port: int
    upstream_base_url: str
    enable_max_completion_tokens_override: bool
    forced_max_completion_tokens: int
    environment: str
    request_log_label: str
    connect_timeout: float
    read_timeout: float
    write_timeout: float
    pool_timeout: float
    http_max_connections: int
    http_max_keepalive_connections: int
    loki_enabled: bool
    loki_push_url: str
    loki_batch_size: int
    loki_flush_interval_sec: float
    loki_queue_max_size: int
    log_body_sha256: bool
    otel_enabled: bool
    otel_service_name: str
    otel_exporter_otlp_endpoint: str
    otel_exporter_otlp_protocol: str
    otel_sample_ratio: float
    otel_fastapi_excluded_urls: str
    session_valkey_url: str
    session_key_prefix: str
    session_ttl_sec: int
    session_tracker_max_connections: int

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables with production defaults."""

        otel_sample_ratio = max(0.0, min(1.0, float(os.getenv("OTEL_SAMPLE_RATIO", "1.0"))))

        return cls(
            host=os.getenv("GATEWAY_HOST", "0.0.0.0"),
            port=int(os.getenv("GATEWAY_PORT", "8080")),
            upstream_base_url=os.getenv("GATEWAY_UPSTREAM_BASE_URL", "http://vllm:8000").rstrip("/"),
            enable_max_completion_tokens_override=_get_bool_env("GATEWAY_ENABLE_MAX_COMPLETION_TOKENS_OVERRIDE", False),
            forced_max_completion_tokens=int(os.getenv("GATEWAY_FORCED_MAX_COMPLETION_TOKENS", "1024")),
            environment=os.getenv("GATEWAY_ENV", "local"),
            request_log_label=os.getenv("GATEWAY_REQUEST_LOG_LABEL", "vllm-gateway"),
            connect_timeout=float(os.getenv("GATEWAY_TIMEOUT_CONNECT_SEC", "30")),
            read_timeout=float(os.getenv("GATEWAY_TIMEOUT_READ_SEC", "1800")),
            write_timeout=float(os.getenv("GATEWAY_TIMEOUT_WRITE_SEC", "1800")),
            pool_timeout=float(os.getenv("GATEWAY_TIMEOUT_POOL_SEC", "30")),
            http_max_connections=int(os.getenv("GATEWAY_HTTP_MAX_CONNECTIONS", "200")),
            http_max_keepalive_connections=int(
                os.getenv("GATEWAY_HTTP_MAX_KEEPALIVE_CONNECTIONS", "100")
            ),
            loki_enabled=_get_bool_env("LOKI_ENABLED", True),
            loki_push_url=os.getenv("LOKI_PUSH_URL", "http://vllm-gateway-loki:3100/loki/api/v1/push"),
            loki_batch_size=int(os.getenv("LOKI_BATCH_SIZE", "200")),
            loki_flush_interval_sec=float(os.getenv("LOKI_FLUSH_INTERVAL_SEC", "1.0")),
            loki_queue_max_size=int(os.getenv("LOKI_QUEUE_MAX_SIZE", "10000")),
            log_body_sha256=_get_bool_env("LOG_BODY_SHA256", True),
            otel_enabled=_get_bool_env("OTEL_ENABLED", False),
            otel_service_name=os.getenv("OTEL_SERVICE_NAME", "vllm-gateway"),
            otel_exporter_otlp_endpoint=os.getenv(
                "OTEL_EXPORTER_OTLP_ENDPOINT",
                "http://otel-collector:4317",
            ),
            otel_exporter_otlp_protocol=os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc"),
            otel_sample_ratio=otel_sample_ratio,
            otel_fastapi_excluded_urls=os.getenv(
                "OTEL_FASTAPI_EXCLUDED_URLS",
                "/metrics,/gateway/metrics,/healthz,/$",
            ),
            session_valkey_url=os.getenv(
                "SESSION_VALKEY_URL",
                "redis://vllm-gateway-valkey:6379/0",
            ),
            session_key_prefix=os.getenv("SESSION_KEY_PREFIX", "vllm-gateway:session:"),
            session_ttl_sec=int(os.getenv("SESSION_TTL", "21600")),
            session_tracker_max_connections=int(
                os.getenv("SESSION_TRACKER_MAX_CONNECTIONS", "256")
            ),
        )
