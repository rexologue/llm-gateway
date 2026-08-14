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
- Stored records are `{metadata, tools, messages}`. The `Dialog` and
  `Declared tools` panels call `GET /gateway/session/{id}?pretty=1`, which adds
  `messages_pretty`/`tools_pretty` (indented-JSON strings) to the response, and
  render them with wrapped table cells so the indentation and newlines survive.
  (Grafana's structural JSON cell collapses Infinity's parsed value onto one
  line, which is why the pretty strings are used instead.) Use a cell's inspect
  icon for a full-height view.
- `messages` holds the whole dialog: system/user/assistant turns, assistant
  `tool_calls` made during the session, the `role: "tool"` results they
  returned, and the final assistant turn. `tools` shows the declared schemas.
- `metadata` reports `created_at`/`updated_at` plus read-time durations:
  `age_sec` (lifetime since the first request), `idle_sec` (since the last
  request), and `expires_in_sec` (remaining TTL). Session lifetime comes from
  `created_at`, not the TTL, because the TTL is reset on every request.
- Legacy flat records (written before the `{metadata, tools, messages}` shape)
  are normalized on read, so old sessions still render — with empty `tools` and
  no `created_at`/`age_sec`.
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

The session viewer needs the Infinity datasource, which must be created and
configured before importing `gateway-session-viewer.json`. See
[Session Viewer setup (Infinity)](#session-viewer-setup-infinity) below.

The backend dashboards assume the LLM-side Prometheus scrapes the selected
backend together with Node exporter and DCGM exporter.

## Session Viewer setup (Infinity)

The session viewer is the only dashboard that reads from the gateway's own HTTP
API instead of Prometheus/Loki/Tempo. It uses the Infinity datasource
(`yesoreyeram-infinity-datasource`), which runs its HTTP queries from the
**Grafana backend**, not the browser. Every panel query uses a **relative** url
(`/gateway/session_list`, `/gateway/session/${session_id}`), so the gateway
address lives in one place: the datasource **Base URL**.

### 1. Install the plugin

Install the Infinity plugin (`yesoreyeram-infinity-datasource`) in Grafana —
Administration → Plugins, or `grafana-cli plugins install
yesoreyeram-infinity-datasource`, or the `GF_INSTALL_PLUGINS` env var. Restart
Grafana if prompted.

### 2. Create the datasource

Connections → Data sources → Add data source → **Infinity**. Give it a name you
will recognise when picking `DS_INFINITY` on import (e.g.
`Gateway / Session Viewer`). Leave **Default** off.

Then walk the settings tabs:

**Authentication** — select **No Auth**. The `/gateway/session_*` routes are
unauthenticated. (If you put an auth proxy in front of the gateway, pick the
matching auth type here instead.)

**URL, Headers & Params** — set **Base URL** to the gateway root as reachable
*from the Grafana server*, with no trailing path:

```
http://<gateway-host>:<GATEWAY_HTTP_PORT>
```

For example `http://host.docker.internal:9090` (`GATEWAY_HTTP_PORT` defaults to `9090`
in `deploy/gateway/.env.example`), or `http://llm-gateway:8080` when Grafana
shares the gateway's Docker network (container port is `8080`). Leave Custom
HTTP Headers and URL Query Params empty; leave the URL settings toggles off.

**Network** — the defaults are fine: Timeout `60`s, TLS/SSL toggles off for
plain `http`. If the gateway is behind HTTPS with a private CA, enable the
relevant TLS option. Proxy Mode can stay "From environment variable / Default".

**Security** — Infinity restricts which hosts it will query. Add your Base URL
host under **Allowed hosts** (click Add, paste e.g.
`http://host.docker.internal:9090`) and set **Query security** to **Deny** so Infinity
only ever talks to the gateway. Leaving it on **Warn** with an empty allow-list
also works (queries run with a warning) but is looser.

**Health check** — leave "Enable custom health check" off.

Click **Save & test**. A green "Health check successful" confirms Grafana can
reach the Base URL.

### 3. Import the dashboard

Import `gateway-session-viewer.json` and select this Infinity datasource for the
`DS_INFINITY` variable. Open the dashboard, then either type a `session_id` in
the textbox or click a row in **Stored sessions** to load a full dialog.

### Notes and troubleshooting

- **Relative urls, one address.** Panels never hardcode the host; they rely on
  Base URL. Point the viewer at a different gateway by editing only the
  datasource. Base URL must have no path (`/gateway/...` is added per panel).
- **Server-side fetch.** Base URL must resolve from the Grafana container/host,
  not from your laptop. `localhost` means the Grafana host. Prefer the Docker
  service name (`http://llm-gateway:8080`) when co-located.
- **Health check fails / empty panels.** Usually a wrong Base URL, a Query
  security block (host not in Allowed hosts while set to Deny), or the gateway
  not reachable from Grafana. Test directly:
  `curl http://<gateway-host>:<port>/gateway/session_list`.
- **`session not found` (404).** The id is not in Valkey: sessions require the
  `X-Session-ID` header on the original `/v1/chat/completions` call, and records
  expire after `GATEWAY_SESSION_STORE_TTL` (15 days by default).
- **Security.** Base URL exposes the raw stored dialogs, which can contain
  sensitive prompt/response content, to anyone with access to this datasource.
  Keep the gateway reachable only on a trusted network, or place authentication
  in front of it and configure the Authentication tab accordingly.
