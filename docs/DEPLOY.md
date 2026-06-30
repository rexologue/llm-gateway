# Deployment Notes

This document describes the split deployment layout:

- `deploy/llm` runs exactly one LLM backend stack: vLLM or SGLang.
- `deploy/gateway` runs the OpenAI-compatible gateway and gateway observability
  plumbing.

The recommended deployment flow is backend-first:

1. Start the selected backend stack.
2. Validate the backend directly.
3. Start the gateway stack.
4. Validate the gateway path against the already checked backend.

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

Start vLLM and run the direct backend smoke test:

```bash
cd deploy/llm
docker compose --env-file .env -f docker-compose.vllm.yaml up -d --build
docker compose --env-file .env -f docker-compose.vllm.yaml --profile test run --rm llm-smoke-tests
```

Start SGLang instead and run the direct backend smoke test:

```bash
cd deploy/llm
docker compose --env-file .env -f docker-compose.sglang.yaml up -d --build
docker compose --env-file .env -f docker-compose.sglang.yaml --profile test run --rm llm-smoke-tests
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

Then start the gateway stack and run the gateway smoke test:

```bash
cd deploy/gateway
docker compose --env-file .env -f docker-compose.yaml up -d --build
docker compose --env-file .env -f docker-compose.yaml --profile test run --rm gateway-smoke-tests
```

Gateway-side useful URLs:

- gateway: `http://0.0.0.0:9090`
- gateway health: `http://0.0.0.0:9090/health`
- gateway metrics endpoint: `http://0.0.0.0:9090/gateway/metrics`
- gateway Prometheus: `http://0.0.0.0:9091`
- Loki: `http://0.0.0.0:9092`
- Tempo: `http://0.0.0.0:3200`
- OTLP/gRPC collector endpoint: `0.0.0.0:4317`

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
and fail when the backend/gateway does not return a valid answer. When
`SMOKE_CHECK_TOOLS=true`, they also send a forced OpenAI-compatible tool-call
request and fail unless the response contains valid `tool_calls` with JSON
function arguments.

The normal workflow is:

1. Start the backend stack in detached mode.
2. Run the backend smoke-test service as a one-off container.
3. Start the gateway stack in detached mode.
4. Run the gateway smoke-test service as a one-off container.
5. Keep the stacks running after the test containers exit.

Run direct backend smoke tests from `deploy/llm`.

For vLLM:

```bash
docker compose \
  --env-file .env \
  -f docker-compose.vllm.yaml \
  up -d --build

docker compose \
  --env-file .env \
  -f docker-compose.vllm.yaml \
  --profile test \
  run --rm llm-smoke-tests
```

One-command variant:

```bash
docker compose --env-file .env -f docker-compose.vllm.yaml up -d --build && docker compose --env-file .env -f docker-compose.vllm.yaml --profile test run --rm llm-smoke-tests
```

For SGLang:

```bash
docker compose \
  --env-file .env \
  -f docker-compose.sglang.yaml \
  up -d --build

docker compose \
  --env-file .env \
  -f docker-compose.sglang.yaml \
  --profile test \
  run --rm llm-smoke-tests
```

One-command variant:

```bash
docker compose --env-file .env -f docker-compose.sglang.yaml up -d --build && docker compose --env-file .env -f docker-compose.sglang.yaml --profile test run --rm llm-smoke-tests
```

Run gateway smoke tests from `deploy/gateway` after the LLM stack is reachable
through `GATEWAY_BACKEND_BASE_URL`:

```bash
docker compose \
  --env-file .env \
  -f docker-compose.yaml \
  up -d --build

docker compose \
  --env-file .env \
  -f docker-compose.yaml \
  --profile test \
  run --rm gateway-smoke-tests
```

One-command variant:

```bash
docker compose --env-file .env -f docker-compose.yaml up -d --build && docker compose --env-file .env -f docker-compose.yaml --profile test run --rm gateway-smoke-tests
```

`run --rm` returns the pytest exit code and removes only the finished test
container. It does not stop the backend, gateway, Prometheus, Loki, Tempo, or
exporters.

The prompt, model, timeout, and optional API key are passed to the smoke test
container through `env_file: .env`:

- `SMOKE_MODEL`
- `SMOKE_PROMPT`
- `SMOKE_TIMEOUT_SEC`
- `SMOKE_API_KEY`
- `SMOKE_CHECK_TOOLS`

Set `SMOKE_CHECK_TOOLS=true` only when tool calling is part of the expected
runtime contract and the selected backend was launched with tool-call support.
Leave it `false` for deployments that only need plain chat completions.
