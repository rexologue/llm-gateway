# OpenAI-compatible LLM gateway

This project runs a FastAPI gateway in front of any backend that exposes an
OpenAI-compatible `/v1/*` API. The gateway is intentionally backend-neutral:
vLLM and SGLang are provided only as deployment variants.

The gateway:

- proxies `/v1/chat/completions` with streaming and non-streaming support;
- proxies all other `/v1/*` routes as generic OpenAI-compatible routes;
- writes compact request/response/error events to Loki;
- exposes gateway Prometheus metrics at `/gateway/metrics`;
- emits OpenTelemetry traces to an OTLP collector when enabled;
- tracks first request in a session by `X-Session-ID` using Valkey.

The gateway no longer exposes `/metrics`. Prometheus should scrape backend
metrics directly from the backend service, for example `vllm:8000/metrics` or
`sglang:30000/metrics`.

## Layout

```text
gateway/          FastAPI gateway code
configs/          Loki, Valkey, and Prometheus scrape configs
deploy/           backend-specific compose files and launch scripts
observability/    Tempo, OpenTelemetry Collector, and Grafana stack
```

## Running

Create deployment settings first:

```bash
cp deploy/.env.example deploy/.env
# edit deploy/.env
```

vLLM variant:

```bash
cd deploy
docker compose -f docker-compose.vllm.yaml up -d
```

SGLang variant:

```bash
cd deploy
docker compose -f docker-compose.sglang.yaml up -d
```

Observability stack:

```bash
docker compose --env-file deploy/.env -f observability/docker-compose.yaml up -d
```

Default ports:

- gateway: `http://127.0.0.1:9090`
- gateway metrics: `http://127.0.0.1:9090/gateway/metrics`
- Prometheus: `http://127.0.0.1:9091`
- Loki: `http://127.0.0.1:9092`
- Grafana: `http://127.0.0.1:3000`
- Tempo: `http://127.0.0.1:3200`
- SGLang direct API in the SGLang variant: `http://127.0.0.1:9900`

## Backend Contract

The backend must provide an OpenAI-compatible HTTP API under `/v1`. The gateway
does not depend on backend-specific Python APIs or engine internals.

Required for the main path:

- `POST /v1/chat/completions`
- streaming responses using OpenAI-compatible SSE when `stream=true`

Generic proxying also supports routes such as:

- `GET /v1/models`
- `POST /v1/completions`
- `POST /v1/embeddings`
- any other `/v1/*` route supported by the backend

## Important Environment Variables

| Variable | Meaning | Default |
| --- | --- | --- |
| `GATEWAY_HOST` | Host address used by Compose port bindings | `0.0.0.0` |
| `GATEWAY_BACKEND_BASE_URL` | OpenAI-compatible backend base URL | `http://backend:8000` |
| `GATEWAY_ENABLE_MAX_COMPLETION_TOKENS_OVERRIDE` | Force `max_completion_tokens` on chat requests | `false` |
| `GATEWAY_FORCED_MAX_COMPLETION_TOKENS` | Forced value when override is enabled | `1024` |
| `GATEWAY_REQUEST_LOG_LABEL` | Loki `app` label | `llm-gateway` |
| `GATEWAY_LOKI_ENABLED` | Enable Loki event delivery | `true` |
| `GATEWAY_LOKI_PUSH_URL` | Loki Push API URL | `http://llm-gateway-loki:3100/loki/api/v1/push` |
| `GATEWAY_OTEL_ENABLED` | Enable OpenTelemetry tracing | `false` |
| `GATEWAY_SESSION_VALKEY_URL` | Valkey/Redis URL for session state | `redis://llm-gateway-valkey:6379/0` |
| `GATEWAY_SESSION_TTL` | Sliding session TTL in seconds | `21600` |

## Logs

Loki event buckets:

- `request_generation` for generation requests such as `/v1/chat/completions`;
- `request_non_generation` for other `/v1/*` requests;
- `response_backend` for backend responses;
- `gateway_error` for failures before a backend response exists.

Request and response events include `request_id`, optional `session_id`,
`session_present`, `session_first_request`, `trace_id`, and `span_id`.
Sensitive headers such as `Authorization`, cookies, and API keys are redacted.

## Traces

When `GATEWAY_OTEL_ENABLED=true`, the gateway emits:

- `llm.gateway.request` for the full gateway request;
- `llm.backend.request` for the backend call;
- `llm.stream_response` for streaming response iteration.

The observability stack in `observability/` provides Tempo and Grafana.
