# OpenAI-совместимый LLM-шлюз

Проект запускает FastAPI-шлюз перед любым бэкендом, который предоставляет
OpenAI-совместимый API `/v1/*`. Шлюз намеренно не привязан к конкретному
бэкенду: vLLM и SGLang даны лишь как варианты развёртывания.

Шлюз:

- проксирует `/v1/chat/completions` с поддержкой потокового и непотокового режимов;
- проксирует все остальные маршруты `/v1/*` как обобщённые OpenAI-совместимые маршруты;
- пишет компактные события запросов/ответов/ошибок в Loki;
- отдаёт собственные Prometheus-метрики шлюза на `/gateway/metrics`;
- проксирует метрики бэкенда на `/metrics`;
- отправляет OpenTelemetry-трассы в OTLP-коллектор, когда это включено;
- отслеживает первый запрос в сессии по заголовку `X-Session-ID` с помощью Valkey.

Шлюз предоставляет два отдельных эндпоинта метрик: `/gateway/metrics` — для
собственных Prometheus-метрик шлюза, и `/metrics`, который проксирует эндпоинт
метрик бэкенда (возвращая `503`, когда бэкенд недоступен). У compose-стека LLM
свой Prometheus для метрик бэкенда, а у compose-стека шлюза — отдельный
Prometheus для метрик шлюза.

## Структура

```text
gateway/          код FastAPI-шлюза
deploy/llm        compose-файлы LLM-движка, скрипты запуска, метрики бэкенда
deploy/gateway    compose-файл шлюза, Loki, Valkey, Prometheus, Tempo, OTEL
observability/    JSON-экспорты дашбордов для импорта в существующий Grafana/workspace
docs/             справочник по развёртыванию, метрикам и трассировке
```

Подробные справочники:

- [Развёртывание](docs/DEPLOY.md)
- [Метрики](docs/METRICS.md)
- [Трассы](docs/TRACES.md)
- [Дашборды](docs/DASHBOARDS.md)

## Рекомендуемое развёртывание

Сначала создайте настройки развёртывания:

```bash
cp deploy/llm/.env.example deploy/llm/.env
# отредактируйте deploy/llm/.env

cp deploy/gateway/.env.example deploy/gateway/.env
# отредактируйте deploy/gateway/.env
```

Сначала запустите один из вариантов бэкенда и проверьте его напрямую.

vLLM:

```bash
cd deploy/llm
docker compose --env-file .env -f docker-compose.vllm.yaml up -d --build
docker compose --env-file .env -f docker-compose.vllm.yaml --profile test run --rm llm-smoke-tests
```

SGLang:

```bash
cd deploy/llm
docker compose --env-file .env -f docker-compose.sglang.yaml up -d --build
docker compose --env-file .env -f docker-compose.sglang.yaml --profile test run --rm llm-smoke-tests
```

Только после того, как smoke-тест бэкенда прошёл, запускайте стек шлюза и
проверяйте тот же путь запроса уже через шлюз:

```bash
cd deploy/gateway
docker compose --env-file .env -f docker-compose.yaml up -d --build
docker compose --env-file .env -f docker-compose.yaml --profile test run --rm gateway-smoke-tests
```

Smoke-тесты разделены на два набора:

- `deploy/tests/smoke/test_backend_contract.py` — только OpenAI-совместимый
  контракт (`/v1`): непотоковый ответ, SSE-стрим, tool calling, отсутствие
  reasoning-трейса. Он корректен для любого эндпоинта, поэтому его гоняют оба
  стека — и бэкенд напрямую, и шлюз.
- `deploy/tests/smoke/test_gateway_sessions.py` — только шлюз: персистенция
  диалога в Valkey через `/gateway/session/{session_id}` и
  `/gateway/session_list`. У бэкенда этих маршрутов нет, поэтому набор
  запускается лишь в стеке шлюза.

Какой набор выполняется, задаёт `command` соответствующего сервиса в compose,
так что обе команды выше запускаются без дополнительных флагов.

Если для развёртывания требуется вызов инструментов (tool calling), задайте
`SMOKE_CHECK_TOOLS=true` в `deploy/llm/.env` и `deploy/gateway/.env`; тогда
раннер также отправит запрос с вызовом инструмента и завершится с ошибкой, если
выбранный бэкенд или путь через шлюз не может вернуть OpenAI-совместимые
`tool_calls`. Оставьте значение `false`, когда инструменты не входят в
ожидаемый рантайм-контракт. Подробности — в [Развёртывании](docs/DEPLOY.md).

Порты по умолчанию:

- LLM API: `http://127.0.0.1:9900`
- Prometheus LLM: `http://0.0.0.0:9191`
- шлюз: `http://0.0.0.0:9090`
- эндпоинт метрик шлюза: `http://0.0.0.0:9090/gateway/metrics`
- прокси метрик бэкенда: `http://0.0.0.0:9090/metrics`
- Prometheus шлюза: `http://0.0.0.0:9091`
- Loki: `http://0.0.0.0:9092`
- Tempo: `http://0.0.0.0:3200`

## Контракт бэкенда

Бэкенд должен предоставлять OpenAI-совместимый HTTP API по префиксу `/v1`. Шлюз
не зависит от специфичных для бэкенда Python-API или внутренностей движка.

Обязательно для основного пути:

- `POST /v1/chat/completions`
- потоковые ответы в OpenAI-совместимом формате SSE при `stream=true`

Обобщённое проксирование поддерживает также маршруты вида:

- `GET /v1/models`
- `POST /v1/completions`
- `POST /v1/embeddings`
- любой другой маршрут `/v1/*`, поддерживаемый бэкендом

## Важные переменные окружения

| Переменная | Значение | По умолчанию |
| --- | --- | --- |
| `GATEWAY_HOST` | Адрес хоста, используемый Compose при привязке портов | `0.0.0.0` |
| `GATEWAY_BACKEND_BASE_URL` | Базовый URL OpenAI-совместимого бэкенда | `http://host.docker.gateway:9900` |
| `GATEWAY_FORCED_MAX_COMPLETION_TOKENS` | Опциональное принудительное `max_completion_tokens` для чат-запросов | не задано |
| `GATEWAY_FORCED_THINKING_DISABLED` | Принудительно выставлять `chat_template_kwargs.enable_thinking=false` в JSON-теле запроса | `false` |
| `GATEWAY_ENABLE_SAMPLING_FALLBACK_OVERRIDE` | Заменять некорректные параметры сэмплирования в чат-запросах безопасными резервными значениями | `false` |
| `GATEWAY_LOKI_APP_NAME` | Метка `app` в Loki | `llm-gateway` |
| `GATEWAY_LOKI_ENABLED` | Включить доставку событий в Loki | `true` |
| `GATEWAY_LOKI_PUSH_URL` | URL Loki Push API | `http://llm-gateway-loki:3100/loki/api/v1/push` |
| `GATEWAY_OTEL_ENABLED` | Включить трассировку OpenTelemetry | `false` |
| `GATEWAY_VALKEY_URL` | Базовый URL Valkey/Redis; рантайм использует DB 0, сохранённые чаты — DB 1 | `redis://llm-gateway-valkey:6379` |
| `GATEWAY_SESSION_TTL` | Скользящий TTL сессии в секундах | `21600` |
| `GATEWAY_SESSION_STORE_TTL` | TTL сохранённой чат-сессии в секундах | `1296000` |

## Сессии

Когда запрос к `/v1/chat/completions` содержит `X-Session-ID`, шлюз сохраняет
диалог в Valkey DB 1 в виде `{metadata, tools, messages}`: объявленные в запросе
`tools`, его `messages` и ход ассистента, сгенерированный бэкендом (добавляется
после генерации, а в потоковом режиме — реконструируется из SSE-потока).
Последующие запросы с тем же идентификатором сессии перезаписывают запись и
обновляют TTL, сохраняя исходное значение `created_at`.

- `GET /gateway/session_list` возвращает по одной сводке метаданных на каждую
  сохранённую сессию, включая `age_sec` (время жизни с первого запроса),
  `idle_sec` (с последнего запроса) и `expires_in_sec` (остаток TTL).
- `GET /gateway/session/{session_id}` возвращает одну полную запись сессии. Её
  `metadata` дополняется на момент чтения теми же длительностями
  `age_sec`/`idle_sec`/`expires_in_sec`. Время жизни сессии считается от
  `created_at`, а не от TTL, который сбрасывается при каждом запросе. Добавьте
  `?pretty=1`, чтобы дополнительно получить `messages_pretty`/`tools_pretty`
  (строки с JSON и отступами) для отображения.

Записи, созданные до появления формы `{metadata, tools, messages}`,
нормализуются при чтении, поэтому оба эндпоинта продолжают работать со старыми
сессиями.

Дашборд `gateway-session-viewer` (см. `docs/DASHBOARDS.md`) отображает эти
эндпоинты как просмотрщик полного диалога.

## Логи

Корзины (buckets) событий в Loki:

- `request_generation` — запросы к `/v1/chat/completions`;
- `request_non_generation` — прочие запросы `/v1/*`;
- `response_generation` — ответы `/v1/chat/completions`, содержащие ответ модели;
- `response_non_generation` — все остальные ответы бэкенда;
- `gateway_error` — сбои, произошедшие до того, как появился ответ бэкенда.

События запросов и ответов включают `request_id`, опциональный `session_id`,
`session_first_request`, `trace_id` и `span_id`.
События запросов генерации включают очищенный `request_json` без тел сообщений,
`tool_call_count` и `fallback_params`, когда некорректные значения сэмплирования
были заменены.
События ответов генерации сохраняют полезную нагрузку бэкенда, `assistant_text`,
тайминги, статус и поля размера.
Потоковые ответы генерации сохраняются как валидный JSON-объект, содержащий
упорядоченные SSE-события.
События непотоковых ответов включают очищенные JSON-нагрузки, когда бэкенд
возвращает JSON.
Чувствительные заголовки, такие как `Authorization`, cookie и API-ключи,
маскируются.

## Трассы

При `GATEWAY_OTEL_ENABLED=true` инструментация FastAPI создаёт HTTP-спаны для
не исключённых маршрутов. Путь chat completion дополнительно создаёт
доменные спаны:

- `llm.gateway.request` — полный запрос chat completion, обработанный шлюзом;
- `llm.backend.request` — вызов бэкенда;
- `llm.session.flow` — работа с сессией на стороне шлюза;
- `valkey.operation` — операции Valkey на уровне сессий;
- `llm.stream_response` — итерирование потокового ответа.

Compose-стек шлюза включает Tempo и OTEL Collector. Атрибуты спанов, семантика
ошибок и советы по поиску — в [Трассах](docs/TRACES.md).
