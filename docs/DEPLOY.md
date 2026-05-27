# Deployment Notes

This document describes how to start the backend-specific gateway stacks and
the separate observability stack. Metrics are documented in
[METRICS.md](METRICS.md), and tracing is documented in [TRACES.md](TRACES.md).

The project has two backend deployment entry points:

- `deploy/docker-compose.vllm.yaml` - gateway plus a vLLM backend.
- `deploy/docker-compose.sglang.yaml` - gateway plus an SGLang backend.

The trace/Grafana stack is separate:

- `observability/docker-compose.yaml` - Tempo, OpenTelemetry Collector, Grafana.

All paths in this document are relative to the repository root unless a command
changes directory explicitly.

Before starting a backend stack, create a local deployment env file:

```bash
cp deploy/.env.example deploy/.env
```

Then edit `deploy/.env` and replace dummy values such as model paths, ports, image
tags, and backend URLs for your host.

Before starting the observability stack, create its local env file:

```bash
cp observability/.env.example observability/.env
```

Then edit `observability/.env` and replace dummy values such as ports, image
tags, and Grafana credentials for your host.

## vLLM Variant

```bash
cd deploy
docker compose -f docker-compose.vllm.yaml up -d
```

This variant sets:

```text
GATEWAY_BACKEND_BASE_URL=http://vllm:8000
```

Prometheus uses `configs/prometheus-vllm.yml` and scrapes:

- `llm-gateway:8080/gateway/metrics`
- `vllm:8000/metrics`
- node exporter
- DCGM exporter

The vLLM launch script is `deploy/serve_vllm.sh`.

## SGLang Variant

```bash
cd deploy
docker compose -f docker-compose.sglang.yaml up -d
```

This variant sets:

```text
GATEWAY_BACKEND_BASE_URL=http://sglang:30000
```

Prometheus uses `configs/prometheus-sglang.yml` and scrapes:

- `llm-gateway:8080/gateway/metrics`
- `sglang:30000/metrics`
- node exporter
- DCGM exporter

The SGLang launch script is `deploy/serve_sglang.sh`.

## Observability Stack

```bash
cd observability
docker compose -f docker-compose.yaml up -d
```

The gateway variants send OTLP traces to `host.docker.internal:4317`, so this
stack can run independently from the backend-specific compose file. Keep
`GATEWAY_OTEL_ENABLED=true` and
`GATEWAY_OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4317` in
`deploy/.env` when you want traces to appear in Tempo.

Useful URLs:

- gateway: `http://127.0.0.1:9090`
- gateway metrics: `http://127.0.0.1:9090/gateway/metrics`
- Prometheus: `http://127.0.0.1:9091`
- Loki: `http://127.0.0.1:9092`
- Grafana: `http://127.0.0.1:3000`
- Tempo: `http://127.0.0.1:3200`

Grafana provisions the `Gateway Overview` dashboard at startup from
`observability/configs/grafana/provisioning/dashboards/json/gateway-overview.json`.
The dashboard uses only the Prometheus datasource and only metrics exported by
the gateway itself.

## Validation

```bash
cd deploy
docker compose -f docker-compose.vllm.yaml config
docker compose -f docker-compose.sglang.yaml config
cd ..
cd observability
docker compose -f docker-compose.yaml config
```

Smoke checks after startup:

```bash
curl -fsS http://127.0.0.1:9090/healthz
curl -fsS http://127.0.0.1:9090/gateway/metrics
curl -fsS http://127.0.0.1:9090/v1/models
```

`http://127.0.0.1:9090/metrics` is intentionally not served by the gateway.
