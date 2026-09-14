"""Application settings for the OpenAI-compatible gateway.

This module keeps all environment parsing in one place so the rest of the
application can depend on a typed configuration object instead of reading
environment variables ad hoc.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from app.route_paths import DEFAULT_OTEL_FASTAPI_EXCLUDED_URLS


def _get_bool_env(name: str, default: bool) -> bool:
    """Return a boolean environment variable using common true/false strings."""

    value = os.getenv(name)
    if value is None:
        return default

    return value.strip().lower() == "true"


def _get_optional_int_env(name: str) -> int | None:
    """Return an integer environment variable when it is set and non-empty."""

    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None

    return int(value)


def _valkey_url_for_db(url: str, db: int) -> str:
    """Return a Valkey URL that points at the requested logical database."""

    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{db}", parts.query, parts.fragment))


def _require_monitor_base_url(enabled: bool) -> str:
    """Return the Monitor base URL, refusing to enable Monitor without one.

    An enabled sink with no address is the failure this check exists to
    prevent: the gateway would keep running, keep queueing records, and keep
    reporting nothing, and the mistake would surface as a Monitor dashboard
    that is simply missing this engine.
    """

    base_url = os.getenv("GATEWAY_MONITOR_API_URL", "").strip().rstrip("/")

    if enabled and not base_url:
        raise ValueError(
            "GATEWAY_MONITOR_API_URL must be set when GATEWAY_MONITOR_ENABLED is true"
        )

    return base_url


def _require_engine_id() -> str:
    """Return the engine identity of this gateway, refusing to guess one.

    The value names this engine in every Loki stream and every trace resource,
    so a deployment that forgets it does not fail loudly - it quietly merges the
    telemetry of every machine into one anonymous heap, and the mistake surfaces
    months later as a dashboard that cannot be split. Stopping at startup is the
    cheaper failure.
    """

    engine_id = os.getenv("GATEWAY_ENGINE_ID", "").strip()

    if not engine_id:
        raise ValueError(
            "GATEWAY_ENGINE_ID must be set: it labels this engine in Loki and Tempo"
        )

    return engine_id


@dataclass(frozen=True, slots=True)
class Settings:
    """Typed runtime configuration for the gateway process.

    Attributes map one-to-one to environment variables used by Docker Compose.
    The object is intentionally immutable because settings are read once during
    startup and should stay stable for the lifetime of the process.
    """

    # Identity of the engine this gateway instance sits in front of.
    engine_id: str

    # Backend routing and request shaping.
    backend_base_url: str
    forced_max_completion_tokens: int | None
    forced_thinking_disabled: bool
    enable_sampling_fallback_override: bool

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

    # Monitor conversation metric delivery.
    monitor_enabled: bool
    monitor_base_url: str
    monitor_service_name: str
    monitor_timeout_sec: float
    monitor_queue_max_size: int
    monitor_concurrency: int

    # OpenTelemetry trace export.
    otel_enabled: bool
    otel_service_name: str
    otel_exporter_otlp_endpoint: str
    otel_sample_ratio: float
    otel_fastapi_excluded_urls: str

    # Session state.
    sessions_enabled: bool
    valkey_url: str
    session_runtime_valkey_url: str
    session_key_prefix: str
    session_ttl_sec: int
    session_tracker_max_connections: int
    valkey_breaker_failures: int
    valkey_breaker_cooldown_sec: float

    # Persisted chat sessions.
    session_store_valkey_url: str
    session_store_key_prefix: str
    session_store_ttl_sec: int
    session_store_max_connections: int
    session_store_max_record_bytes: int
    session_store_write_attempts: int

    # Backend draining independent of the caller.
    drain_after_disconnect: bool
    drain_timeout_sec: float
    drain_max_bytes: int
    force_stream_usage: bool

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables with production defaults."""

        monitor_enabled = _get_bool_env("GATEWAY_MONITOR_ENABLED", False)
        otel_sample_ratio = max(
            0.0,
            min(1.0, float(os.getenv("GATEWAY_OTEL_SAMPLE_RATIO", "1.0"))),
        )
        valkey_url = os.getenv(
            "GATEWAY_VALKEY_URL",
            "redis://llm-gateway-valkey:6379",
        )

        return cls(
            # Identity of the engine this gateway instance sits in front of.
            engine_id=_require_engine_id(),

            # Backend routing and request shaping.
            backend_base_url=os.getenv(
                "GATEWAY_BACKEND_BASE_URL",
                "http://backend:8000",
            ).rstrip("/"),
            forced_max_completion_tokens=_get_optional_int_env(
                "GATEWAY_FORCED_MAX_COMPLETION_TOKENS"
            ),
            forced_thinking_disabled=_get_bool_env(
                "GATEWAY_FORCED_THINKING_DISABLED",
                False,
            ),
            enable_sampling_fallback_override=_get_bool_env(
                "GATEWAY_ENABLE_SAMPLING_FALLBACK_OVERRIDE",
                False,
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

            # Monitor conversation metric delivery.
            monitor_enabled=monitor_enabled,
            monitor_base_url=_require_monitor_base_url(monitor_enabled),
            monitor_service_name=os.getenv(
                "GATEWAY_MONITOR_SERVICE_NAME",
                "llm-gateway",
            ),
            monitor_timeout_sec=float(os.getenv("GATEWAY_MONITOR_TIMEOUT_SEC", "1.0")),
            monitor_queue_max_size=int(
                os.getenv("GATEWAY_MONITOR_QUEUE_MAX_SIZE", "10000")
            ),
            monitor_concurrency=int(os.getenv("GATEWAY_MONITOR_CONCURRENCY", "4")),

            # OpenTelemetry trace export.
            otel_enabled=_get_bool_env("GATEWAY_OTEL_ENABLED", False),
            otel_service_name=os.getenv("GATEWAY_OTEL_SERVICE_NAME", "llm-gateway"),
            otel_exporter_otlp_endpoint=os.getenv(
                "GATEWAY_OTEL_EXPORTER_OTLP_ENDPOINT",
                "http://otel-collector:4317",
            ),
            otel_sample_ratio=otel_sample_ratio,
            otel_fastapi_excluded_urls=os.getenv(
                "GATEWAY_OTEL_FASTAPI_EXCLUDED_URLS",
                DEFAULT_OTEL_FASTAPI_EXCLUDED_URLS,
            ),

            # Session state.
            sessions_enabled=_get_bool_env("GATEWAY_SESSIONS_ENABLED", True),
            valkey_url=valkey_url,
            session_runtime_valkey_url=_valkey_url_for_db(valkey_url, 0),
            session_key_prefix=os.getenv(
                "GATEWAY_SESSION_KEY_PREFIX",
                "llm-gateway:session:",
            ),
            session_ttl_sec=int(os.getenv("GATEWAY_SESSION_TTL", "180")),
            session_tracker_max_connections=int(
                os.getenv("GATEWAY_SESSION_TRACKER_MAX_CONNECTIONS", "256")
            ),
            valkey_breaker_failures=int(
                os.getenv("GATEWAY_VALKEY_BREAKER_FAILURES", "3")
            ),
            valkey_breaker_cooldown_sec=float(
                os.getenv("GATEWAY_VALKEY_BREAKER_COOLDOWN_SEC", "30")
            ),

            # Persisted chat sessions.
            session_store_valkey_url=_valkey_url_for_db(valkey_url, 1),
            session_store_key_prefix=os.getenv(
                "GATEWAY_SESSION_STORE_KEY_PREFIX",
                "llm-gateway:session-store:",
            ),
            session_store_ttl_sec=int(os.getenv("GATEWAY_SESSION_STORE_TTL", "1296000")),
            session_store_max_connections=int(
                os.getenv("GATEWAY_SESSION_STORE_MAX_CONNECTIONS", "256")
            ),
            session_store_max_record_bytes=int(
                os.getenv("GATEWAY_SESSION_STORE_MAX_RECORD_BYTES", "4194304")
            ),
            session_store_write_attempts=int(
                os.getenv("GATEWAY_SESSION_STORE_WRITE_ATTEMPTS", "5")
            ),

            # Backend draining independent of the caller.
            drain_after_disconnect=_get_bool_env("GATEWAY_DRAIN_AFTER_DISCONNECT", True),
            drain_timeout_sec=float(os.getenv("GATEWAY_DRAIN_TIMEOUT_SEC", "300")),
            drain_max_bytes=int(os.getenv("GATEWAY_DRAIN_MAX_BYTES", "33554432")),
            force_stream_usage=_get_bool_env("GATEWAY_FORCE_STREAM_USAGE", True),
        )
