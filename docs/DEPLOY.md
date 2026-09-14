# Заметки по развёртыванию

Этот документ описывает разделённую схему развёртывания из трёх стеков:

- `deploy/llm` — ровно один стек LLM-бэкенда (vLLM или SGLang). Запускается на
  **каждой машине с движком**.
- `deploy/gateway` — только OpenAI-совместимый шлюз. Запускается на **каждой
  машине с движком**, по одному экземпляру на процесс движка.
- `deploy/observability` — Valkey, Loki, Tempo, OpenTelemetry Collector и
  Prometheus. Запускается **один раз на весь парк машин**.

```text
  МАШИНА С ДВИЖКОМ (xN)                      ЦЕНТРАЛЬНАЯ МАШИНА
 ┌──────────────────────────┐               ┌────────────────────────────┐
 │  deploy/gateway          │─── push ─────▶│  Loki                      │
 │  GATEWAY_ENGINE_ID=...   │─── push ─────▶│  OTEL Collector ──▶ Tempo  │
 │         │                │─── чтение/────│                            │
 │         │                │    запись ───▶│  Valkey                    │
 │         ▼                │◀── scrape ────│  Prometheus                │
 │  deploy/llm (движок)     │               │                            │
 └──────────────────────────┘               └────────────────────────────┘
```

Разделение решает конкретную задачу: балансер перед шлюзами раскидывает ходы
одной сессии по разным машинам. Общий Valkey делает так, что все шлюзы
дописывают один и тот же транскрипт, и диалог остаётся целым; общие Loki и
Tempo собирают события и трассы всех движков в одном месте, разделяя их меткой
`engine`.

**Стек шлюза не зависит от стека наблюдаемости.** Шлюз стартует и проксирует,
даже когда `deploy/observability` не поднят: доставка в Loki и Tempo — push,
её отказ только считается в метриках, а вызовы Valkey закрывает предохранитель
(см. «Работа без стека наблюдаемости»).

Рекомендуемый порядок развёртывания — «сначала бэкенд»:

1. Запустить стек наблюдаемости на центральной машине.
2. Запустить выбранный стек бэкенда на машине с движком.
3. Проверить бэкенд напрямую.
4. Запустить стек шлюза на той же машине.
5. Проверить путь через шлюз к уже проверенному бэкенду.

Метрики описаны в [METRICS.md](METRICS.md), трассировка — в
[TRACES.md](TRACES.md). JSON-экспорты дашбордов описаны в
[DASHBOARDS.md](DASHBOARDS.md).

Все пути в этом документе указаны относительно корня репозитория, если команда
явно не меняет каталог.

## Стек LLM

Стек LLM содержит:

- выбранный LLM-движок;
- Node exporter;
- DCGM exporter.

Шлюз намеренно не входит в этот compose-стек. Своего Prometheus у стека LLM
тоже больше нет: метрики движка, узла и GPU забирает центральный Prometheus из
`deploy/observability`. Поэтому порт DCGM-экспортера публикуется на хосте через
`LLM_DCGM_EXPORTER_PORT` (по умолчанию `9400`), а метрики самого движка
собираются не напрямую, а через `/metrics` шлюза — так у них оказывается та же
метка `engine`, что и у метрик шлюза.

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
- Node exporter: `http://127.0.0.1:9100/metrics`
- DCGM exporter: `http://127.0.0.1:9400/metrics`

Скрипты запуска:

- `deploy/llm/serve_vllm.sh`
- `deploy/llm/serve_sglang.sh`

## Стек наблюдаемости

Стек `deploy/observability` содержит:

- Valkey — рантайм-отслеживание сессий (DB 0) и сохранённые транскрипты (DB 1);
- Loki — структурированные события всех шлюзов;
- Tempo и OpenTelemetry Collector — трассы всех шлюзов;
- Prometheus — единственный скрейпер на весь парк.

Он запускается **один раз**, на центральной машине. Valkey живёт здесь, потому
что он должен быть общим: именно общий Valkey склеивает диалог, ходы которого
балансер раскидал по разным машинам.

```bash
cp deploy/observability/.env.example deploy/observability/.env
# отредактируйте OBSERVABILITY_HOST: адрес, доступный машинам с движками
cd deploy/observability
docker compose --env-file .env -f docker-compose.yaml up -d
```

Затем перечислите машины в `deploy/observability/configs/prometheus.yaml` —
это единственное место во всей системе, где имена движков указываются руками.
Loki и Tempo получают метку `engine` вместе с самими данными (шлюз их
push-ит), а Prometheus скрейпит и потому обязан знать адреса заранее. Метки в
файле две, и они намеренно разные:

- `engine` — один процесс шлюза перед одним процессом движка (job `gateway`,
  job `engine`);
- `host` — физическая машина (job `node`, job `gpu`): метрики узла и GPU
  принадлежат машине, а не тому из её движков, который окажется первым в списке.

Prometheus запущен с `--web.enable-lifecycle`, поэтому после правки целей
рестарт не нужен:

```bash
curl -XPOST http://127.0.0.1:9091/-/reload
```

Полезные URL на центральной машине:

- Prometheus: `http://0.0.0.0:9091`
- Loki: `http://0.0.0.0:3100`
- Tempo: `http://0.0.0.0:3200`
- эндпоинт коллектора OTLP/gRPC: `0.0.0.0:4317`
- Valkey: `0.0.0.0:6379`

Ни один из этих сервисов по умолчанию не аутентифицирует клиентов. Держите их в
приватной сети и задайте `requirepass` в `configs/valkey.conf`: в Valkey лежат
полные тексты диалогов и вызовы инструментов.

Конфигурации:

- Prometheus: `deploy/observability/configs/prometheus.yaml`
- Loki: `deploy/observability/configs/loki-config.yaml`
- Valkey: `deploy/observability/configs/valkey.conf`
- OpenTelemetry Collector: `deploy/observability/configs/otel-collector.yaml`
- Tempo: `deploy/observability/configs/tempo.yaml`

Grafana не входит в compose-стек. Импортируйте JSON-файлы дашбордов из
`observability/dashboards/` в существующий Grafana или в управляемый workspace
наблюдаемости, когда нужен визуальный интерфейс.

## Стек шлюза

Стек `deploy/gateway` содержит только FastAPI-шлюз. Ни LLM-бэкенд, ни сервисы
наблюдаемости в него не входят: первый живёт в `deploy/llm` на той же машине,
вторые — в `deploy/observability` на центральной.

Запускается по одному экземпляру **на процесс движка**. По правилу «один
процесс vLLM = один порт = одна модель» машина с двумя движками поднимает два
шлюза с разными `GATEWAY_ENGINE_ID` и разными `GATEWAY_HTTP_PORT`.

### GATEWAY_ENGINE_ID

Обязательная переменная. Она задаёт метку `engine` в каждом потоке Loki и
атрибут ресурса `service.instance.id` в каждой трассе. Шлюз **не стартует**
без неё: пустое значение не сломало бы ничего заметно — оно тихо слило бы
телеметрию всех машин в одну безымянную кучу, и ошибка всплыла бы месяцы спустя
в дашборде, который невозможно разрезать.

Значение именует процесс движка, а не машину: `rtx6000a-8001`,
`rtx6000a-8002`, `rtx4090b-8001`.

По умолчанию шлюз обращается к:

```text
GATEWAY_BACKEND_BASE_URL=http://host.docker.gateway:9900
```

Это соответствует привязке по умолчанию в стеке LLM. Измените значение в
`deploy/gateway/.env`, если бэкенд расположен в другом месте.

Создайте локальные настройки шлюза:

```bash
cp deploy/gateway/.env.example deploy/gateway/.env
```

Затем отредактируйте `deploy/gateway/.env`:

- задайте `GATEWAY_ENGINE_ID`;
- укажите адрес центральной машины в `GATEWAY_VALKEY_URL`,
  `GATEWAY_LOKI_PUSH_URL` и `GATEWAY_OTEL_EXPORTER_OTLP_ENDPOINT`;
- проверьте `GATEWAY_BACKEND_BASE_URL` и `GATEWAY_HTTP_PORT`.

Запустите стек шлюза и выполните smoke-тест шлюза:

```bash
cd deploy/gateway
docker compose --env-file .env -f docker-compose.yaml up -d --build
docker compose --env-file .env -f docker-compose.yaml --profile test run --rm gateway-smoke-tests
```

### Юнит-тесты шлюза

Логика чтения ответа бэкенда и сведения транскрипта покрыта юнит-тестами,
которым не нужны ни бэкенд, ни Valkey, ни докер:

```bash
cd gateway
pip install -r requirements-dev.txt
pytest
```

Smoke-тесты (`--profile test`) по-прежнему проверяют шлюз целиком против живого
бэкенда. Проверки вокруг инструментов включаются `SMOKE_CHECK_TOOLS=true`.

Полезные URL на стороне шлюза:

- шлюз: `http://0.0.0.0:9090`
- health шлюза: `http://0.0.0.0:9090/health`
- эндпоинт метрик шлюза: `http://0.0.0.0:9090/gateway/metrics`
- прокси метрик бэкенда: `http://0.0.0.0:9090/metrics`

Собственных конфигурационных файлов у стека шлюза нет — всё задаётся через
`.env`.

### Работа без стека наблюдаемости

Шлюз спроектирован так, чтобы `deploy/observability` был необязательным. Ни
одна его часть не находится на пути запроса в блокирующем виде:

| Зависимость | Модель | Что при отказе |
| --- | --- | --- |
| Loki | push | Событие отбрасывается, счётчики `gateway_loki_push_total{status="error"}` и `gateway_loki_events_dropped_total` растут. Запрос не затронут. |
| Tempo | push | `BatchSpanProcessor` отправляет в фоне; спаны теряются, запрос не затронут. |
| Valkey | чтение/запись | Первые отказы стоят 2 с таймаута, дальше срабатывает предохранитель и вызовы перестают уходить в сеть. |
| Prometheus | pull | Скрейпа просто нет; шлюз ничего не замечает. |

Предохранитель Valkey стоит того, чтобы его понимать. Без него недоступный
Valkey стоил бы по 2 с таймаута дважды на каждый чат-запрос: один раз в
`mark_seen` до обращения к бэкенду и один раз при записи транскрипта — а она
для непотокового ответа выполняется **до** возврата ответа клиенту. Шлюз
формально работал бы, практически — был бы непригоден. После
`GATEWAY_VALKEY_BREAKER_FAILURES` подряд неудач вызовы перестают уходить в
сеть на `GATEWAY_VALKEY_BREAKER_COOLDOWN_SEC` секунд, затем пропускается один
пробный вызов; успех закрывает предохранитель. Ошибка `WatchError` не
считается: она означает, что Valkey ответил, а запись изменилась под
оптимистичной транзакцией — это конкуренция, а не отказ.

Полностью отключить работу с сессиями:

```bash
GATEWAY_SESSIONS_ENABLED=false
```

Тогда шлюз не делает к Valkey ни одного сетевого вызова, транскрипты не
сохраняются, первый запрос сессии не определяется, а `/gateway/session_list` и
`/gateway/session/{id}` отвечают `503`. Smoke-набор
`tests/smoke/test_gateway_sessions.py` при этом помечается как пропущенный.

Текущее состояние видно в метрике:

```text
gateway_dependency_up{dependency="valkey_runtime"}
gateway_dependency_up{dependency="valkey_store"}
```

Без неё «сессии не записывались» и «сессий не было» выглядят одинаково.

## Проверка

Отрисовать (render) compose-конфигурации:

```bash
cd deploy/llm
docker compose --env-file .env.example -f docker-compose.vllm.yaml config
docker compose --env-file .env.example -f docker-compose.sglang.yaml config

cd ../gateway
docker compose --env-file .env.example -f docker-compose.yaml config

cd ../observability
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

Проверки распределённой схемы — с центральной машины:

```bash
# метка engine доехала в Loki
curl -sG http://10.0.0.100:3100/loki/api/v1/label/engine/values

# Prometheus видит все четыре job и у всех есть engine либо host
curl -s http://127.0.0.1:9091/api/v1/targets \
  | jq '.data.activeTargets[] | {job: .labels.job, engine: .labels.engine, host: .labels.host, health}'
```

Главная проверка всей схемы — что транскрипт собирается через разные машины.
Отправьте три хода одной сессии с общим `X-Session-ID` через балансер в режиме
round-robin, затем запросите диалог у **любого** шлюза:

```bash
curl -s http://10.0.0.1:9090/gateway/session/probe-1 | jq '.messages | length'
```

Полная история в ответе означает, что общий Valkey склеил диалог, ходы которого
разъехались по машинам. Список сессий и любую отдельную сессию отдаёт любой
шлюз: Valkey у них один.

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

Оба сервиса собираются из одного образа `deploy/tests/Dockerfile`, но
запускают разные наборы, потому что у бэкенда и шлюза разные контракты:

- `llm-smoke-tests` → `tests/smoke/test_backend_contract.py`: OpenAI-совместимый
  контракт `/v1` (непотоковый ответ, SSE-стрим, tool calling, отсутствие
  reasoning-трейса).
- `gateway-smoke-tests` → тот же контрактный набор плюс
  `tests/smoke/test_gateway_sessions.py`: персистенция диалога в Valkey через
  `/gateway/session/{session_id}` и `/gateway/session_list`, включая проверку,
  что assistant-турн сохраняется даже когда клиент перестаёт читать SSE сразу
  после `[DONE]`. Набор целиком пропускается при `GATEWAY_SESSIONS_ENABLED=false`:
  шлюзу без стека наблюдаемости хранить транскрипты негде.

Набор выбирается через `command` сервиса в compose-файле, а не через переменные
окружения.

Промпт, модель, таймаут и опциональный API-ключ передаются в контейнер
smoke-тестов через `env_file: .env`:

- `SMOKE_MODEL`
- `SMOKE_PROMPT`
- `SMOKE_TIMEOUT_SEC`
- `SMOKE_API_KEY`
- `SMOKE_CHECK_TOOLS`
- `SMOKE_CHECK_THINKING`

Задавайте `SMOKE_CHECK_TOOLS=true` только тогда, когда вызов инструментов
входит в ожидаемый рантайм-контракт и выбранный бэкенд был запущен с поддержкой
tool-call. Для развёртываний, которым нужны только обычные chat completions,
оставьте `false`.
