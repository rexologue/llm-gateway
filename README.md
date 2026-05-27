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
docs/             deployment, metrics, and tracing reference
observability/    Tempo, OpenTelemetry Collector, and Grafana stack
```

Detailed references:

- [Deployment](docs/DEPLOY.md)
- [Metrics](docs/METRICS.md)
- [Traces](docs/TRACES.md)

## Running

Create deployment settings first:

```bash
cp deploy/.env.example deploy/.env
# edit deploy/.env
cp observability/.env.example observability/.env
# edit observability/.env
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
cd observability
docker compose -f docker-compose.yaml up -d
```

Grafana loads the `Gateway Overview` dashboard from provisioning on startup.

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
| `GATEWAY_FORCED_MAX_COMPLETION_TOKENS` | Optional forced `max_completion_tokens` on chat requests | unset |
| `GATEWAY_FORCED_THINKING_DISABLED` | Force `enable_thinking=false` on JSON request payloads | `false` |
| `GATEWAY_LOKI_APP_NAME` | Loki `app` label | `llm-gateway` |
| `GATEWAY_LOKI_ENABLED` | Enable Loki event delivery | `true` |
| `GATEWAY_LOKI_PUSH_URL` | Loki Push API URL | `http://llm-gateway-loki:3100/loki/api/v1/push` |
| `GATEWAY_OTEL_ENABLED` | Enable OpenTelemetry tracing | `false` |
| `GATEWAY_VALKEY_URL` | Valkey/Redis base URL; runtime uses DB 0 and stored chats use DB 1 | `redis://llm-gateway-valkey:6379` |
| `GATEWAY_SESSION_TTL` | Sliding session TTL in seconds | `21600` |
| `GATEWAY_SESSION_STORE_TTL` | Stored chat session TTL in seconds | `1296000` |

## Sessions

When a `/v1/chat/completions` request includes `X-Session-ID`, the gateway stores
that request's `messages` array in Valkey DB 1. Later requests with the same
session id overwrite the stored record and refresh the TTL.

- `GET /gateway/session_list` returns all stored session ids.
- `GET /gateway/session/{session_id}` returns one stored session record.

## Logs

Loki event buckets:

- `request_generation` for `/v1/chat/completions` requests;
- `request_non_generation` for other `/v1/*` requests;
- `response_generation` for `/v1/chat/completions` responses that contain a model answer;
- `response_non_generation` for all other backend responses;
- `gateway_error` for failures before a backend response exists.

Request and response events include `request_id`, optional `session_id`,
`session_present`, `session_first_request`, `trace_id`, and `span_id`.
Request generation events include `request_json` and `message_cnt` without logging message bodies.
Generation response events keep the backend payload, `assistant_text`, timing, status, size, and hash fields.
Streaming generation responses are stored as a valid JSON object containing ordered SSE events.
Non-generation response events include sanitized JSON payloads when the backend returns JSON.
Sensitive headers such as `Authorization`, cookies, and API keys are redacted.

## Traces

When `GATEWAY_OTEL_ENABLED=true`, FastAPI instrumentation emits HTTP spans for
non-excluded routes. The chat completion path also emits custom domain spans:

- `llm.gateway.request` for the full chat completion gateway request;
- `llm.backend.request` for the backend call;
- `llm.stream_response` for streaming response iteration.

The observability stack in `observability/` provides Tempo and Grafana. See
[Traces](docs/TRACES.md) for span attributes, error semantics, and lookup tips.
