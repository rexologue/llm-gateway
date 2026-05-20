# vLLM gateway + Loki + Tempo

Этот проект поднимает HTTP gateway перед vLLM и делает его единственной точкой входа для запросов `/v1/*`.

Gateway:

- принимает клиентский HTTP-запрос;
- проксирует его в vLLM;
- пишет компактные диагностические события в Loki;
- отдает Prometheus-метрики vLLM через `/metrics` и собственные метрики через `/gateway/metrics`;
- отправляет OpenTelemetry traces в OTLP collector для Tempo;
- отслеживает первый запрос сессии по `X-Session-ID` через Valkey со sliding TTL.

## Observability

В проекте три канала наблюдаемости:

- логи в Loki;
- метрики в Prometheus;
- traces в Tempo через OpenTelemetry OTLP.

Карта:

| Что | Куда попадает | Grafana datasource |
| --- | --- | --- |
| Gateway metrics `/gateway/metrics` | Prometheus | `Prometheus` |
| vLLM metrics `/metrics` | Prometheus | `Prometheus` |
| Request/response/error events | Loki | `Loki` |
| OpenTelemetry traces | OTel Collector -> Tempo | `Tempo` |
| Session state по `X-Session-ID` | Valkey | не datasource |

Prometheus:

- `/metrics` проксирует Prometheus-метрики upstream vLLM;
- `/gateway/metrics` отдает собственные метрики gateway.

Loki:

- gateway пишет request/response events;
- события содержат `request_id`, `session_id`, `session_present`, `session_first_request`, `trace_id`, `span_id`;
- Loki stream labels остаются низкокардинальными: `app`, `env`, `bucket`, `route`.

Tempo:

- gateway создает spans для `/v1/chat/completions`;
- root span `llm.gateway.request` содержит `request.id`, `session.id` при наличии, `session.present`, `session.first_request`, `llm.model`, `llm.stream`, размеры body и latency;
- child span `llm.vllm.upstream` покрывает обращение к vLLM;
- для streaming-запросов span `llm.stream_response` живет до завершения iterator.

Локальный trace-stack находится в `deploy/`:

- `deploy/docker-compose.yaml` поднимает Tempo, OpenTelemetry Collector и Grafana;
- Tempo хранит traces локально;
- Grafana уже содержит datasources `Prometheus`, `Loki`, `Tempo`.

Клиент должен передавать session header:

```text
X-Session-ID: <session-id>
```

Gateway не создает fake session id. Если header отсутствует, запрос не падает, но растет `gateway_session_id_missing_total`.

`session_id`, `request_id`, `trace_id` и `span_id` не используются как Prometheus labels.

## Метрики

Отдельная операторская выжимка по Grafana metrics: [METRICS.md](./METRICS.md).

Gateway публикует два отдельных endpoint'а:

- `/metrics` - проксирует Prometheus-метрики upstream vLLM;
- `/gateway/metrics` - отдает собственные метрики gateway.

Собственные метрики gateway имеют префиксы `gateway_proxy_` и `gateway_session_`, чтобы их было проще отличать от метрик vLLM и стандартных `process_*`/`python_*` метрик.

### Gateway metrics

`gateway_proxy_requests_total`

- Тип: counter
- Назначение: все запросы, обработанные gateway
- Labels: `route`, `method`, `stream`

`gateway_proxy_request_latency_seconds`

- Тип: histogram
- Назначение: end-to-end latency запроса через gateway
- Labels: `route`, `method`, `stream`
- Buckets: `0.05`, `0.1`, `0.25`, `0.5`, `1`, `2`, `5`, `10`, `30`, `60`, `120`, `300`, `900`

`gateway_proxy_loki_push_total`

- Тип: counter
- Назначение: попытки отправки батчей в Loki
- Labels: `status`
- Значения `status`: `success`, `error`

`gateway_proxy_loki_events_dropped_total`

- Тип: counter
- Назначение: события Loki, отброшенные до доставки
- Labels: `reason`
- Основная причина: `queue_full`, когда in-memory очередь достигла `LOKI_QUEUE_MAX_SIZE`

`gateway_session_requests_total`

- Тип: counter
- Назначение: session-aware запросы `/v1/chat/completions`
- Labels: `route`, `method`, `stream`, `session_present`, `session_first_request`
- `session_present`: `true`, если пришел непустой `X-Session-ID`
- `session_first_request`: `true` только если session id не было в Valkey и gateway записал его с TTL

`gateway_session_id_missing_total`

- Тип: counter
- Назначение: запросы `/v1/chat/completions` без непустого `X-Session-ID`
- Labels: `route`, `method`, `stream`

`gateway_session_tracker_errors_total`

- Тип: counter
- Назначение: ошибки Valkey session tracker при проверке или обновлении session state
- Labels: `operation`, `error_type`

`gateway_session_init_ttft_seconds`

- Тип: histogram
- Назначение: TTFT первого streaming-запроса в сессии
- Определение: время от приема первого запроса gateway до первого non-empty chunk от vLLM
- Labels: `route`, `method`, `stream`, `model`, `status_family`, `result`
- Buckets: `0.05`, `0.1`, `0.25`, `0.5`, `1`, `2`, `5`, `10`, `30`, `60`, `120`, `300`
- Пишется только для первого запроса с новым `X-Session-ID` и только для `stream=true`

`gateway_session_init_ttft_missing_total`

- Тип: counter
- Назначение: первый запрос session, где TTFT невозможно записать
- Labels: `route`, `method`, `stream`, `model`, `reason`, `result`
- Значения `reason`: `non_stream`, `no_chunk`, `cancelled_before_first_chunk`, `error_before_first_chunk`

`gateway_session_init_e2e_latency_seconds`

- Тип: histogram
- Назначение: end-to-end latency первого запроса в сессии
- Определение: время от приема первого запроса gateway до полного завершения ответа
- Labels: `route`, `method`, `stream`, `model`, `status_family`, `result`
- Buckets: `0.05`, `0.1`, `0.25`, `0.5`, `1`, `2`, `5`, `10`, `30`, `60`, `120`, `300`, `900`
- Пишется только для первого запроса с новым `X-Session-ID`

Для session init histograms:

- `model`: имя модели из request JSON или `unknown`
- `model` ограничен 128 символами; пустые или слишком длинные значения пишутся как `unknown`
- `status_family`: `2xx`, `4xx`, `5xx` или `unknown`
- `result`: `success`, `error`, `cancelled`

Запрещенные labels:

- `session_id`
- `request_id`
- `trace_id`
- `span_id`

## Схема логирования

Логи раскладываются по трем логическим корзинам. Каждая корзина становится Loki label `bucket`.

### `request_generation`

Сюда попадают запросы на генерацию:

- `/v1/chat/completions`
- `/v1/completions`
- `/v1/responses`

В этой корзине хранится один request event на один завершившийся запрос.

### `request_non_generation`

Сюда попадают остальные запросы `/v1/*`, например:

- `/v1/models`
- `/v1/embeddings`
- любые служебные и негенерационные endpoint'ы

В этой корзине тоже хранится один request event на один завершившийся запрос.

### `response_vllm`

Сюда попадает все, что вернула vLLM.

Это один response event на один завершившийся запрос.

Для streaming-запросов gateway не пишет chunks отдельно. Он дожидается завершения потока, собирает полный ответ и после этого логирует:

- один request event в `request_generation`;
- один response event в `response_vllm`.

Для одного `chat/completions` обычно получается две записи:

- запись о запросе;
- запись об ответе.

## Состав логов

OpenAI-compatible JSON запроса и ответа уже содержит почти все полезное. Поэтому в логах остаются:

- стандартное тело запроса или ответа;
- только те дополнительные поля, которые не живут внутри стандартного JSON, но полезны для разбора работы системы.

### Что хранится в request event

Request event содержит:

- `bucket`
- `route`
- `method`
- `request_id`
- `session_id`
- `session_present`
- `session_first_request`
- `trace_id`
- `span_id`
- `stream`
- `body_bytes`
- `body_sha256`
- компактную сводку по важным request headers
- `request_json`, если тело разобралось как JSON
- `request_text`, если тело не JSON

Для `chat/completions` дополнительно сохраняются:

- `request_model`
- `message_count`
- `tool_message_count`
- `assistant_tool_call_count`

Эти derived-поля помогают быстро понять характер входного трафика без ручного просмотра полного массива `messages`.

### Что хранится в response event

Response event содержит:

- `bucket`
- `route`
- `method`
- `request_id`
- `session_id`
- `session_present`
- `session_first_request`
- `trace_id`
- `span_id`
- `stream`
- `status_code`
- `duration_sec`
- `ttft_sec`, если это streaming response и первый non-empty chunk был получен
- `session_init_ttft_sec`, если это первый request session и TTFT был получен
- `session_init_e2e_sec`, если это первый request session
- `body_bytes`
- `body_sha256`
- `response_content_type`
- `assistant_text`, если из ответа удалось извлечь текст ассистента
- `response_json`, если ответ разобрался как JSON
- `response_text`, если ответ не JSON

`duration_sec` вынесен отдельно, потому что это важное системное поле, которого нет в стандартном OpenAI JSON.

Для `stream=false` поле `assistant_text` берется из итогового ответа модели. Для `stream=true` gateway склеивает текст из всех SSE-чанков и пишет уже собранный результат.

### Что хранится в gateway error event

Если запрос к upstream vLLM падает до нормального response event, gateway пишет отдельный Loki event с `bucket="gateway_error"`.

Error event содержит:

- `bucket`
- `route`
- `method`
- `request_id`
- `session_id`
- `session_present`
- `session_first_request`
- `trace_id`
- `span_id`
- `stream`
- `error_type`
- `error_message`
- `duration_sec`
- `session_init_e2e_sec`, если это первый request session

## Что такое `route` в `LokiSink`

`route` в `LokiSink` не означает URL Loki и не означает имя docker-сервиса. Это маршрут gateway, к которому относится логируемое событие.

Примеры:

- `POST /v1/chat/completions` дает `route="/v1/chat/completions"`
- `GET /v1/models` дает `route="/v1/models"`
- `POST /v1/embeddings` дает `route="/v1/embeddings"`

Это поле поднимается в Loki label, чтобы:

- быстро отделять генерационные запросы от служебных;
- фильтровать конкретную ручку без `| json`;
- не смешивать события разных endpoint'ов в одном stream.

Как это работает:

- gateway формирует event и кладет в него `route`;
- `LokiSink` берет `route` и помещает его в `stream_labels`;
- Loki хранит отдельные streams для разных `route`.

`LokiSink` группирует события по четырем labels:

- `app`
- `env`
- `bucket`
- `route`

Это находится в [event_sinks.py](gateway/app/event_sinks.py).

## Структура приложения

```text
gateway/app
├── __init__.py
├── event_sinks.py
├── http_utils.py
├── log_payloads.py
├── main.py
├── observability.py
├── routes.py
├── session_tracker.py
├── settings.py
├── state.py
└── tracing.py
```

Назначение модулей:

- `main.py`
  Создает FastAPI app и управляет lifecycle shared-ресурсов.

- `routes.py`
  Содержит proxy-роуты и orchestration запроса: чтение, proxy, логирование и возврат ответа.

- `log_payloads.py`
  Собирает компактные request/response payload'ы для Loki и извлекает derived-поля вроде `assistant_text`.

- `settings.py`
  Читает переменные окружения gateway.

- `state.py`
  Хранит `httpx.AsyncClient`, `LokiSink` и `SessionTracker`.

- `session_tracker.py`
  Проверяет первый запрос сессии через Valkey: `SET NX EX` для новых session id и `EXPIRE` для уже известных session id.

- `tracing.py`
  Настраивает OpenTelemetry FastAPI instrumentation и OTLP exporter, а также добавляет `trace_id`/`span_id` в Loki events.

- `event_sinks.py`
  Буферизует события и отправляет их в Loki батчами.

- `http_utils.py`
  Содержит helper-функции для JSON, hash, redaction и proxy headers.

- `observability.py`
  Определяет Prometheus-метрики.

## Docker Compose

`docker-compose.yaml` поднимает:

- `gateway`
- `vllm`
- `loki`
- `valkey`
- `vllm-prometheus`
- `vllm-hardware-node-exporter`
- `vllm-hardware-dcgm-exporter`

`deploy/docker-compose.yaml` отдельно поднимает:

- `tempo`
- `otel-collector`
- `grafana`

Gateway использует:

- Loki для логов;
- Prometheus для метрик;
- OTLP exporter для отправки traces в Collector из `deploy/` через `host.docker.internal:4317`;
- Valkey для session first-request tracking;
- один компактный request event и один compact response event на завершившийся запрос.

Tempo в `deploy/` хранит traces на локальном volume.

## Подробное описание всех параметров gateway

Ниже перечислены все env-параметры, которые читает gateway.

### `GATEWAY_HOST`

Адрес, на котором uvicorn слушает входящие подключения внутри контейнера.

- Тип: строка
- Значение по умолчанию: `0.0.0.0`
- Когда менять: если gateway запускается не в контейнере или нужно ограничить bind конкретным интерфейсом
- Практический смысл: в Docker почти всегда должен оставаться `0.0.0.0`

### `GATEWAY_PORT`

Внутренний порт uvicorn внутри контейнера gateway.

- Тип: целое число
- Значение по умолчанию: `8080`
- Когда менять: если внутри контейнера нужен другой порт
- Важно: это не host-port из `docker-compose.yaml`, а именно порт процесса в контейнере

### `GATEWAY_UPSTREAM_BASE_URL`

Базовый URL upstream vLLM, куда gateway пересылает запросы.

- Тип: строка URL
- Значение по умолчанию: `http://vllm:8000`
- Когда менять: если vLLM живет на другом hostname, порту или вынесен в другой network
- Важно: завершающий `/` автоматически убирается, чтобы route корректно склеивался с `/v1/...`

### `GATEWAY_ENV`

Логическая метка окружения, которая уходит в Loki label `env`.

- Тип: строка
- Значение по умолчанию: `local`
- Когда менять: если нужно различать `dev`, `stage`, `prod`, `perf`
- Практический смысл: позволяет отделять логи окружений без чтения тела записи

### `GATEWAY_REQUEST_LOG_LABEL`

Имя приложения, которое уходит в Loki label `app`.

- Тип: строка
- Значение по умолчанию: `vllm-gateway`
- Когда менять: если несколько gateway пишут в один Loki или нужна другая схема именования
- Практический смысл: это основной label для запросов вида `{app="vllm-gateway"}`

### `GATEWAY_TIMEOUT_CONNECT_SEC`

Максимальное время установки TCP-соединения до upstream vLLM.

- Тип: число с плавающей точкой, секунды
- Значение по умолчанию: `30`
- Когда менять: если сеть до vLLM нестабильна или, наоборот, нужен более агрессивный fail-fast
- Что контролирует: именно этап connect, а не полное время ответа модели

### `GATEWAY_TIMEOUT_READ_SEC`

Максимальное время ожидания чтения ответа от upstream.

- Тип: число с плавающей точкой, секунды
- Значение по умолчанию: `1800`
- Когда менять: если генерации очень длинные или если нужно жестче ограничить зависшие запросы
- Особенно важно: влияет на длинные completions и streaming

### `GATEWAY_TIMEOUT_WRITE_SEC`

Максимальное время, которое `httpx` тратит на отправку запроса upstream'у.

- Тип: число с плавающей точкой, секунды
- Значение по умолчанию: `1800`
- Когда менять: если запросы большие или канал до vLLM медленный
- Практический смысл: контролирует стадию записи request body в upstream-сокет

### `GATEWAY_TIMEOUT_POOL_SEC`

Максимальное время ожидания свободного соединения из локального connection pool `httpx`.

- Тип: число с плавающей точкой, секунды
- Значение по умолчанию: `30`
- Когда менять: если под нагрузкой соединений не хватает и запросы стоят в очереди pool
- Важно: это не сетевой timeout до vLLM, а timeout ожидания слота в локальном pool

### `GATEWAY_HTTP_MAX_CONNECTIONS`

Общий лимит одновременно открытых соединений `httpx` к upstream.

- Тип: целое число
- Значение по умолчанию: `200`
- Когда менять: если требуется больше параллелизма или нужно ограничить давление на vLLM
- Риск слишком малого значения: рост ожидания в pool и увеличение latency

### `GATEWAY_HTTP_MAX_KEEPALIVE_CONNECTIONS`

Максимальное число keep-alive соединений, которые `httpx` оставляет открытыми для повторного использования.

- Тип: целое число
- Значение по умолчанию: `100`
- Когда менять: если много коротких запросов и важно снизить накладные расходы на повторные TCP-connect
- Практический смысл: помогает держать соединения горячими

### `LOKI_ENABLED`

Включает или выключает отправку логов в Loki.

- Тип: boolean в виде строки `true` или `false`
- Значение по умолчанию: `true`
- Когда менять: если нужно временно поднять gateway без внешнего Loki
- Важно: при `false` события не пишутся никуда, fallback-хранилища нет

### `LOKI_PUSH_URL`

HTTP endpoint Loki push API.

- Тип: строка URL
- Значение по умолчанию: `http://loki:3100/loki/api/v1/push`
- Когда менять: если Loki вынесен на другой адрес, порт, ingress или host
- Важно: это URL push API, а не query endpoint и не UI

### `LOKI_BATCH_SIZE`

Максимальное количество событий, которое `LokiSink` собирает перед отправкой одной пачкой.

- Тип: целое число
- Значение по умолчанию: `200`
- Когда менять: если нужно уменьшить число запросов к Loki или, наоборот, быстрее отправлять маленькие партии
- Компромисс: большой батч снижает сетевой overhead, но увеличивает задержку появления логов

### `LOKI_FLUSH_INTERVAL_SEC`

Интервал принудительного flush для неполного батча в `LokiSink`.

- Тип: число с плавающей точкой, секунды
- Значение по умолчанию: `1.0`
- Когда менять: если нужен более быстрый или более редкий сброс логов
- Компромисс: меньшее значение уменьшает задержку доставки логов, но увеличивает число POST-запросов в Loki

### `LOKI_QUEUE_MAX_SIZE`

Максимальный размер in-memory очереди событий Loki.

- Тип: целое число
- Значение по умолчанию: `10000`
- Когда менять: если нужно ограничить память при падении Loki или поднять допустимый буфер при пиковых нагрузках
- Поведение: если очередь заполнена, gateway отбрасывает новое событие, увеличивает `gateway_proxy_loki_events_dropped_total{reason="queue_full"}` и не блокирует inference path

### `LOG_BODY_SHA256`

Определяет, считать ли SHA-256 для request и response body.

- Тип: boolean в виде строки `true` или `false`
- Значение по умолчанию: `true`
- Когда менять: если hash не нужен или хочется снизить CPU overhead на больших телах
- Практический смысл: помогает сравнивать payload/response и искать дубликаты без полного визуального сравнения body

### `OTEL_ENABLED`

Включает OpenTelemetry tracing.

- Тип: boolean в виде строки `true` или `false`
- Значение по умолчанию: `false`
- Когда менять: `true`, если рядом есть OpenTelemetry Collector или другой OTLP receiver
- Практический смысл: при `false` gateway работает без tracing и без попыток отправки OTLP

### `OTEL_SERVICE_NAME`

Имя сервиса в traces.

- Тип: строка
- Значение по умолчанию: `vllm-gateway`
- Практический смысл: попадает в resource attribute `service.name`

### `OTEL_EXPORTER_OTLP_ENDPOINT`

Адрес OTLP gRPC endpoint.

- Тип: URL
- Значение по умолчанию в gateway settings: `http://otel-collector:4317`
- Значение в `docker-compose.yaml`: `http://host.docker.internal:4317`
- Когда менять: если gateway должен писать в другой OTLP receiver
- Важно: gateway пишет в OpenTelemetry Collector, а не в Tempo напрямую

### `OTEL_EXPORTER_OTLP_PROTOCOL`

Протокол OTLP exporter.

- Тип: строка
- Значение по умолчанию: `grpc`
- Поддерживается: `grpc`

### `OTEL_SAMPLE_RATIO`

Доля traces, которые gateway семплирует.

- Тип: число от `0.0` до `1.0`
- Значение по умолчанию: `1.0`
- Практический смысл: `1.0` пишет все traces, `0.1` примерно 10%

### `OTEL_FASTAPI_EXCLUDED_URLS`

Список URL patterns, исключенных из FastAPI auto-instrumentation.

- Тип: строка, разделенная запятыми
- Значение по умолчанию: `/metrics,/gateway/metrics,/healthz,/$`
- Практический смысл: healthchecks и Prometheus scrapes не попадают в Tempo как отдельные traces

### `SESSION_VALKEY_URL`

URL Valkey для хранения session ids.

- Тип: Redis-compatible URL
- Значение по умолчанию: `redis://vllm-gateway-valkey:6379/0`
- Практический смысл: gateway использует Valkey как распределенный tracker, чтобы несколько процессов одинаково понимали первый запрос сессии

### `SESSION_KEY_PREFIX`

Префикс ключей session tracker в Valkey.

- Тип: строка
- Значение по умолчанию: `vllm-gateway:session:`
- Практический смысл: изолирует ключи gateway от других данных в той же базе

### `SESSION_TTL`

TTL session id в Valkey.

- Тип: целое число, секунды
- Значение по умолчанию: `21600`
- Поведение: если session id нет в базе, gateway создает ключ с TTL и считает запрос первым; если ключ уже есть, gateway обновляет TTL через `EXPIRE`
- Важно: TTL sliding, то есть считается от последнего запроса с этим `X-Session-ID`, а не от первого создания ключа

### `SESSION_TRACKER_MAX_CONNECTIONS`

Максимальный размер connection pool к Valkey.

- Тип: целое число
- Значение по умолчанию: `256`
- Когда менять: при высокой параллельности запросов или ограничениях на число соединений к Valkey

## Порты

По умолчанию `docker-compose.yaml` публикует:

- gateway: `9090`
- Loki: `9092`
- Prometheus: `9091`

По умолчанию `deploy/docker-compose.yaml` публикует:

- Grafana: `3000`
- Tempo HTTP query API: `3200`
- OpenTelemetry Collector OTLP gRPC: `4317`
- OpenTelemetry Collector OTLP HTTP: `4318`
- OpenTelemetry Collector healthcheck: `13133`

Внутри docker network gateway ходит в upstream по адресу `http://vllm:8000`.
Gateway отправляет traces в Collector по адресу `http://host.docker.internal:4317`.

## Запуск

Из корня проекта:

```bash
docker compose -f deploy/docker-compose.yaml up -d
docker compose up -d --build
```

## Базовая проверка

Проверить health endpoint:

```bash
curl http://127.0.0.1:9090/healthz
```

Проверить метрики:

```bash
curl http://127.0.0.1:9090/metrics
```

Проверить собственные метрики gateway:

```bash
curl http://127.0.0.1:9090/gateway/metrics
```

Проверить proxied `/v1/models`:

```bash
curl http://127.0.0.1:9090/v1/models
```

## Примеры LogQL

Все generation requests:

```logql
{app="vllm-gateway",bucket="request_generation"}
```

Все non-generation requests:

```logql
{app="vllm-gateway",bucket="request_non_generation"}
```

Все ответы vLLM:

```logql
{app="vllm-gateway",bucket="response_vllm"}
```

Только generation requests на `chat/completions`:

```logql
{app="vllm-gateway",bucket="request_generation",route="/v1/chat/completions"}
```

Только ответы по `models`:

```logql
{app="vllm-gateway",bucket="response_vllm",route="/v1/models"}
```

Только generation requests, где в истории уже были tool messages:

```logql
{app="vllm-gateway",bucket="request_generation",route="/v1/chat/completions"} | json | tool_message_count > 0
```

Только медленные ответы дольше 5 секунд:

```logql
{app="vllm-gateway",bucket="response_vllm"} | json | duration_sec > 5
```

## Полезные команды

Логи gateway:

```bash
docker logs -f vllm-gateway
```

Логи Loki:

```bash
docker logs -f loki
```

Логи Prometheus:

```bash
docker logs -f vllm-prometheus
```

## Ограничения текущей схемы

- Gateway не исполняет tools.
- Gateway не модифицирует payload.
- Gateway не пишет chunks стриминга по отдельности.
- Gateway не хранит логи локально на диске.
- Если Loki недоступен, fallback storage отсутствует.

То есть это прозрачный прокси с компактным, но все еще достаточно диагностичным логированием.
