# Metrics

The gateway exposes only its own metrics:

```text
GET /gateway/metrics
```

It does not proxy backend `/metrics`. Prometheus scrapes backend metrics
directly from the selected backend service.

Use metrics for aggregate behavior and alerting. Use [TRACES.md](TRACES.md) for
per-request timing and span-level investigation.

## Gateway Metrics

`gateway_proxy_requests_total`

- Counter for all requests accepted by the gateway route handlers.
- Increments once per request after the gateway determines the logical route and
  stream mode.
- It includes malformed JSON chat requests that are answered directly by the
  gateway with `400`.
- Labels: `route`, `method`, `stream`.

`gateway_proxy_request_latency_seconds`

- End-to-end gateway latency from request receipt until the gateway has either
  built the final non-streaming response or finished iterating a streaming
  response.
- For streaming requests, this measures the full stream duration, not TTFT.
- Failed backend calls and client cancellations are still observed.
- Labels: `route`, `method`, `stream`.

`gateway_proxy_responses_total`

- Counter for completed gateway proxy outcomes.
- `status_family` is a coarse HTTP family such as `2xx`, `4xx`, `5xx`, or
  `unknown` when no response status was available.
- `result` is `success`, `error`, or `cancelled`.
- This metric is intended for dashboard error-rate panels without adding
  high-cardinality labels.
- Labels: `route`, `method`, `stream`, `status_family`, `result`.

`gateway_proxy_loki_push_total`

- Number of batch push attempts made by the gateway Loki sink.
- A successful push means Loki accepted the batch. It does not imply that a
  particular request produced a log event.
- Labels: `status`.

`gateway_proxy_loki_events_dropped_total`

- Number of log events dropped before they reached Loki.
- Typical reasons are local queue pressure or delivery failures after retry
  limits.
- Labels: `reason`.

`gateway_session_requests_total`

- Counter for `/v1/chat/completions` requests with session classification.
- `session_present=false` means the request did not include `X-Session-ID`.
- `session_first_request=true` means the runtime session tracker did not see
  this session id in Valkey DB 0 before this request.
- This metric is about runtime session observation, not the persisted chat
  history stored in Valkey DB 1.
- Labels: `route`, `method`, `stream`, `session_present`,
  `session_first_request`.

`gateway_active_sessions`

- Gauge for the current number of runtime session keys in Valkey DB 0.
- The value is refreshed when Prometheus scrapes `GET /gateway/metrics`.
- It follows the runtime session TTL, not the persisted chat history TTL in
  Valkey DB 1.
- Labels: none.

`gateway_session_tracker_errors_total`

- Valkey/Redis failures while checking, creating, or refreshing runtime session
  keys in DB 0.
- These errors affect first-request classification and session TTL refresh, but
  do not by themselves mean the backend generation failed.
- Labels: `operation`, `error_type`.

`gateway_session_init_ttft_seconds`

- Time from gateway request start to the first non-empty streamed backend chunk
  for the first observed request in a session.
- Recorded only for streaming `/v1/chat/completions` requests where
  `session_first_request=true` and a non-empty chunk is observed.
- This metric is intentionally absent for non-streaming requests because the
  gateway cannot observe token-level TTFT without streaming.
- Labels: `route`, `method`, `stream`, `model`, `status_family`, `result`.

`gateway_session_init_e2e_latency_seconds`

- End-to-end latency for the first observed `/v1/chat/completions` request in a
  session.
- Recorded for both streaming and non-streaming requests when
  `session_first_request=true`.
- `result` is derived from gateway/backend outcome: success, error, or
  cancelled.
- Labels: `route`, `method`, `stream`, `model`, `status_family`, `result`.

The gateway intentionally does not use `session_id`, `request_id`, `trace_id`,
or `span_id` as Prometheus labels.

## Backend Metrics

Backend metrics are backend-specific and are scraped directly:

- vLLM variant: `vllm:8000/metrics` through `configs/prometheus-vllm.yml`.
- SGLang variant: `sglang:30000/metrics` through
  `configs/prometheus-sglang.yml`.

This keeps the gateway invariant to backend implementation while still allowing
Prometheus dashboards to use engine-specific metrics.
