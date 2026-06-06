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

The gateway no longer exposes `/metrics`. The LLM compose stack has its own
Prometheus for backend metrics, and the gateway compose stack has a separate
Prometheus for gateway metrics.

## Layout

```text
gateway/          FastAPI gateway code
deploy/llm        LLM engine compose files, launch scripts, backend metrics
deploy/gateway    gateway compose file, Loki, Valkey, Prometheus, Tempo, OTEL
observability/    dashboard JSON exports for existing Grafana/workspace imports
docs/             deployment, metrics, and tracing reference
```

Detailed references:

- [Deployment](docs/DEPLOY.md)
- [Metrics](docs/METRICS.md)
- [Traces](docs/TRACES.md)
- [Dashboards](docs/DASHBOARDS.md)

## Running

Create deployment settings first:

```bash
cp deploy/llm/.env.example deploy/llm/.env
# edit deploy/llm/.env

cp deploy/gateway/.env.example deploy/gateway/.env
# edit deploy/gateway/.env
```

vLLM variant:

```bash
cd deploy/llm
docker compose -f docker-compose.vllm.yaml up -d
```

SGLang variant:

```bash
cd deploy/llm
docker compose -f docker-compose.sglang.yaml up -d
```

Gateway stack:

```bash
cd deploy/gateway
docker compose -f docker-compose.yaml up -d
```

Optional smoke tests are available through the compose `test` profile. They send
one chat completion request directly to the selected backend or through the
gateway and return the test container exit code. See [Deployment](docs/DEPLOY.md)
for commands.

Default ports:

- LLM API: `http://127.0.0.1:9900`
- LLM Prometheus: `http://0.0.0.0:9191`
- gateway: `http://0.0.0.0:9090`
- gateway metrics endpoint: `http://0.0.0.0:9090/gateway/metrics`
- gateway Prometheus: `http://0.0.0.0:9091`
- Loki: `http://0.0.0.0:9092`
- Tempo: `http://0.0.0.0:3200`

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
| `GATEWAY_BACKEND_BASE_URL` | OpenAI-compatible backend base URL | `http://host.docker.gateway:9900` |
| `GATEWAY_FORCED_MAX_COMPLETION_TOKENS` | Optional forced `max_completion_tokens` on chat requests | unset |
| `GATEWAY_FORCED_THINKING_DISABLED` | Force `chat_template_kwargs.enable_thinking=false` on JSON request payloads | `false` |
| `GATEWAY_ENABLE_SAMPLING_FALLBACK_OVERRIDE` | Replace invalid chat sampling parameters with safe fallback values | `false` |
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
`session_first_request`, `trace_id`, and `span_id`.
Request generation events include sanitized `request_json` without message
bodies, `tool_call_count`, and `fallback_params` when invalid sampling values
were replaced.
Generation response events keep the backend payload, `assistant_text`, timing,
status, and size fields.
Streaming generation responses are stored as a valid JSON object containing ordered SSE events.
Non-generation response events include sanitized JSON payloads when the backend returns JSON.
Sensitive headers such as `Authorization`, cookies, and API keys are redacted.

## Traces

When `GATEWAY_OTEL_ENABLED=true`, FastAPI instrumentation emits HTTP spans for
non-excluded routes. The chat completion path also emits custom domain spans:

- `llm.gateway.request` for the full chat completion gateway request;
- `llm.backend.request` for the backend call;
- `llm.session.flow` for gateway-side session handling;
- `valkey.operation` for session-layer Valkey operations;
- `llm.stream_response` for streaming response iteration.

The gateway compose stack provides Tempo and the OTEL Collector. See
[Traces](docs/TRACES.md) for span attributes, error semantics, and lookup tips.
