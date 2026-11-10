# Заметки по развёртыванию

Этот документ описывает разделённую схему развёртывания из трёх стеков:

- `deploy/llm` — ровно один стек LLM-бэкенда (vLLM или SGLang) плюс экспортеры
  узла и GPU. Запускается на **каждой машине с движком**.
- `deploy/gateway` — OpenAI-совместимый шлюз и его агент метрик. Запускается на
  **каждой машине с движком**, по одному экземпляру на процесс движка.
- `deploy/observability` — Valkey, Loki, Tempo, OpenTelemetry Collector и
  VictoriaMetrics. Запускается **один раз на весь парк машин**.

```text
  МАШИНА С ДВИЖКОМ (xN)                      ЦЕНТРАЛЬНАЯ МАШИНА
 ┌──────────────────────────┐               ┌────────────────────────────┐
 │  deploy/gateway          │─── push ─────▶│  Loki                      │
 │  GATEWAY_ENGINE_ID=...   │─── push ─────▶│  OTEL Collector ──▶ Tempo  │
 │         │                │─── чтение/────│                            │
 │         │                │    запись ───▶│  Valkey                    │
 │  gateway-vmagent         │─── push ─────▶│  VictoriaMetrics           │
 │         ▼                │               │                            │
 │  deploy/llm (движок)     │               │                            │
 │  llm-vmagent             │─── push ─────▶│  VictoriaMetrics           │
 └──────────────────────────┘               └────────────────────────────┘
```

Всё идёт от машин к центру, обратно — ничего. Центральному стеку не нужно знать
адреса машин: новая машина подключается тем, что прописывает адрес центра в
своих `.env`.

Разделение решает конкретную задачу: балансер перед шлюзами раскидывает ходы
одной сессии по разным машинам. Общий Valkey делает так, что все шлюзы
дописывают один и тот же транскрипт, и диалог остаётся целым; общие Loki и
Tempo собирают события и трассы всех движков в одном месте, разделяя их меткой
`engine`.

**Стек шлюза не зависит от стека наблюдаемости.** Шлюз стартует и проксирует,
даже когда `deploy/observability` не поднят: доставка в Loki и Tempo — push,
её отказ только считается в метриках, агент метрик — отдельный контейнер, от
которого шлюз не зависит, а вызовы Valkey закрывает предохранитель (см.
«Работа без стека наблюдаемости»).

Рекомендуемый порядок развёртывания — «сначала бэкенд»:

1. Собрать образы и зафиксировать их версию в `.env` (см. «Образы»).
2. Запустить стек наблюдаемости на центральной машине.
3. Запустить выбранный стек бэкенда на машине с движком.
4. Проверить бэкенд напрямую.
5. Запустить стек шлюза на той же машине.
6. Проверить путь через шлюз к уже проверенному бэкенду.

Метрики описаны в [METRICS.md](METRICS.md), трассировка — в
[TRACES.md](TRACES.md). JSON-экспорты дашбордов описаны в
[DASHBOARDS.md](DASHBOARDS.md).

Все пути в этом документе указаны относительно корня репозитория, если команда
явно не меняет каталог.

## Образы

Compose-файлы ничего не собирают: образ шлюза и образ smoke-тестов называются в
`.env`, а `docker compose up -d` запускает ровно тот тег, который там записан.
Собирает их один скрипт:

```bash
deploy/build_images.sh
```

Он:

1. берёт версию `<дата UTC>-<короткий sha коммита>`, например
   `20261110-6d94998`, так что тег всегда называет коммит, из которого собран
   образ;
2. собирает `llm-gateway:<версия>` из `gateway/` и
   `llm-gateway-smoke-tests:<версия>` из `deploy/tests/Dockerfile`;
3. записывает теги в `deploy/gateway/.env` (`GATEWAY_IMAGE`,
   `GATEWAY_SMOKE_TESTS_IMAGE`) и `deploy/llm/.env` (`LLM_SMOKE_TESTS_IMAGE`) —
   в те из них, что существуют.

Незакоммиченные изменения в `gateway/` или `deploy/tests/` останавливают сборку:
иначе тег назвал бы коммит, в котором нет кода из образа. `--allow-dirty`
собирает их всё равно и добавляет к версии `-dirty`.

Чтобы развезти образы по машинам, которые сами не собирают, скрипт может
сохранить оба образа в один архив — каталог задаётся `--save-dir` или
переменной `IMAGE_SAVE_DIR`; без них архив не пишется:

```bash
deploy/build_images.sh --save-dir /some/dir
# на другой машине:
docker load -i /some/dir/llm-gateway-<версия>.tar.gz
```

Архив пишется под временным именем и переименовывается только целиком, так что
недописанный файл загрузить нельзя. Если установлен `pv`, сохранение идёт с
прогресс-баром. Имена образов меняются переменными `GATEWAY_IMAGE_NAME` и
`SMOKE_TESTS_IMAGE_NAME`.

На машине, куда образ загружен через `docker load`, впишите тот же тег в `.env`
руками или скопируйте эти строки из `.env` машины, где шла сборка.

## Стек LLM

Стек LLM содержит:

- выбранный LLM-движок;
- Node exporter;
- DCGM exporter;
- `llm-vmagent` — агент, который собирает метрики узла и GPU этой машины и
  отправляет их в центральный VictoriaMetrics.

Шлюз намеренно не входит в этот compose-стек. Метрики узла и GPU не ждут, пока
их заберёт центр: `llm-vmagent` читает их локально (Node exporter — через
host-сеть, DCGM exporter — по compose-сети, наружу его порт не публикуется) и
сам отправляет в центр с меткой `host` из `LLM_HOST_ID`. Метрики самого движка
сюда не входят: их собирает агент шлюза через `/metrics` шлюза — так у них
оказывается та же метка `engine`, что и у метрик шлюза.

Создайте локальные настройки LLM:

```bash
cp deploy/llm/.env.example deploy/llm/.env
cp deploy/llm/serve_vllm.example.sh deploy/llm/serve_vllm.sh
cp deploy/llm/serve_sglang.example.sh deploy/llm/serve_sglang.sh
```

Compose-файлы от машины к машине не меняются: всё локальное задаётся в `.env`,
скриптах запуска и конфигах, а их рабочие копии в git не попадают.

Затем отредактируйте `deploy/llm/.env`:

- укажите локальный путь к модели;
- если нужен LoRA-адаптер vLLM, укажите его директорию в
  `LLM_VLLM_ADAPTER_MODEL_PATH` (она монтируется в `/adapter_model`) и включите
  `ENABLE_LORA`/`LORA_MODULES` в `serve_vllm.sh`; пустое значение — адаптера
  нет, на его место монтируется `/dev/null`;
- выберите теги образов (`LLM_SMOKE_TESTS_IMAGE` пишет `build_images.sh`);
- задайте `LLM_HOST_ID` — имя машины, то же, что `GATEWAY_HOST_ID` у шлюзов на
  ней, — и адрес центрального VictoriaMetrics в `LLM_METRICS_PUSH_URL`;
- оставьте `LLM_HOST=127.0.0.1` и `LLM_HTTP_PORT=9900`, если не хотите
  публиковать API бэкенда иначе.

Запустить vLLM и выполнить прямой smoke-тест бэкенда:

```bash
cd deploy/llm
docker compose --env-file .env -f docker-compose.vllm.yaml up -d
docker compose --env-file .env -f docker-compose.vllm.yaml --profile test run --rm llm-smoke-tests
```

Запустить вместо него SGLang и выполнить прямой smoke-тест бэкенда:

```bash
cd deploy/llm
docker compose --env-file .env -f docker-compose.sglang.yaml up -d
docker compose --env-file .env -f docker-compose.sglang.yaml --profile test run --rm llm-smoke-tests
```

В один момент времени `127.0.0.1:9900` должен занимать только один вариант
бэкенда. Node exporter, DCGM exporter и `llm-vmagent` рассчитаны на один
экземпляр на машину: метрики узла и GPU принадлежат машине, а не движку.

Полезные URL на стороне LLM:

- OpenAI-совместимый API LLM: `http://127.0.0.1:9900`
- Node exporter: `http://127.0.0.1:9100/metrics`

Скрипты запуска (локальные копии `*.example.sh`):

- `deploy/llm/serve_vllm.sh`
- `deploy/llm/serve_sglang.sh`

Что и с какими метками собирает `llm-vmagent` — в `deploy/llm/configs/vmagent.yaml`.
Файл одинаковый на всех машинах: всё машинное в него подставляется из окружения.

## Стек наблюдаемости

Стек `deploy/observability` содержит:

- Valkey — рантайм-отслеживание сессий (DB 0) и сохранённые транскрипты (DB 1);
- Loki — структурированные события всех шлюзов;
- Tempo и OpenTelemetry Collector — трассы всех шлюзов;
- VictoriaMetrics — метрики, которые присылают агенты всех машин.

Он запускается **один раз**, на центральной машине. Valkey живёт здесь, потому
что он должен быть общим: именно общий Valkey склеивает диалог, ходы которого
балансер раскидал по разным машинам.

```bash
cp deploy/observability/.env.example deploy/observability/.env
cp deploy/observability/configs/valkey.example.conf deploy/observability/configs/valkey.conf
# отредактируйте OBSERVABILITY_HOST: адрес, доступный машинам с движками,
# пути *_VOLUME и замените requirepass в configs/valkey.conf
mkdir -p <каталоги из OBSERVABILITY_*_VOLUME>
cd deploy/observability
docker compose --env-file .env -f docker-compose.yaml up -d
```

Списка машин здесь нет нигде. Все данные, включая метрики, приходят по push и
уже с метками: Loki и Tempo получают `engine` от шлюзов, а метрики — от агентов
на машинах (`gateway-vmagent` и `llm-vmagent`). Меток две, и они намеренно
разные:

- `engine` — один процесс шлюза перед одним процессом движка (job `gateway`,
  job `engine`), из `GATEWAY_ENGINE_ID`;
- `host` — физическая машина (у всех четырёх job, у `node` и `gpu` —
  единственная): метрики узла и GPU принадлежат машине, а не одному из её
  движков. Берётся из `GATEWAY_HOST_ID` и `LLM_HOST_ID`, которые на одной машине
  должны совпадать.

VictoriaMetrics принимает запись на `/api/v1/write` и отвечает Grafana как
обычный Prometheus-источник на том же порту.

### Тома и права

Все сервисы, которые хранят данные (Valkey, Loki, VictoriaMetrics, Tempo),
запускаются от `OBSERVABILITY_RUN_AS`, а не от пользователя своего образа, и
каждый каталог из `*_VOLUME` должен быть доступен ему на запись.

По умолчанию это `65534:65534` (nobody), и это рассчитано на NFS с
`root_squash`. На таком экспорте root — и на хосте, и в контейнере —
превращается в nobody и не может делать `chown`, а все остальные uid остаются
собой. Поэтому:

- каталог, созданный root через `mkdir -p`, уже принадлежит nobody, и сервис,
  запущенный от nobody, пишет в него без каких-либо `chown`;
- пользователь образа (999 у Valkey, 10001 у Loki и Tempo) в такой каталог
  писать не смог бы, а `chown` в entrypoint на NFS падает с
  `Operation not permitted`.

На локальном диске каталоги нужно отдать этому пользователю явно:
`chown 65534:65534 <каталог>`.

NFS заметно медленнее локального диска на синхронных записях и создании мелких
файлов. Valkey (AOF с `appendfsync everysec`), Tempo и Loki при наших объёмах
это переносят. Prometheus на NFS официально не поддерживается и может
повредить данные; отчасти поэтому метрики хранит VictoriaMetrics, которая
допускает сетевое хранилище.

Полезные URL на центральной машине:

- VictoriaMetrics: `http://0.0.0.0:8428` (запись — `/api/v1/write`, UI —
  `/vmui`)
- Loki: `http://0.0.0.0:3100`
- Tempo: `http://0.0.0.0:3200`
- эндпоинт коллектора OTLP/gRPC: `0.0.0.0:4317`
- Valkey: `0.0.0.0:6379`

Ни один из этих сервисов по умолчанию не аутентифицирует клиентов. Держите их в
приватной сети и задайте `requirepass` в `configs/valkey.conf`: в Valkey лежат
полные тексты диалогов и вызовы инструментов. Тот же пароль шлюзы передают в
`GATEWAY_VALKEY_URL` (`redis://:пароль@хост:6379`); проще всего взять пароль
только из букв и цифр (`openssl rand -hex 32`), чтобы его не пришлось
кодировать в URL.

Конфигурации (`valkey.conf` — локальная копия `valkey.example.conf`, в git не
попадает; у VictoriaMetrics файла нет, всё задано флагами из `.env`):

- Loki: `deploy/observability/configs/loki-config.yaml`
- Valkey: `deploy/observability/configs/valkey.conf`
- OpenTelemetry Collector: `deploy/observability/configs/otel-collector.yaml`
- Tempo: `deploy/observability/configs/tempo.yaml`

Grafana не входит в compose-стек. Импортируйте JSON-файлы дашбордов из
`observability/dashboards/` в существующий Grafana или в управляемый workspace
наблюдаемости, когда нужен визуальный интерфейс. Prometheus-источник Grafana
направьте на VictoriaMetrics: `http://<центральная машина>:8428`.

## Стек шлюза

Стек `deploy/gateway` содержит FastAPI-шлюз и `gateway-vmagent` — агент,
который читает метрики своего шлюза по compose-сети и отправляет их в
центральный VictoriaMetrics. Ни LLM-бэкенд, ни сервисы наблюдаемости в него не
входят: первый живёт в `deploy/llm` на той же машине, вторые — в
`deploy/observability` на центральной.

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

- задайте `GATEWAY_ENGINE_ID` и `GATEWAY_HOST_ID` (имя машины, то же, что
  `LLM_HOST_ID`);
- укажите адрес центральной машины (и пароль Valkey) в `GATEWAY_VALKEY_URL`,
  `GATEWAY_LOKI_PUSH_URL`, `GATEWAY_OTEL_EXPORTER_OTLP_ENDPOINT` и
  `GATEWAY_METRICS_PUSH_URL`;
- проверьте `GATEWAY_BACKEND_BASE_URL` и `GATEWAY_HTTP_PORT`;
- `GATEWAY_IMAGE` и `GATEWAY_SMOKE_TESTS_IMAGE` пишет `build_images.sh`; на
  машине, куда образы загружены через `docker load`, впишите их теги сами.

Запустите стек шлюза и выполните smoke-тест шлюза:

```bash
cd deploy/gateway
docker compose --env-file .env -f docker-compose.yaml up -d
docker compose --env-file .env -f docker-compose.yaml --profile test run --rm gateway-smoke-tests
```

Машина с двумя движками поднимает два стека шлюза, и у каждого свой
`gateway-vmagent` со своим `GATEWAY_VMAGENT_CONTAINER_NAME`: агент собирает
только свой шлюз, так что метрики двух движков не смешиваются.

### Юнит-тесты шлюза

Логика чтения ответа бэкенда, разбиения обмена на блоки сессии и сборки
сессии при чтении покрыта юнит-тестами, которым не нужны ни бэкенд, ни Valkey,
ни докер:

```bash
cd gateway
pip install -r requirements-dev.txt
pytest
```

Сами команды Valkey проверяет `tests/test_session_store_valkey.py`. Он
запускается, только если `GATEWAY_TEST_VALKEY_URL` указывает на одноразовый
Valkey, например `redis://127.0.0.1:6390/15`, и пропускается в остальных
случаях.

Smoke-тесты (`--profile test`) по-прежнему проверяют шлюз целиком против живого
бэкенда. Проверки вокруг инструментов включаются `SMOKE_CHECK_TOOLS=true`.

Полезные URL на стороне шлюза:

- шлюз: `http://0.0.0.0:9090`
- health шлюза: `http://0.0.0.0:9090/health`
- эндпоинт метрик шлюза: `http://0.0.0.0:9090/gateway/metrics`
- прокси метрик бэкенда: `http://0.0.0.0:9090/metrics`

Сам шлюз настраивается только через `.env`. Единственный файл конфигурации
стека — `deploy/gateway/configs/vmagent.yaml`, что собирает агент; он одинаковый
на всех машинах, машинное подставляется из окружения.

### Работа без стека наблюдаемости

Шлюз спроектирован так, чтобы `deploy/observability` был необязательным. Ни
одна его часть не находится на пути запроса в блокирующем виде:

| Зависимость | Модель | Что при отказе |
| --- | --- | --- |
| Loki | push | Событие отбрасывается, счётчики `gateway_loki_push_total{status="error"}` и `gateway_loki_events_dropped_total` растут. Запрос не затронут. |
| Tempo | push | `BatchSpanProcessor` отправляет в фоне; спаны теряются, запрос не затронут. |
| Valkey | чтение/запись | Первые отказы стоят 2 с таймаута, дальше срабатывает предохранитель и вызовы перестают уходить в сеть. |
| VictoriaMetrics | push через `gateway-vmagent` | Агент держит неотправленное в памяти, затем отбрасывает; шлюз ничего не замечает. |

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

# агенты всех машин присылают все четыре job, у каждой цели есть host,
# у gateway и engine ещё и engine; value "1" — цель отвечает
curl -sG http://127.0.0.1:8428/api/v1/query --data-urlencode 'query=up' \
  | jq '.data.result[] | {job: .metric.job, engine: .metric.engine, host: .metric.host, up: .value[1]}'
```

Свежие точки появляются в запросах с задержкой около 30 секунд
(`-search.latencyOffset` VictoriaMetrics по умолчанию).

Главная проверка всей схемы — что транскрипт собирается через разные машины.
Отправьте три хода одной сессии с общим `X-Session-ID` через балансер в режиме
round-robin, затем запросите диалог у **любого** шлюза:

```bash
curl -s http://10.0.0.1:9090/gateway/session/probe-1 | jq '.metadata.turn_cnt'
```

`3` в ответе означает, что ходы, разъехавшиеся по машинам, попали в одну
сессию общего Valkey. Список сессий и любую отдельную сессию отдаёт любой
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
  up -d

docker compose \
  --env-file .env \
  -f docker-compose.vllm.yaml \
  --profile test \
  run --rm llm-smoke-tests
```

Вариант одной командой:

```bash
docker compose --env-file .env -f docker-compose.vllm.yaml up -d && docker compose --env-file .env -f docker-compose.vllm.yaml --profile test run --rm llm-smoke-tests
```

Для SGLang:

```bash
docker compose \
  --env-file .env \
  -f docker-compose.sglang.yaml \
  up -d

docker compose \
  --env-file .env \
  -f docker-compose.sglang.yaml \
  --profile test \
  run --rm llm-smoke-tests
```

Вариант одной командой:

```bash
docker compose --env-file .env -f docker-compose.sglang.yaml up -d && docker compose --env-file .env -f docker-compose.sglang.yaml --profile test run --rm llm-smoke-tests
```

Smoke-тесты шлюза запускайте из `deploy/gateway` после того, как стек LLM стал
доступен по адресу из `GATEWAY_BACKEND_BASE_URL`:

```bash
docker compose \
  --env-file .env \
  -f docker-compose.yaml \
  up -d

docker compose \
  --env-file .env \
  -f docker-compose.yaml \
  --profile test \
  run --rm gateway-smoke-tests
```

Вариант одной командой:

```bash
docker compose --env-file .env -f docker-compose.yaml up -d && docker compose --env-file .env -f docker-compose.yaml --profile test run --rm gateway-smoke-tests
```

`run --rm` возвращает код выхода pytest и удаляет только завершившийся тестовый
контейнер. Он не останавливает бэкенд, шлюз, агенты метрик, Loki, Tempo или
экспортеры.

Оба сервиса запускаются из одного образа smoke-тестов (`deploy/tests/Dockerfile`,
собирает `build_images.sh`), но запускают разные наборы, потому что у бэкенда и шлюза разные контракты:

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
