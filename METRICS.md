# Metrics

The gateway exposes only its own metrics:

```text
GET /gateway/metrics
```

It does not proxy backend `/metrics`. Prometheus scrapes backend metrics
directly from the selected backend service.

## Gateway Metrics

`gateway_proxy_requests_total`

- Counter for all gateway-handled requests.
- Labels: `route`, `method`, `stream`.

`gateway_proxy_request_latency_seconds`

- End-to-end gateway latency.
- Labels: `route`, `method`, `stream`.

`gateway_proxy_loki_push_total`

- Loki push attempts.
- Labels: `status`.

`gateway_proxy_loki_events_dropped_total`

- Loki events dropped before delivery.
- Labels: `reason`.

`gateway_session_requests_total`

- Session-aware `/v1/chat/completions` requests.
- Labels: `route`, `method`, `stream`, `session_present`,
  `session_first_request`.

`gateway_session_tracker_errors_total`

- Valkey/Redis session tracker failures.
- Labels: `operation`, `error_type`.

`gateway_session_init_ttft_seconds`

- Time from gateway request start to first non-empty streamed backend chunk for
  the first observed request in a session.
- Labels: `route`, `method`, `stream`, `model`, `status_family`, `result`.

`gateway_session_init_e2e_latency_seconds`

- End-to-end latency for the first observed request in a session.
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
