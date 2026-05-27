# Deployment Notes

The project has three deployment entry points:

- `docker-compose.yaml` - backend-neutral gateway stack.
- `deploy/docker-compose.vllm.yaml` - gateway plus a vLLM backend.
- `deploy/docker-compose.sglang.yaml` - gateway plus an SGLang backend.

The trace/Grafana stack is separate:

- `observability/docker-compose.yaml` - Tempo, OpenTelemetry Collector, Grafana.

## Backend-Neutral Stack

Run the gateway against any OpenAI-compatible backend:

```bash
GATEWAY_BACKEND_BASE_URL=http://host.docker.internal:8000 docker compose up -d
```

This stack starts:

- `llm-gateway`
- `llm-gateway-valkey`
- `llm-gateway-loki`
- `llm-prometheus`
- host and GPU exporters

Prometheus scrapes only gateway, host, and GPU metrics in this generic stack.
Backend metrics must be added through a backend-specific scrape config.

## vLLM Variant

```bash
docker compose -f deploy/docker-compose.vllm.yaml up -d
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
docker compose -f deploy/docker-compose.sglang.yaml up -d
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
docker compose -f observability/docker-compose.yaml up -d
```

The gateway variants send OTLP traces to `host.docker.internal:4317`, so this
stack can run independently from the backend-specific compose file.

Useful URLs:

- gateway: `http://127.0.0.1:9090`
- gateway metrics: `http://127.0.0.1:9090/gateway/metrics`
- Prometheus: `http://127.0.0.1:9091`
- Loki: `http://127.0.0.1:9092`
- Grafana: `http://127.0.0.1:3000`
- Tempo: `http://127.0.0.1:3200`

## Validation

```bash
docker compose config
docker compose -f deploy/docker-compose.vllm.yaml config
docker compose -f deploy/docker-compose.sglang.yaml config
docker compose -f observability/docker-compose.yaml config
```

Smoke checks after startup:

```bash
curl -fsS http://127.0.0.1:9090/healthz
curl -fsS http://127.0.0.1:9090/gateway/metrics
curl -fsS http://127.0.0.1:9090/v1/models
```

`http://127.0.0.1:9090/metrics` is intentionally not served by the gateway.
