# Заметки по развёртыванию

Этот документ описывает разделённую схему развёртывания:

- `deploy/llm` запускает ровно один стек LLM-бэкенда: vLLM или SGLang.
- `deploy/gateway` запускает OpenAI-совместимый шлюз и обвязку наблюдаемости
  для шлюза.

Рекомендуемый порядок развёртывания — «сначала бэкенд»:

1. Запустить выбранный стек бэкенда.
2. Проверить бэкенд напрямую.
3. Запустить стек шлюза.
4. Проверить путь через шлюз к уже проверенному бэкенду.

Метрики описаны в [METRICS.md](METRICS.md), трассировка — в
[TRACES.md](TRACES.md). JSON-экспорты дашбордов описаны в
[DASHBOARDS.md](DASHBOARDS.md).

Все пути в этом документе указаны относительно корня репозитория, если команда
явно не меняет каталог.

## Стек LLM

Стек LLM содержит:

- выбранный LLM-движок;
- Prometheus, собирающий метрики бэкенда, метрики узла и метрики GPU из DCGM;
- Node exporter;
- DCGM exporter.

Шлюз намеренно не входит в этот compose-стек.

Создайте локальные настройки LLM:

```bash
cp deploy/llm/.env.example deploy/llm/.env
```

Затем отредактируйте `deploy/llm/.env`:

- укажите локальный путь к модели;
- выберите теги образов;
- оставьте `LLM_HOST=127.0.0.1` и `LLM_HTTP_PORT=9900`, если не хотите
  публиковать API бэкенда иначе.

Запустить vLLM и выполнить прямой smoke-тест бэкенда:

```bash
cd deploy/llm
docker compose --env-file .env -f docker-compose.vllm.yaml up -d --build
docker compose --env-file .env -f docker-compose.vllm.yaml --profile test run --rm llm-smoke-tests
```

Запустить вместо него SGLang и выполнить прямой smoke-тест бэкенда:

```bash
cd deploy/llm
docker compose --env-file .env -f docker-compose.sglang.yaml up -d --build
docker compose --env-file .env -f docker-compose.sglang.yaml --profile test run --rm llm-smoke-tests
```

В один момент времени `127.0.0.1:9900` должен занимать только один вариант
бэкенда.

Полезные URL на стороне LLM:

- OpenAI-совместимый API LLM: `http://127.0.0.1:9900`
- Prometheus LLM: `http://0.0.0.0:9191`

Конфигурации Prometheus:

- vLLM: `deploy/llm/configs/prometheus-vllm.yaml`
- SGLang: `deploy/llm/configs/prometheus-sglang.yaml`

Скрипты запуска:

- `deploy/llm/serve_vllm.sh`
- `deploy/llm/serve_sglang.sh`

## Стек шлюза

Стек шлюза содержит:

- FastAPI-шлюз;
- Valkey для рантайм-отслеживания сессий и просмотра сохранённых чатов;
- Prometheus, собирающий только метрики шлюза;
- Loki для структурированных событий шлюза;
- OpenTelemetry Collector;
- Tempo.

LLM-бэкенд намеренно не входит в этот compose-стек. По умолчанию шлюз
обращается к:

```text
GATEWAY_BACKEND_BASE_URL=http://host.docker.gateway:9900
```

Это соответствует привязке по умолчанию в стеке LLM. Измените значение в
`deploy/gateway/.env`, если бэкенд расположен в другом месте.

Создайте локальные настройки шлюза:

```bash
cp deploy/gateway/.env.example deploy/gateway/.env
```

Затем запустите стек шлюза и выполните smoke-тест шлюза:

```bash
cd deploy/gateway
docker compose --env-file .env -f docker-compose.yaml up -d --build
docker compose --env-file .env -f docker-compose.yaml --profile test run --rm gateway-smoke-tests
```

Полезные URL на стороне шлюза:

- шлюз: `http://0.0.0.0:9090`
- health шлюза: `http://0.0.0.0:9090/health`
- эндпоинт метрик шлюза: `http://0.0.0.0:9090/gateway/metrics`
- прокси метрик бэкенда: `http://0.0.0.0:9090/metrics`
- Prometheus шлюза: `http://0.0.0.0:9091`
- Loki: `http://0.0.0.0:9092`
- Tempo: `http://0.0.0.0:3200`
- эндпоинт коллектора OTLP/gRPC: `0.0.0.0:4317`

Конфигурации шлюза:

- Prometheus: `deploy/gateway/configs/prometheus-gateway.yaml`
- Loki: `deploy/gateway/configs/loki-config.yaml`
- Valkey: `deploy/gateway/configs/valkey.conf`
- OpenTelemetry Collector: `deploy/gateway/configs/otel-collector.yaml`
- Tempo: `deploy/gateway/configs/tempo.yaml`

Grafana не входит в compose-стек. Импортируйте JSON-файлы дашбордов из
`observability/dashboards/` в существующий Grafana или в управляемый workspace
наблюдаемости, когда нужен визуальный интерфейс.

## Проверка

Отрисовать (render) compose-конфигурации:

```bash
cd deploy/llm
docker compose --env-file .env.example -f docker-compose.vllm.yaml config
docker compose --env-file .env.example -f docker-compose.sglang.yaml config

cd ../gateway
docker compose --env-file .env.example -f docker-compose.yaml config
```

Smoke-проверки после запуска:

```bash
curl -fsS http://127.0.0.1:9900/v1/models
curl -fsS http://127.0.0.1:9090/health
curl -fsS http://127.0.0.1:9090/gateway/metrics
curl -fsS http://127.0.0.1:9090/metrics
curl -fsS http://127.0.0.1:9090/v1/models
```

`http://127.0.0.1:9090/metrics` проксирует эндпоинт метрик бэкенда и возвращает
`503`, когда бэкенд недоступен. Это отдельный эндпоинт от собственных метрик
шлюза на `/gateway/metrics`.

## Smoke-тесты в Compose

Compose-файлы содержат опциональные сервисы-раннеры тестов под профилем `test`.
Они отправляют один непотоковый OpenAI-совместимый запрос chat completion и
завершаются с ошибкой, когда бэкенд/шлюз не возвращает корректный ответ. При
`SMOKE_CHECK_TOOLS=true` они дополнительно отправляют принудительный
OpenAI-совместимый запрос с вызовом инструмента и завершаются с ошибкой, если в
ответе нет корректных `tool_calls` с JSON-аргументами функции.

Обычный сценарий работы:

1. Запустить стек бэкенда в фоновом режиме (detached).
2. Запустить сервис smoke-тестов бэкенда как одноразовый контейнер.
3. Запустить стек шлюза в фоновом режиме.
4. Запустить сервис smoke-тестов шлюза как одноразовый контейнер.
5. Оставить стеки работающими после завершения тестовых контейнеров.

Прямые smoke-тесты бэкенда запускайте из `deploy/llm`.

Для vLLM:

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

Вариант одной командой:

```bash
docker compose --env-file .env -f docker-compose.vllm.yaml up -d --build && docker compose --env-file .env -f docker-compose.vllm.yaml --profile test run --rm llm-smoke-tests
```

Для SGLang:

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

Вариант одной командой:

```bash
docker compose --env-file .env -f docker-compose.sglang.yaml up -d --build && docker compose --env-file .env -f docker-compose.sglang.yaml --profile test run --rm llm-smoke-tests
```

Smoke-тесты шлюза запускайте из `deploy/gateway` после того, как стек LLM стал
доступен по адресу из `GATEWAY_BACKEND_BASE_URL`:

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

Вариант одной командой:

```bash
docker compose --env-file .env -f docker-compose.yaml up -d --build && docker compose --env-file .env -f docker-compose.yaml --profile test run --rm gateway-smoke-tests
```

`run --rm` возвращает код выхода pytest и удаляет только завершившийся тестовый
контейнер. Он не останавливает бэкенд, шлюз, Prometheus, Loki, Tempo или
экспортеры.

Промпт, модель, таймаут и опциональный API-ключ передаются в контейнер
smoke-тестов через `env_file: .env`:

- `SMOKE_MODEL`
- `SMOKE_PROMPT`
- `SMOKE_TIMEOUT_SEC`
- `SMOKE_API_KEY`
- `SMOKE_CHECK_TOOLS`

Задавайте `SMOKE_CHECK_TOOLS=true` только тогда, когда вызов инструментов
входит в ожидаемый рантайм-контракт и выбранный бэкенд был запущен с поддержкой
tool-call. Для развёртываний, которым нужны только обычные chat completions,
оставьте `false`.
