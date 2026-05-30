# Metrics

The gateway exposes only its own Prometheus metrics:

```text
GET /gateway/metrics
```

It does not proxy backend `/metrics`. Prometheus scrapes backend metrics
directly from the selected backend service.

Use metrics for aggregate behavior and alerting. Use [TRACES.md](TRACES.md) for
per-request timing and span-level investigation.

## Gateway Metrics

`gateway_requests_total`

- Counter for all requests accepted by gateway route handlers.
- Increments once after the gateway determines route and stream mode.
- Includes malformed JSON chat requests that are answered directly by the
  gateway with `400`.
- Labels: `route`, `method`, `stream`.

`gateway_responses_total`

- Counter for terminal gateway outcomes.
- `status_family` is a coarse HTTP family such as `2xx`, `4xx`, `5xx`, or
  `unknown` when no response status was available.
- `result` is `success`, `error`, or `cancelled`.
- Labels: `route`, `method`, `stream`, `status_family`, `result`.

`gateway_request_e2e_seconds`

- End-to-end gateway latency from request receipt until the final non-streaming
  response is ready, a streaming response is fully iterated, or the request
  terminates with an error/cancellation.
- For streaming requests, this measures full stream duration, not TTFT.
- Labels: `route`, `method`, `stream`, `model`, `status_family`, `result`.

`gateway_request_ttft_seconds`

- Time from gateway request start to the first non-empty streamed backend chunk.
- Recorded for streamed responses when a first non-empty chunk is observed.
- Labels: `route`, `method`, `stream`, `model`, `status_family`, `result`.

`gateway_session_requests_total`

- Counter for `/v1/chat/completions` requests with session classification.
- `session_present=false` means the request did not include `X-Session-ID`.
- `session_first_request=true` means the runtime session tracker did not see
  this session id in Valkey DB 0 before this request.
- Labels: `route`, `method`, `stream`, `session_present`,
  `session_first_request`.

`gateway_session_request_e2e_seconds`

- End-to-end latency for `/v1/chat/completions` requests with session
  classification.
- Recorded for ordinary and first-in-session chat completion requests. Use
  `session_first_request=true` to isolate session-init behavior.
- Labels: `route`, `method`, `stream`, `model`, `session_first_request`,
  `status_family`, `result`.

`gateway_session_request_ttft_seconds`

- TTFT for streamed `/v1/chat/completions` requests with session classification.
- Recorded when a non-empty streamed backend chunk is observed. Use
  `session_first_request=true` to isolate session-init TTFT.
- Labels: `route`, `method`, `stream`, `model`, `session_first_request`,
  `status_family`, `result`.

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

`gateway_loki_push_total`

- Number of batch push attempts made by the gateway Loki publisher.
- A successful push means Loki accepted the batch. It does not imply that a
  particular request produced a log event.
- Labels: `status`.

`gateway_loki_events_dropped_total`

- Number of log events dropped before they reached Loki.
- Typical reason: local queue pressure.
- Labels: `reason`.

The gateway intentionally does not use `session_id`, `request_id`, `trace_id`,
or `span_id` as Prometheus labels.

## Backend Metrics

Backend metrics are backend-specific and are scraped directly:

- vLLM variant: `vllm:8000/metrics` through `configs/prometheus-vllm.yml`.
- SGLang variant: `sglang:30000/metrics` through
  `configs/prometheus-sglang.yml`.

This keeps the gateway invariant to backend implementation while still allowing
Prometheus dashboards to use engine-specific metrics.
