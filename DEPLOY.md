# Deploy Runbook

Этот документ описывает, что именно экспортирует `vllm-gateway`, куда это попадает и каким datasource это смотреть в Grafana.

## 1. Карта Сигналов

| Что | Кто создает | Куда отправляется | Где хранится | Grafana datasource |
| --- | --- | --- | --- | --- |
| Gateway metrics | `vllm-gateway` | `/gateway/metrics` | Prometheus | `Prometheus` |
| vLLM metrics | `vLLM`, через gateway proxy | `/metrics` на gateway | Prometheus | `Prometheus` |
| Request/response logs | `vllm-gateway` | Loki Push API | Loki | `Loki` |
| Error logs | `vllm-gateway` | Loki Push API | Loki | `Loki` |
| Traces | `vllm-gateway` | OTel Collector `:4317` | Tempo | `Tempo` |
| Session state | `vllm-gateway` | Valkey | Valkey | не datasource |

Коротко:

```text
metrics -> Prometheus -> Grafana datasource Prometheus
logs    -> Loki       -> Grafana datasource Loki
traces  -> Collector  -> Tempo -> Grafana datasource Tempo
session -> Valkey     -> нужен gateway, не Grafana
```

## 2. Контейнеры

Основной `docker-compose.yaml`:

- `vllm-gateway` - принимает HTTP-запросы, пишет logs/metrics/traces
- `vllm` - upstream OpenAI-compatible API
- `vllm-gateway-valkey` - хранит `X-Session-ID` со sliding TTL
- `vllm-gateway-loki` - хранит JSON events
- `vllm-prometheus` - scrape metrics
- `vllm-hardware-node-exporter` - host metrics
- `vllm-hardware-dcgm-exporter` - GPU metrics

Отдельный `deploy/docker-compose.yaml`:

- `vllm-gateway-otel-collector` - принимает OTLP от gateway
- `vllm-gateway-tempo` - хранит traces
- `vllm-gateway-grafana` - UI для Prometheus/Loki/Tempo

## 3. Grafana Datasources

Grafana поднимается на:

```text
http://127.0.0.1:3000
```

Логин по умолчанию:

```text
admin / admin
```

Datasource `Prometheus`:

- URL внутри Grafana: `http://host.docker.internal:9091`
- показывает gateway metrics, vLLM metrics, host metrics, GPU metrics
- query language: PromQL

Datasource `Loki`:

- URL внутри Grafana: `http://host.docker.internal:9092`
- показывает request/response/error JSON events от gateway
- query language: LogQL
- настроен derived field `trace_id`, который ведет в Tempo

Datasource `Tempo`:

- URL внутри Grafana: `http://tempo:3200`
- показывает traces
- query language: TraceQL/Search

## 4. Что Смотреть В Prometheus

Все ниже смотреть через Grafana datasource `Prometheus`.

Отдельная подробная выжимка только по метрикам: [METRICS.md](./METRICS.md).

| Метрика | Что означает |
| --- | --- |
| `gateway_proxy_requests_total` | количество запросов через gateway |
| `gateway_proxy_request_latency_seconds` | E2E latency всех gateway-запросов |
| `gateway_session_requests_total` | запросы с учетом `X-Session-ID` |
| `gateway_session_id_missing_total` | запросы без `X-Session-ID` |
| `gateway_session_init_e2e_latency_seconds` | E2E первого запроса session |
| `gateway_session_init_ttft_seconds` | TTFT первого streaming-запроса session |
| `gateway_session_init_ttft_missing_total` | первый запрос session, где TTFT не был измерен |
| `gateway_session_tracker_errors_total` | ошибки Valkey session tracker |
| `gateway_proxy_loki_push_total` | успешные/ошибочные push-запросы в Loki |
| `gateway_proxy_loki_events_dropped_total` | события Loki, отброшенные из-за полной очереди |

Важно:

- `session_id`, `request_id`, `trace_id`, `span_id` не используются как Prometheus labels
- `duration_sec` и `ttft_sec` конкретного запроса лежат в Loki, не в Prometheus

## 5. Что Смотреть В Loki

Все ниже смотреть через Grafana datasource `Loki`.

Gateway пишет три основных типа events.

Request events:

```logql
{app="vllm-gateway", bucket="request_generation", route="/v1/chat/completions"}
| json
```

Главные поля:

- `request_id`
- `session_id`
- `session_present`
- `session_first_request`
- `trace_id`
- `span_id`
- `request_model`
- `message_count`

Response events:

```logql
{app="vllm-gateway", bucket="response_vllm", route="/v1/chat/completions"}
| json
```

Главные поля:

- `assistant_text` - текст ответа модели
- `duration_sec` - E2E latency конкретного request
- `ttft_sec` - TTFT конкретного streaming request
- `session_init_e2e_sec` - E2E первого request session
- `session_init_ttft_sec` - TTFT первого streaming request session
- `status_code`
- `trace_id`

Готовый LogQL для таблицы logs с текстом ассистента и latency:

```logql
{app="vllm-gateway", bucket="response_vllm", route="/v1/chat/completions"}
| json
| line_format "session={{.session_id}} request={{.request_id}} stream={{.stream}} status={{.status_code}} duration={{.duration_sec}} ttft={{.ttft_sec}} session_e2e={{.session_init_e2e_sec}} session_ttft={{.session_init_ttft_sec}} assistant={{.assistant_text}}"
```

Error events:

```logql
{app="vllm-gateway", bucket="gateway_error", route="/v1/chat/completions"}
| json
| line_format "session={{.session_id}} request={{.request_id}} stream={{.stream}} error={{.error_type}} duration={{.duration_sec}} session_e2e={{.session_init_e2e_sec}} message={{.error_message}}"
```

## 6. Что Смотреть В Tempo

Все ниже смотреть через Grafana datasource `Tempo`.

Gateway создает traces для:

```text
/v1/chat/completions
```

Ожидаемая структура:

```text
llm.gateway.request
  llm.vllm.upstream
  llm.stream_response       # только для stream=true
```

Главные attributes:

- `request.id`
- `session.id`, если был `X-Session-ID`
- `session.present`
- `session.first_request`
- `http.route`
- `http.method`
- `http.status_code`
- `llm.model`
- `llm.stream`
- `llm.duration_sec`

TraceQL:

```traceql
{ resource.service.name = "vllm-gateway" }
```

```traceql
{ .http.route = "/v1/chat/completions" }
```

```traceql
{ .session.first_request = true }
```

## 7. Связь Loki -> Tempo

Gateway добавляет в Loki events:

- `trace_id`
- `span_id`

Datasource `Loki` в Grafana уже настроен так, что поле `trace_id` ведет в datasource `Tempo`.

Проверочный LogQL:

```logql
{app="vllm-gateway", bucket="response_vllm", route="/v1/chat/completions"}
| json
| trace_id != ""
```

В Grafana:

1. Откройте datasource `Loki`.
2. Выполните query выше.
3. Раскройте log line.
4. Нажмите на derived field `trace_id`.
5. Grafana откроет trace в datasource `Tempo`.

## 8. Запуск

Из корня проекта сначала поднимите trace-stack:

```bash
docker compose -f deploy/docker-compose.yaml up -d
```

Потом основной runtime:

```bash
docker compose up -d --build
```

Если остались старые контейнеры от прошлых запусков:

```bash
docker compose down --remove-orphans
docker compose -f deploy/docker-compose.yaml up -d
docker compose up -d --build
```

## 9. Порты

Основной compose:

- `http://127.0.0.1:9090` - vLLM Gateway
- `http://127.0.0.1:9091` - Prometheus
- `http://127.0.0.1:9092` - Loki API

Trace compose:

- `http://127.0.0.1:3000` - Grafana
- `http://127.0.0.1:3200` - Tempo HTTP API
- `127.0.0.1:4317` - OTel Collector OTLP gRPC
- `127.0.0.1:4318` - OTel Collector OTLP HTTP
- `http://127.0.0.1:13133` - Collector healthcheck

## 10. Проверки

Проверить compose-файлы:

```bash
docker compose -f deploy/docker-compose.yaml config
docker compose config
```

Проверить сервисы:

```bash
curl -fsS http://127.0.0.1:13133/
curl -fsS http://127.0.0.1:3200/ready
curl -fsS http://127.0.0.1:9090/healthz
curl -fsS http://127.0.0.1:9090/gateway/metrics | grep gateway_session
curl -fsS http://127.0.0.1:9091/-/ready
curl -fsS http://127.0.0.1:9092/ready
```

Посмотреть логи:

```bash
docker logs -f vllm-gateway
docker logs -f vllm-gateway-otel-collector
docker logs -f vllm-gateway-tempo
```

## 11. Smoke Request

Узнать model id:

```bash
curl -fsS http://127.0.0.1:9090/v1/models
```

Non-stream:

```bash
curl -fsS http://127.0.0.1:9090/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: deploy-smoke-nonstream-1' \
  -H 'X-Session-ID: deploy-smoke-session-1' \
  -d '{
    "model": "<model-id>",
    "stream": false,
    "messages": [
      {"role": "user", "content": "Say one short sentence."}
    ]
  }'
```

Stream:

```bash
curl -N http://127.0.0.1:9090/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: deploy-smoke-stream-1' \
  -H 'X-Session-ID: deploy-smoke-session-2' \
  -d '{
    "model": "<model-id>",
    "stream": true,
    "messages": [
      {"role": "user", "content": "Count from one to five."}
    ]
  }'
```

Для повторной проверки session init metrics используйте новый `X-Session-ID`.

## 12. Session TTL

Gateway хранит `X-Session-ID` в Valkey со sliding TTL:

- ключа нет -> первый запрос session
- ключ есть -> TTL обновляется
- session не дергали `SESSION_TTL` секунд -> ключ удаляется

Проверка:

```bash
docker exec vllm-gateway-valkey valkey-cli --scan --pattern 'vllm-gateway:session:*'
docker exec vllm-gateway-valkey valkey-cli ttl 'vllm-gateway:session:<session-id>'
```

## 13. Troubleshooting

Нет metrics в Grafana:

- проверьте `http://127.0.0.1:9091/targets`
- job `gateway` должен быть `UP`
- напрямую проверьте `curl -fsS http://127.0.0.1:9090/gateway/metrics`

Нет logs в Grafana:

- проверьте `curl -fsS http://127.0.0.1:9092/ready`
- проверьте `docker logs vllm-gateway`
- проверьте `gateway_proxy_loki_push_total{status="error"}`

Нет traces в Grafana:

- проверьте `curl -fsS http://127.0.0.1:13133/`
- проверьте `curl -fsS http://127.0.0.1:3200/ready`
- проверьте `OTEL_ENABLED=true`
- проверьте `OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4317`
- проверьте `docker logs vllm-gateway-otel-collector`

Нет `gateway_session_init_ttft_seconds`:

- TTFT пишется только для `stream=true`
- TTFT session init пишется только для первого запроса нового `X-Session-ID`
- если TTFT не удалось измерить, смотрите `gateway_session_init_ttft_missing_total`

Нет `gateway_session_init_e2e_latency_seconds`:

- проверьте, что запрос был `/v1/chat/completions`
- проверьте, что `X-Session-ID` непустой
- второй запрос с тем же `X-Session-ID` не пишет session init metric
