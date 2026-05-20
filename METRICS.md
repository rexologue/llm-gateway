# Metrics Cheat Sheet


```text
Prometheus
```

Основные dashboards:

- `vLLM Gateway - Gateway Metrics Only` - gateway, session, Loki delivery
- `vLLM on Calls` - vLLM engine, GPU, host, network, storage

## 1. Что Где Смотреть

| Вопрос | Dashboard | Панель / метрика |
| --- | --- | --- |
| gateway живой и принимает запросы? | Gateway Metrics Only | `Proxy RPS`, `gateway_proxy_requests_total` |
| сколько запросов с session id? | Gateway Metrics Only | `Session req/s`, `gateway_session_requests_total` |
| сколько новых session? | Gateway Metrics Only | `First session req/s`, `session_first_request="true"` |
| клиенты забывают `X-Session-ID`? | Gateway Metrics Only | `Missing Session-ID %`, `gateway_session_id_missing_total` |
| session init медленный? | Gateway Metrics Only | `Init TTFT p95`, `Init E2E p95` |
| Valkey ломает session tracking? | Gateway Metrics Only / Explore | `gateway_session_tracker_errors_total` |
| Loki не принимает события? | Gateway Metrics Only | `Loki error %`, `gateway_proxy_loki_push_total` |
| события Loki теряются в gateway? | Gateway Metrics Only / Explore | `gateway_proxy_loki_events_dropped_total` |
| vLLM перегружен? | vLLM on Calls | `Running requests`, `Waiting requests`, `Preemptions` |
| vLLM медленно дает первый токен? | vLLM on Calls | `TTFT p95`, `vllm:time_to_first_token_seconds` |
| vLLM долго завершает запросы? | vLLM on Calls | `E2E p95`, `vllm:e2e_request_latency_seconds` |
| GPU уперлась? | vLLM on Calls | `GPU utilization`, `GPU memory used`, `GPU temperature`, `GPU power` |
| host уперся? | vLLM on Calls | `Host CPU busy %`, `Host RAM usage %`, `Disk throughput`, `TCP retrans/s` |

## 2. Переменные В Gateway Dashboard

Обычно оставить `All`, пока не нужен конкретный срез.

| Variable | Что значит | Когда менять |
| --- | --- | --- |
| `gateway_job` | Prometheus job gateway | если несколько jobs |
| `gateway_instance` | конкретный gateway instance | если несколько replicas |
| `route` | HTTP route | для session metrics чаще `/v1/chat/completions` |
| `stream` | `true` / `false` | если нужно разделить stream и non-stream |
| `model` | model label из request | если нужно сравнить модели |
| `result` | `success` / `error` / `cancelled` | для session init latency |
| `status_family` | `2xx` / `4xx` / `5xx` / `unknown` | для ошибок и деградаций |

Важно:

- `session_id`, `request_id`, `trace_id`, `span_id` не являются Prometheus labels
- конкретный `session_id` через Prometheus не ищется
- конкретный текст ответа модели через Prometheus не ищется

## 3. Gateway Traffic

### Proxy RPS

Все запросы, которые прошли через gateway.

```promql
sum(rate(gateway_proxy_requests_total{job=~"$gateway_job", instance=~"$gateway_instance", route=~"$route", stream=~"$stream"}[$__rate_interval]))
```

Интерпретация:

- `0` при ожидаемой нагрузке -> клиент не ходит в gateway или Prometheus не scrape'ит gateway
- резкий рост -> рост входящего трафика
- разделяйте `stream=true` и `stream=false`, если меняется характер запросов

### Proxy Latency

Полная latency запроса через gateway.

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(gateway_proxy_request_latency_seconds_bucket{job=~"$gateway_job", instance=~"$gateway_instance", route=~"$route", stream=~"$stream"}[$__rate_interval])
  )
)
```

Что означает:

- для `stream=false` это время до полного HTTP response
- для `stream=true` это время до полного завершения stream
- это per-request gateway E2E, не только session init

## 4. Session Metrics

Session metrics считаются только вокруг `/v1/chat/completions`.

### Session Req/s

Все session-aware запросы.

```promql
sum(rate(gateway_session_requests_total{job=~"$gateway_job", instance=~"$gateway_instance", route=~"$route", stream=~"$stream"}[$__rate_interval]))
```

### First Session Req/s

Запросы, где gateway впервые увидел `X-Session-ID`.

```promql
sum(rate(gateway_session_requests_total{job=~"$gateway_job", instance=~"$gateway_instance", route=~"$route", stream=~"$stream", session_first_request="true"}[$__rate_interval]))
```

Интерпретация:

- примерно равно числу новых звонков/session
- если сильно выше ожидаемого, session id может меняться слишком часто
- если `0`, но звонки идут, проверьте `X-Session-ID` и Valkey

### Missing Session-ID %

Доля запросов без непустого `X-Session-ID`.

```promql
100 *
sum(rate(gateway_session_id_missing_total{job=~"$gateway_job", instance=~"$gateway_instance", route=~"$route", stream=~"$stream"}[$__rate_interval]))
/
clamp_min(
  sum(rate(gateway_session_requests_total{job=~"$gateway_job", instance=~"$gateway_instance", route=~"$route", stream=~"$stream"}[$__rate_interval])),
  1e-9
)
```

Интерпретация:

- нормальное значение: `0%`
- больше `0%` -> часть клиентов не передает `X-Session-ID`
- это ломает session init аналитику для этих запросов

## 5. Session Init Latency

Session init latency пишется только один раз на `X-Session-ID`, пока ключ живет в Valkey.

### Init TTFT

TTFT первого streaming request в session.

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(gateway_session_init_ttft_seconds_bucket{job=~"$gateway_job", instance=~"$gateway_instance", route=~"$route", stream=~"$stream", model=~"$model", result=~"$result", status_family=~"$status_family"}[$__rate_interval])
  )
)
```

Что означает:

- время от приема первого request session gateway до первого non-empty chunk от vLLM
- есть только для `stream=true`
- для `stream=false` TTFT не придумывается

Если Init TTFT высокий:

- vLLM долго начинает генерацию
- prefill/queue/cache/model warm path может быть узким местом
- проверьте в `vLLM on Calls`: `Waiting requests`, `TTFT p95`, `queue p95`, `prefill p95`, GPU utilization

### Init E2E

Полная latency первого request session.

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(gateway_session_init_e2e_latency_seconds_bucket{job=~"$gateway_job", instance=~"$gateway_instance", route=~"$route", stream=~"$stream", model=~"$model", result=~"$result", status_family=~"$status_family"}[$__rate_interval])
  )
)
```

Что означает:

- время от приема первого request session gateway до полного завершения ответа
- для `stream=true` это до конца stream
- для `stream=false` это до полного response body

Если Init E2E высокий, а Init TTFT нормальный:

- первый токен приходит быстро, но весь ответ идет долго
- вероятны длинные outputs, низкий decode throughput или высокая нагрузка на генерацию
- проверьте `Generation tok/s`, `Per-token latency`, `E2E p95`

### TTFT Missing

Первый request session, где TTFT не был измерен.

```promql
sum by (reason, result) (
  increase(gateway_session_init_ttft_missing_total{job=~"$gateway_job", instance=~"$gateway_instance", route=~"$route", model=~"$model"}[$__range])
)
```

Причины:

| reason | Что значит |
| --- | --- |
| `non_stream` | первый request session был `stream=false`; это нормально |
| `no_chunk` | stream завершился без non-empty chunk |
| `cancelled_before_first_chunk` | клиент оборвал stream до первого chunk |
| `error_before_first_chunk` | ошибка до первого chunk |

## 6. Session Tracker / Valkey

### Tracker Errors

Ошибки обращения gateway к Valkey.

```promql
sum by (operation, error_type) (
  rate(gateway_session_tracker_errors_total{job=~"$gateway_job", instance=~"$gateway_instance"}[$__rate_interval])
)
```

Интерпретация:

- нормальное значение: `0`
- если растет, gateway продолжит обслуживать запросы, но session init metrics могут недосчитываться
- проверять `vllm-gateway-valkey`, network, connection limits

## 7. Loki Delivery Metrics

Это метрики не про пользовательскую latency, а про доставку событий в Loki.

### Loki Push Error %

```promql
100 *
sum(rate(gateway_proxy_loki_push_total{job=~"$gateway_job", instance=~"$gateway_instance", status="error"}[$__rate_interval]))
/
clamp_min(
  sum(rate(gateway_proxy_loki_push_total{job=~"$gateway_job", instance=~"$gateway_instance"}[$__rate_interval])),
  1e-9
)
```

Интерпретация:

- нормальное значение: `0%`
- если растет, gateway не может отправить events в Loki
- это не обязательно ломает inference, но ломает logs/debug

### Loki Dropped Events

```promql
sum by (reason) (
  rate(gateway_proxy_loki_events_dropped_total{job=~"$gateway_job", instance=~"$gateway_instance"}[$__rate_interval])
)
```

Интерпретация:

- нормальное значение: `0`
- `reason="queue_full"` -> Loki sink queue заполнена, gateway отбрасывает events, чтобы не тормозить inference
- смотреть вместе с `Loki Push Error %`

## 8. vLLM Engine Metrics

Смотреть в dashboard `vLLM on Calls`.

### Load

| Панель | Метрика | Что значит |
| --- | --- | --- |
| `Running requests` | `vllm:num_requests_running` | запросы прямо сейчас в обработке |
| `Waiting requests` | `vllm:num_requests_waiting` | очередь ожидания |
| `Completed req/s` | `vllm:request_success_total` | успешно завершенные запросы в секунду |
| `Preemptions` | `vllm:num_preemptions_total` | preemption events scheduler'а |

Если `Waiting requests` растет:

- vLLM не успевает переваривать входящий поток
- latency почти наверняка будет расти
- проверьте GPU utilization, KV cache, queue p95

### vLLM Latency

| Панель | Метрика | Что значит |
| --- | --- | --- |
| `TTFT p95` | `vllm:time_to_first_token_seconds_bucket` | время до первого токена внутри vLLM |
| `E2E p95` | `vllm:e2e_request_latency_seconds_bucket` | полная latency request внутри vLLM |
| `queue p95` | `vllm:request_queue_time_seconds_bucket` | ожидание в очереди |
| `prefill p95` | `vllm:request_prefill_time_seconds_bucket` | prefill часть |
| `decode p95` | `vllm:request_decode_time_seconds_bucket` | decode часть |
| `Per-token latency` | `vllm:request_time_per_output_token_seconds_bucket`, `vllm:inter_token_latency_seconds_bucket` | скорость генерации токенов |

Как читать:

- высокий `queue p95` -> не хватает capacity
- высокий `prefill p95` -> тяжелый prompt/context или cache miss path
- высокий `decode p95` / per-token latency -> генерация идет медленно
- gateway Init TTFT высокий + vLLM TTFT высокий -> проблема внутри vLLM path
- gateway Proxy latency высокий + vLLM E2E нормальный -> смотреть gateway/network/client side

### Token Throughput

| Панель | Метрика | Что значит |
| --- | --- | --- |
| `Prompt tok/s` | `vllm:prompt_tokens_total` | скорость входных токенов |
| `Generation tok/s` | `vllm:generation_tokens_total` | скорость выходных токенов |
| `Average request size` | `vllm:request_prompt_tokens_*`, `vllm:request_generation_tokens_*` | средний размер prompt/output |

Если latency выросла одновременно с output tokens:

- возможно, ответы стали длиннее
- проверьте `avg output tokens` и `avg requested max_tokens`

### Cache / Scheduler

| Панель | Метрика | Что значит |
| --- | --- | --- |
| `prefix cache hit %` | `vllm:prefix_cache_hits_total / vllm:prefix_cache_queries_total` | эффективность prefix cache |
| `KV cache %` | `vllm:kv_cache_usage_perc` | заполнение KV cache |
| `Preemptions` | `vllm:num_preemptions_total` | scheduler вытесняет requests |

Если KV cache высокий и preemptions растут:

- vLLM давит памятью KV cache
- возможны latency spikes

## 9. GPU / Host Metrics

Смотреть в dashboard `vLLM on Calls`.

### GPU

| Панель | Метрика | Что значит |
| --- | --- | --- |
| `GPU utilization by GPU` | `DCGM_FI_DEV_GPU_UTIL` | загрузка GPU |
| `GPU memory used by GPU` | `DCGM_FI_DEV_FB_USED`, `DCGM_FI_DEV_FB_FREE` | память GPU |
| `GPU temperature by GPU` | `DCGM_FI_DEV_GPU_TEMP` | температура |
| `GPU power by GPU` | `DCGM_FI_DEV_POWER_USAGE` | power draw |
| `XID errors` | DCGM XID metrics | GPU/driver errors |

Как читать:

- GPU util высокий, waiting растет -> не хватает GPU capacity
- GPU util низкий, waiting растет -> bottleneck может быть CPU/network/scheduler/I/O
- GPU memory почти полная -> риск preemptions/ошибок/ограничения batch size

### Host / Network / Storage

| Панель | Метрика | Что значит |
| --- | --- | --- |
| `Host CPU busy %` | `node_cpu_seconds_total` | CPU pressure |
| `Host RAM usage %` | `node_memory_*` | RAM pressure |
| `Disk throughput` | `node_disk_*_bytes_total` | disk I/O |
| `TCP retrans/s` | `node_netstat_Tcp_RetransSegs` | сетевые ретрансляции |
| `listen drops/s` | `node_netstat_TcpExt_ListenDrops` | kernel drops на listen queue |

Если TCP retrans/listen drops растут:

- возможна network/kernel-level деградация
- latency может расти без явной проблемы в vLLM

## 10. Быстрый Разбор Инцидента

### Клиенты жалуются "долго отвечает"

1. Gateway dashboard: `Proxy latency p95`.
2. Gateway dashboard: `Init TTFT p95` и `Init E2E p95`.
3. vLLM dashboard: `TTFT p95`, `E2E p95`, `Waiting requests`.
4. vLLM dashboard: `GPU utilization`, `KV cache %`, `Preemptions`.

Развилка:

- Gateway latency высокая, vLLM latency высокая -> проблема в vLLM/model/GPU path
- Gateway latency высокая, vLLM latency нормальная -> смотреть gateway/network/client/Loki delivery
- Init TTFT высокая -> первый токен медленный
- Init E2E высокая, TTFT нормальная -> долго идет генерация после первого токена

### Session init графики пустые

Проверить:

```promql
sum(rate(gateway_session_requests_total[$__rate_interval]))
```

```promql
sum(rate(gateway_session_requests_total{session_first_request="true"}[$__rate_interval]))
```

```promql
sum(rate(gateway_session_id_missing_total[$__rate_interval]))
```

```promql
sum(rate(gateway_session_tracker_errors_total[$__rate_interval]))
```

Частые причины:

- нет `X-Session-ID`
- все запросы идут с уже виденным session id
- Valkey недоступен
- первый request session был `stream=false`, поэтому TTFT histogram пустой, но E2E должен быть

### Logs пропали, inference работает

Проверить:

```promql
sum by (status) (rate(gateway_proxy_loki_push_total[$__rate_interval]))
```

```promql
sum by (reason) (rate(gateway_proxy_loki_events_dropped_total[$__rate_interval]))
```

Если `status="error"` или `reason="queue_full"` растут, проблема в доставке logs в Loki или в очереди LokiSink.

## 11. Что Не Искать В Metrics

В Prometheus специально нет:

- конкретного `session_id`
- конкретного `request_id`
- конкретного `trace_id`
- конкретного `span_id`
- текста ответа модели
- полного JSON request/response

Для конкретного запроса нужен Loki/Tempo. Metrics нужны для агрегатов, SLO, rates, p95/p99 и health signals.
