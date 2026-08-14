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

`gateway-session-viewer.json`

- Full-dialog viewer for a single persisted chat session.
- Backed entirely by the gateway (no Loki): the `Stored sessions` list calls
  `GET /gateway/session_list` and the detail panels call
  `GET /gateway/session/{session_id}`, both through the Infinity datasource with
  relative urls.
- Stored records are `{metadata, tools, messages}`. `messages` is shown as raw
  indented JSON and holds the whole dialog, including assistant `tool_calls`
  made during the session, the `role: "tool"` results they returned, and the
  final assistant turn. `tools` shows the declared tool schemas.
- `metadata` reports `created_at`/`updated_at` plus read-time durations:
  `age_sec` (lifetime since the first request), `idle_sec` (since the last
  request), and `expires_in_sec` (remaining TTL). Session lifetime comes from
  `created_at`, not the TTL, because the TTL is reset on every request.
- Enter a `session_id` in the textbox, or click a row in `Stored sessions`.

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
- `DS_TEMPO` for gateway Tempo;
- `DS_INFINITY` for the session viewer (`yesoreyeram-infinity-datasource`).

The session viewer needs the Infinity datasource plugin installed. Set its
**Base URL** to the gateway root as seen from the Grafana backend (for example
`http://llm-gateway:8080`) and add that host under Infinity →
Security → Allowed hosts. The `/gateway/session_*` routes are unauthenticated,
so expose them only where that is acceptable.

The backend dashboards assume the LLM-side Prometheus scrapes the selected
backend together with Node exporter and DCGM exporter.
