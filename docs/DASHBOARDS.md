# Dashboards

Grafana is intentionally not part of the deployment stack. Dashboard JSON
exports are kept in `observability/dashboards/` so they can be imported into an
existing Grafana instance or another managed observability workspace.

## Files

`gateway-prometheus-overview.json`

- Gateway-only Prometheus dashboard.
- Uses `gateway_*` metrics from `GET /gateway/metrics`.
- Includes filters for route, model, stream mode, result, status family, and
  first-in-session classification.

`gateway-loki-events.json`

- Gateway structured Loki event dashboard.
- Uses Loki stream labels `app`, `bucket`, and `route`.
- Shows generation requests, generation responses, warning responses, and
  gateway errors.

`gateway-tempo-traces.json`

- Gateway Tempo TraceQL lookup dashboard.
- Uses current span names: `llm.gateway.request`, `llm.backend.request`,
  `llm.stream_response`, `llm.session.flow`, and `valkey.operation`.
- Filters by service, route, model, session id, request id, and latency
  thresholds.

`backend-vllm-prometheus-overview.json`

- vLLM backend, DCGM, and node-exporter dashboard.
- Intended for the LLM-side Prometheus stack in `deploy/llm`.
- Includes the per-vCPU host load panel.

`backend-sglang-prometheus-overview.json`

- SGLang backend, DCGM, and node-exporter dashboard.
- Intended for the LLM-side Prometheus stack in `deploy/llm`.

## Datasource UIDs

The dashboards are templated. On import, select the matching datasource
variables:

- `DS_PROMETHEUS` for gateway or LLM Prometheus;
- `DS_LOKI` for gateway Loki;
- `DS_TEMPO` for gateway Tempo.

The backend dashboards assume the LLM-side Prometheus scrapes the selected
backend together with Node exporter and DCGM exporter.
