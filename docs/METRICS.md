# Метрики

Шлюз отдаёт собственные Prometheus-метрики:

```text
GET /gateway/metrics
```

Он также проксирует эндпоинт метрик бэкенда:

```text
GET /metrics
```

`GET /metrics` перенаправляет запрос на собственный эндпоинт `/metrics` бэкенда
и возвращает его ответ без изменений (статус, тело и `content-type`). Когда
бэкенд недоступен, возвращается `503` с телом `{"ok": false, "backend":
"unavailable", "detail": ...}`.

Prometheus в стеке шлюза по-прежнему собирает только `/gateway/metrics`, а
метрики бэкенда собираются напрямую с выбранного сервиса бэкенда (см.
[Метрики бэкенда](#метрики-бэкенда)). Прокси `/metrics` — это удобный способ
добраться до метрик бэкенда через шлюз, а не цель сбора (scrape target) для
Prometheus шлюза.

Используйте метрики для агрегированного поведения и алертов. Для тайминга
отдельных запросов и разбора на уровне спанов см. [TRACES.md](TRACES.md).

## Метрики шлюза

`gateway_requests_total`

- Счётчик всех запросов, принятых обработчиками маршрутов шлюза.
- Инкрементируется один раз после того, как шлюз определил маршрут и режим
  потоковости.
- Включает чат-запросы с некорректным JSON, на которые шлюз сам отвечает `400`.
- Метки: `route`, `method`, `stream`.

`gateway_responses_total`

- Счётчик терминальных исходов на шлюзе.
- `status_family` — грубое семейство HTTP-статусов, например `2xx`, `4xx`,
  `5xx`, либо `unknown`, когда статус ответа недоступен.
- `result` — `success`, `error` или `cancelled`.
- Метки: `route`, `method`, `stream`, `status_family`, `result`.

`gateway_request_e2e_seconds`

- Сквозная (end-to-end) задержка шлюза: от получения запроса до момента, когда
  готов финальный непотоковый ответ, потоковый ответ полностью проитерирован,
  либо запрос завершился ошибкой/отменой.
- Для потоковых запросов измеряется полная длительность потока, а не TTFT.
- Метки: `route`, `method`, `stream`, `model`, `status_family`, `result`.

`gateway_request_ttft_seconds`

- Время от начала обработки запроса шлюзом до первого непустого потокового
  чанка от бэкенда.
- Записывается для потоковых ответов, когда обнаружен первый непустой чанк.
- Метки: `route`, `method`, `stream`, `model`, `status_family`, `result`.

`gateway_session_requests_total`

- Счётчик запросов `/v1/chat/completions` с классификацией по сессии.
- `session_present=false` означает, что в запросе не было `X-Session-ID`.
- `session_first_request=true` означает, что рантайм-трекер сессий не видел
  этот идентификатор сессии в Valkey DB 0 до данного запроса.
- Метки: `route`, `method`, `stream`, `session_present`,
  `session_first_request`.

`gateway_session_request_e2e_seconds`

- Сквозная задержка запросов `/v1/chat/completions` с классификацией по сессии.
- Записывается для обычных запросов chat completion и для первых запросов в
  сессии. Используйте `session_present=true,session_first_request=true`, чтобы
  выделить поведение при инициализации сессии. Используйте
  `session_present=true,session_first_request=false` для повторных запросов в
  уже известных сессиях.
- Метки: `route`, `method`, `stream`, `model`, `session_present`,
  `session_first_request`, `status_family`, `result`.

`gateway_session_request_ttft_seconds`

- TTFT для потоковых запросов `/v1/chat/completions` с классификацией по сессии.
- Записывается, когда обнаружен непустой потоковый чанк от бэкенда. Используйте
  `session_present=true,session_first_request=true`, чтобы выделить TTFT при
  инициализации сессии. Используйте
  `session_present=true,session_first_request=false` для повторных запросов в
  уже известных сессиях.
- Метки: `route`, `method`, `stream`, `model`, `session_present`,
  `session_first_request`, `status_family`, `result`.

`gateway_active_sessions`

- Gauge с текущим числом рантайм-ключей сессий в Valkey DB 0.
- Значение обновляется, когда Prometheus опрашивает `GET /gateway/metrics`.
- Следует рантайм-TTL сессии, а не TTL сохранённой истории чатов в Valkey DB 1.
- Метки: отсутствуют.

`gateway_session_tracker_errors_total`

- Сбои Valkey/Redis при проверке, создании или обновлении рантайм-ключей сессий
  в DB 0.
- Эти ошибки влияют на классификацию первого запроса и обновление TTL сессии, но
  сами по себе не означают, что генерация на бэкенде провалилась.
- Метки: `operation`, `error_type`.

`gateway_loki_push_total`

- Количество попыток пакетной отправки, сделанных Loki-публишером шлюза.
- Успешная отправка означает, что Loki принял батч. Она не означает, что
  конкретный запрос породил событие лога.
- Метки: `status`.

`gateway_loki_events_dropped_total`

- Количество событий логов, отброшенных до того, как они дошли до Loki.
- Типичная причина: давление на локальную очередь.
- Метки: `reason`.

Шлюз намеренно не использует `session_id`, `request_id`, `trace_id` и `span_id`
в качестве Prometheus-меток.

## Метрики бэкенда

Метрики бэкенда специфичны для бэкенда и собираются напрямую:

- вариант vLLM: `vllm:8000/metrics` через
  `deploy/llm/configs/prometheus-vllm.yaml`;
- вариант SGLang: `sglang:30000/metrics` через
  `deploy/llm/configs/prometheus-sglang.yaml`.

Стек шлюза использует собственный Prometheus с конфигурацией
`deploy/gateway/configs/prometheus-gateway.yaml` и собирает только
`/gateway/metrics`. Это делает метрики шлюза независимыми от метрик
LLM-движка.
