# Deployment Notes

This document describes the split deployment layout:

- `deploy/llm` runs exactly one LLM backend stack: vLLM or SGLang.
- `deploy/gateway` runs the OpenAI-compatible gateway and gateway observability
  plumbing.

Metrics are documented in [METRICS.md](METRICS.md), and tracing is documented
in [TRACES.md](TRACES.md). Dashboard JSON exports are documented in
[DASHBOARDS.md](DASHBOARDS.md).

All paths in this document are relative to the repository root unless a command
changes directory explicitly.

## LLM Stack

The LLM stack contains:

- the selected LLM engine;
- Prometheus scraping backend metrics, node metrics, and DCGM GPU metrics;
- Node exporter;
- DCGM exporter.

The gateway is intentionally not part of this compose stack.

Create local LLM settings:

```bash
cp deploy/llm/.env.example deploy/llm/.env
```

Then edit `deploy/llm/.env`:

- set the local model path;
- choose image tags;
- keep `LLM_HOST=127.0.0.1` and `LLM_HTTP_PORT=9900` unless you want the backend
  API exposed differently.

Start vLLM:

```bash
cd deploy/llm
docker compose -f docker-compose.vllm.yaml up -d
```

Start SGLang instead:

```bash
cd deploy/llm
docker compose -f docker-compose.sglang.yaml up -d
```

Only one backend variant should bind `127.0.0.1:9900` at a time.

LLM-side useful URLs:

- LLM OpenAI-compatible API: `http://127.0.0.1:9900`
- LLM Prometheus: `http://0.0.0.0:9191`

Prometheus configs:

- vLLM: `deploy/llm/configs/prometheus-vllm.yaml`
- SGLang: `deploy/llm/configs/prometheus-sglang.yaml`

The launch scripts are:

- `deploy/llm/serve_vllm.sh`
- `deploy/llm/serve_sglang.sh`

## Gateway Stack

The gateway stack contains:

- the FastAPI gateway;
- Valkey for runtime session tracking and persisted chat inspection;
- Prometheus scraping only gateway metrics;
- Loki for gateway structured events;
- OpenTelemetry Collector;
- Tempo.

The LLM backend is intentionally not part of this compose stack. By default, the
gateway calls:

```text
GATEWAY_BACKEND_BASE_URL=http://host.docker.gateway:9900
```

That matches the default LLM stack binding. Change it in
`deploy/gateway/.env` when the backend lives elsewhere.

Create local gateway settings:

```bash
cp deploy/gateway/.env.example deploy/gateway/.env
```

Then start the gateway stack:

```bash
cd deploy/gateway
docker compose -f docker-compose.yaml up -d
```

Gateway-side useful URLs:

- gateway: `http://0.0.0.0:9090`
- gateway health: `http://0.0.0.0:9090/health`
- gateway metrics endpoint: `http://0.0.0.0:9090/gateway/metrics`
- gateway Prometheus: `http://0.0.0.0:9091`
- Loki: `http://0.0.0.0:9092`
- Tempo: `http://0.0.0.0:3200`
- OTLP/gRPC collector endpoint: `0.0.0.0:4317`
- OTLP/HTTP collector endpoint: `0.0.0.0:4318`

Gateway configs:

- Prometheus: `deploy/gateway/configs/prometheus-gateway.yaml`
- Loki: `deploy/gateway/configs/loki-config.yaml`
- Valkey: `deploy/gateway/configs/valkey.conf`
- OpenTelemetry Collector: `deploy/gateway/configs/otel-collector.yaml`
- Tempo: `deploy/gateway/configs/tempo.yaml`

Grafana is not part of the compose stack. Import dashboard JSON files from
`observability/dashboards/` into an existing Grafana or managed observability
workspace when a visual UI is needed.

## Validation

Render compose configs:

```bash
cd deploy/llm
docker compose --env-file .env.example -f docker-compose.vllm.yaml config
docker compose --env-file .env.example -f docker-compose.sglang.yaml config

cd ../gateway
docker compose --env-file .env.example -f docker-compose.yaml config
```

Smoke checks after startup:

```bash
curl -fsS http://127.0.0.1:9900/v1/models
curl -fsS http://127.0.0.1:9090/health
curl -fsS http://127.0.0.1:9090/gateway/metrics
curl -fsS http://127.0.0.1:9090/v1/models
```

`http://127.0.0.1:9090/metrics` is intentionally not served by the gateway.

## Compose Smoke Tests

The compose files include optional test-runner services under the `test`
profile. They send one non-streaming OpenAI-compatible chat completion request
and fail when the backend/gateway does not return a valid answer.

Run direct backend smoke tests from `deploy/llm`:

```bash
docker compose \
  --env-file .env \
  -f docker-compose.vllm.yaml \
  --profile test \
  up --build --abort-on-container-exit --exit-code-from llm-smoke-tests
```

```bash
docker compose \
  --env-file .env \
  -f docker-compose.sglang.yaml \
  --profile test \
  up --build --abort-on-container-exit --exit-code-from llm-smoke-tests
```

Run gateway smoke tests from `deploy/gateway` after the LLM stack is reachable
through `GATEWAY_BACKEND_BASE_URL`:

```bash
docker compose \
  --env-file .env \
  -f docker-compose.yaml \
  --profile test \
  up --build --abort-on-container-exit --exit-code-from gateway-smoke-tests
```

The prompt, model, timeout, and optional API key are passed to the smoke test
container through `env_file: .env`:

- `SMOKE_MODEL`
- `SMOKE_PROMPT`
- `SMOKE_TIMEOUT_SEC`
- `SMOKE_API_KEY`
