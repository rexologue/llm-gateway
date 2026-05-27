#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# serve_sglang.sh
#
# Назначение:
#   Запуск одного OpenAI-compatible SGLang endpoint с:
#   - multi-GPU tensor parallel serving
#   - reasoning parser для Qwen/Qwen3.x
#   - tool calling parser
#   - Prometheus metrics
#   - request logging без gateway/Loki
#
# Важное отличие от serve_vllm.sh:
#   - У SGLang Radix cache / prefix cache включён по умолчанию.
#     Его отключают флагом --disable-radix-cache.
#   - Для Qwen thinking/non-thinking режима SGLang обычно ожидает
#     chat_template_kwargs на уровне запроса:
#       {"chat_template_kwargs": {"enable_thinking": false}}
#     Поэтому этот launch script не может на 100% заменить vLLM-флаг
#     --default-chat-template-kwargs. Лучше принудительно добавлять это
#     в gateway или клиенте.
###############################################################################

############################################
# REQUIRED
############################################
MODEL_PATH="/model"
# Путь к директории модели внутри контейнера.
# Обычно это смонтированная локальная папка Hugging Face модели.
# Можно заменить на HF repo id, если контейнеру доступен интернет/HF_TOKEN.

TOKENIZER_PATH=""
# Отдельный путь к tokenizer.
# Оставь пустым, если tokenizer лежит рядом с моделью.
# Выноси отдельно только если weights и tokenizer физически разделены.

############################################
# LISTEN / AUTH
############################################
HOST="0.0.0.0"
# Адрес, на котором слушает HTTP API.
# 0.0.0.0 — слушать на всех интерфейсах контейнера.
# 127.0.0.1 — только внутри контейнера.

PORT="30000"
# Порт SGLang OpenAI-compatible API внутри контейнера.
# У SGLang дефолт обычно 30000, в отличие от твоего vLLM 8000.

API_KEY=""
# Если строка непустая, сервер будет требовать Bearer token.
# Для локального isolated стенда можно оставить пустым.
# Для любого внешнего доступа лучше задавать ключ.

SERVED_MODEL_NAME="calls-model"
# Внешний alias модели для OpenAI-compatible API.
# Клиент будет передавать это значение в поле "model".
# Если пусто, SGLang вернёт имя/путь модели.

############################################
# PARALLELISM
############################################
TP_SIZE="1"
# Tensor parallel size.
# Прямой аналог vLLM TENSOR_PARALLEL_SIZE.
# 1 — одна GPU.
# 2 — модель делится на две GPU.
# 4+ — только если реально есть GPU и модель/интерконнект это оправдывают.
# Для 2x4090 обычно стартовая точка — 2.

PP_SIZE="1"
# Pipeline parallel size.
# Обычно оставляют 1.
# Повышать имеет смысл для очень больших моделей/длинного контекста,
# когда tensor parallel уже недостаточен или неудобен.

DP_SIZE=""
# Data parallel size.
# Пусто — не включать.
# DP полезен для throughput, если модель помещается в каждую DP-группу.
# Для твоего сценария 2x4090 + одна большая модель чаще нужен TP=2, не DP=2.

############################################
# MODEL / MEMORY
############################################
CONTEXT_LENGTH="28768"
# Максимальная длина контекста, которую сервер будет поддерживать.
# Ближайший аналог vLLM MAX_MODEL_LEN.
# Чем выше, тем больше давление на KV/cache memory pool.
# Для call-center сценария держи чуть выше реального worst-case:
# system prompt + history + RAG + max output.
# Если сервер не стартует или ловит OOM — снижать одним из первых.

MEM_FRACTION_STATIC="0.88"
# Доля GPU-памяти под static allocation: веса модели + KV/cache memory pool.
# Ближайший аналог vLLM GPU_MEMORY_UTILIZATION, но семантически не 1-в-1.
# Больше — больше места под KV/cache и потенциально выше concurrency.
# Слишком высоко — OOM, проблемы старта, меньше пространства под временные буферы.
# Стартовый диапазон для 4090: 0.82–0.90.
# Для длинных prompt'ов и нестабильного старта снижай.

MAX_RUNNING_REQUESTS="20"
# Максимальное число одновременно исполняемых запросов.
# Ближайший аналог vLLM MAX_NUM_SEQS.
# Для твоего SLA по ~20 одновременным звонкам стартовое значение — 20.
# Если p90/p99 TTFT плывёт или начинаются retractions/OOM — снижать.
# Если запросы короткие, cache hit высокий и GPU недогружена — повышать.

MAX_TOTAL_TOKENS=""
# Жёсткий потолок token memory pool.
# Обычно лучше оставить пустым и дать SGLang рассчитать по MEM_FRACTION_STATIC.
# Имеет смысл задавать для повторяемых экспериментов или диагностики памяти.
# Не путать с MAX_NUM_BATCHED_TOKENS в vLLM: это не тот же самый рычаг.

MAX_PREFILL_TOKENS="32768"
# Максимум prefill-токенов в одном prefill batch.
# Ближайший практический аналог vLLM MAX_NUM_BATCHED_TOKENS для prefill-фазы.
# Больше — лучше throughput на длинных prompt'ах, но выше пик нагрузки и TTFT.
# Меньше — стабильнее latency, ниже риск OOM во время prefill.
# Для 6k+ system prompt и concurrency около 20 стартуй с 32768 и сравни 16384.

CHUNKED_PREFILL_SIZE="4096"
# Максимальный размер одного chunk при chunked prefill.
# Если поставить -1, chunked prefill отключается.
# Меньше — ниже пики памяти и ровнее latency, но больше overhead.
# Больше — агрессивнее prefill, может ускорять throughput, но давить p90/p99.
# При OOM на длинных prompt'ах уменьшай до 2048/4096.

PREFILL_MAX_REQUESTS=""
# Максимум запросов в одном prefill batch.
# Пусто — не ограничивать.
# Можно задать 1..N, если надо прижать конкуренцию именно на prefill.
# Для телефонного workload иногда полезно ограничить, если первый turn душит всех.

SCHEDULE_POLICY="lpm"
# Политика scheduler.
# fcfs — проще и предсказуемее: кто раньше пришёл, тот раньше обрабатывается.
# lpm — longest prefix match; потенциально полезно при общих длинных префиксах,
# потому что лучше совпадает с идеей Radix cache/prefix reuse.
# Для твоего кейса обязательно сравнить lpm vs fcfs на одинаковом бенче.

SCHEDULE_CONSERVATIVENESS="1.0"
# Насколько осторожно scheduler набирает batch.
# Больше — консервативнее, меньше риск request retraction/перегруза.
# Если видишь нестабильный tail latency или частые retractions — подними.
# Если всё стабильно и GPU недогружена — можно пробовать снижать/оставлять 1.0.

RADIX_EVICTION_POLICY="lru"
# Политика вытеснения Radix cache.
# lru — вытеснять давно неиспользованные ветки.
# lfu — вытеснять редко используемые ветки.
# Для звонков с одинаковым system prompt обычно lru — нормальный старт.
# lfu стоит тестировать, если есть несколько постоянных сценариев с разной частотой.

DISABLE_RADIX_CACHE="0"
# 0 — оставить Radix cache включённым.
# 1 — добавить --disable-radix-cache.
# Отключать стоит только для контрольного бенча или диагностики.
# В твоём workload с большим общим system prompt обычно cache должен быть включён.

ENABLE_CACHE_REPORT="1"
# 1 — возвращать cached tokens в usage.prompt_tokens_details.
# Полезно для проверки, реально ли работает prefix/Radix cache.
# Для production можно оставить включённым, если клиент/gateway не ломается от details.

############################################
# QWEN / REASONING / THINKING
############################################
REASONING_PARSER="qwen3"
# Parser reasoning-секции для Qwen3/Qwen3.5.
# Нужен, чтобы отделять reasoning_content от обычного content.
# Для Qwen3-Thinking моделей может потребоваться qwen3-thinking.

############################################
# TOOL CALLING / STRUCTURED OUTPUTS
############################################
TOOL_CALL_PARSER="qwen3_coder"
# Parser tool calls.
# Для Qwen3.5 cookbook SGLang указывает qwen3_coder.
# Если конкретная модель/версия SGLang ругается, проверь parser qwen.
# Для других семейств нужны другие parser'ы: llama3, mistral, deepseekv3 и т.д.

GRAMMAR_BACKEND="xgrammar"
# Backend constrained decoding / tool_choice / structured output.
# xgrammar — дефолтный и основной вариант для tool_choice в SGLang.
# Менять стоит только если упираешься в конкретный баг backend'а.

############################################
# QUANTIZATION / DTYPE
############################################
DTYPE="auto"
# Тип вычислений.
# auto — безопасный старт, SGLang выбирает по модели.
# half/float16 — часто нужен для AWQ.
# bfloat16 — нормальный вариант для BF16 чекпойнтов, если железо поддерживает.

QUANTIZATION="fp8"
# Явно указать quantization backend.
# Пусто — SGLang пытается определить формат из модели/чекпойнта.
# Возможные значения зависят от версии: fp8, awq, gptq, modelopt_fp4 и т.д.
# Для готового FP8 checkpoint часто можно оставить пустым.
# Для экспериментов с конкретным backend задавай явно.

KV_CACHE_DTYPE="auto"
# dtype KV cache.
# auto — обычно лучший старт.
# fp8_e4m3/fp8_e5m2 могут экономить память, но способны ухудшить качество.
# Для reasoning-heavy и extraction-heavy задач сначала тестируй auto.

TRUST_REMOTE_CODE="0"
# Разрешить выполнение custom code из model repo.
# 0 — безопаснее.
# 1 — включать только если модель без этого не стартует и источник доверенный.

LOAD_FORMAT="auto"
# Формат загрузки весов.
# auto — сначала safetensors, потом fallback.
# Обычно не трогать.
# Имеет смысл менять для gguf/bitsandbytes/нестандартных форматов.

MODEL_LOADER_EXTRA_CONFIG=""
# JSON с дополнительными настройками загрузчика.
# Для очень больших моделей иногда ускоряют загрузку multithread load.
# Пример:
#   {"enable_multithread_load": "true", "num_threads": 64}
# Для обычного локального FP8 на 2x4090 можно оставить пустым.

############################################
# SPECULATIVE DECODING / LATENCY
############################################
ENABLE_SPECULATIVE="0"
# 0 — не включать speculative decoding.
# 1 — добавить параметры ниже.
# Для интерактивного TTS-кейса speculative decoding может снизить latency,
# но это отдельный эксперимент: сложнее отладка, выше риск несовместимости.

SPECULATIVE_ALGO="NEXTN"
SPECULATIVE_NUM_STEPS="3"
SPECULATIVE_EAGLE_TOPK="1"
SPECULATIVE_NUM_DRAFT_TOKENS="4"
# Эти значения близки к Qwen3.5 cookbook-примеру.
# Не включай вслепую в baseline. Сначала стабилизируй обычный serving.

############################################
# OBSERVABILITY / LOGGING
############################################
ENABLE_METRICS="1"
# 1 — включить Prometheus /metrics.
# Для твоего сценария обязательно включать: TTFT, latency, cache hit,
# token usage, throughput и другие метрики нужны для сравнения с vLLM.

ENABLE_MFU_METRICS="1"
# 1 — включить estimated MFU-related metrics.
# Может быть полезно для глубокого perf-analysis, но для первого запуска не нужно.

COLLECT_TOKENS_HISTOGRAM="1"
# 1 — собирать histogram prompt/generation tokens.
# Полезно для call-center профилей: видно реальное распределение длины входов/выходов.

BUCKET_TIME_TO_FIRST_TOKEN=""
# Кастомные buckets для TTFT histogram.
# Пусто — дефолт SGLang.
# Имеет смысл задать, если хочешь buckets вокруг TTS-relevant latency:
# например 0.1 0.2 0.3 0.5 0.75 1 1.5 2 3 5 10 20.

BUCKET_INTER_TOKEN_LATENCY=""
# Кастомные buckets для inter-token latency.
# Пусто — дефолт SGLang.
# Для TTS важен не только TTFT, но и стабильность потока токенов.

BUCKET_E2E_REQUEST_LATENCY=""
# Кастомные buckets для end-to-end latency.
# Пусто — дефолт SGLang.

LOG_LEVEL="info"
# Общий уровень логирования SGLang.
# warning — меньше шума.
# info — нормальная диагностика.
# debug — только для отладки.

LOG_LEVEL_HTTP="warning"
# Уровень HTTP-сервера.
# warning обычно достаточно, чтобы не заливать stdout.

LOG_REQUESTS="1"
# 1 — логировать request metadata/input/output в stdout.
# По умолчанию SGLang request contents не логирует.
# Для production с персональными данными лучше аккуратно:
#   LOG_REQUESTS=1 + LOG_REQUESTS_LEVEL=0/1
#   или выключить и логировать через gateway с redaction.

LOG_REQUESTS_LEVEL="1"
# 0 — metadata без sampling params.
# 1 — metadata + sampling params.
# 2 — metadata + sampling params + partial input/output.
# 3 — полный input/output.
# Для звонков/телефонов лучше не ставить 3 без redaction.

LOG_REQUESTS_FORMAT="json"
# text — человекочитаемо.
# json — удобнее парсить и грузить в Loki/ELK/ClickHouse.
# Даже без Loki json-stdout удобнее для будущего pipeline.

UVICORN_ACCESS_LOG_EXCLUDE_PREFIXES="/health /metrics /v1/models"
# Убрать шумные access logs по health/metrics/models.
# Значения добавляются как отдельные argv после флага.

ENABLE_REQUEST_TIME_STATS_LOGGING="1"
# 1 — логировать per-request time stats.
# Полезно для диагностики TTFT/e2e вне Prometheus.

CRASH_DUMP_FOLDER="/tmp/sglang_crash_dump"
# Папка для crash dump последних запросов перед падением.
# Очень полезно для поиска конкретного request/prompt, который валит runtime.
# Поставь пустую строку, если не хочешь хранить такие дампы.

############################################
# RUNTIME / STABILITY
############################################
WATCHDOG_TIMEOUT="300"
# Если forward batch выполняется дольше этого времени, сервер падает,
# чтобы не висеть бесконечно.
# Для нормального interactive serving 300 секунд — достаточно большой потолок.
# Если длинные запросы легитимны, увеличивай.

SOFT_WATCHDOG_TIMEOUT=""
# Мягкий watchdog: дампит debug-информацию, но не обязательно убивает процесс.
# Полезно при расследовании редких зависаний.

DISABLE_CUDA_GRAPH="0"
# 0 — не добавлять --disable-cuda-graph.
# 1 — отключить CUDA graphs.
# Если видишь deadlock/странные CUDA errors, можно попробовать 1.
# Для максимальной производительности лучше сначала оставить 0.

DISABLE_CUSTOM_ALL_REDUCE="1"
# 1 — добавить --disable-custom-all-reduce.
# Как и во vLLM, иногда помогает на consumer multi-GPU и нестабильных связках.
# Если всё стабильно, отдельно тестируй 0 для производительности.

ENABLE_P2P_CHECK="1"
# 1 — добавить --enable-p2p-check.
# Полезно на multi-GPU, если есть подозрение на peer access проблемы.
# Особенно актуально для consumer GPU/PCIe-топологий.

SLEEP_ON_IDLE="0"
# 1 — снизить CPU usage, когда сервер простаивает.
# Для benchmark лучше 0, чтобы не добавлять лишние переменные.

############################################
# EXTRA / EXPERIMENTAL
############################################
EXTRA_ARGS=()
# Сюда складываются дополнительные флаги, которые не хочется выносить
# в отдельные переменные.
# Удобно для быстрых экспериментов и редких SGLang опций.

############################################
# Build argv
############################################
ARGS=()

# Core
ARGS+=("--model-path" "$MODEL_PATH")
ARGS+=("--host" "$HOST")
ARGS+=("--port" "$PORT")

if [[ -n "$TOKENIZER_PATH" ]]; then
  ARGS+=("--tokenizer-path" "$TOKENIZER_PATH")
fi

# Auth / OpenAI API
if [[ -n "$API_KEY" ]]; then
  ARGS+=("--api-key" "$API_KEY")
fi

if [[ -n "$SERVED_MODEL_NAME" ]]; then
  ARGS+=("--served-model-name" "$SERVED_MODEL_NAME")
fi

# Parallelism
ARGS+=("--tp-size" "$TP_SIZE")
ARGS+=("--pp-size" "$PP_SIZE")

if [[ -n "$DP_SIZE" ]]; then
  ARGS+=("--dp-size" "$DP_SIZE")
fi

# Model / memory / scheduling
ARGS+=("--context-length" "$CONTEXT_LENGTH")
ARGS+=("--mem-fraction-static" "$MEM_FRACTION_STATIC")
ARGS+=("--max-running-requests" "$MAX_RUNNING_REQUESTS")
ARGS+=("--max-prefill-tokens" "$MAX_PREFILL_TOKENS")
ARGS+=("--chunked-prefill-size" "$CHUNKED_PREFILL_SIZE")
ARGS+=("--schedule-policy" "$SCHEDULE_POLICY")
ARGS+=("--schedule-conservativeness" "$SCHEDULE_CONSERVATIVENESS")
ARGS+=("--radix-eviction-policy" "$RADIX_EVICTION_POLICY")

if [[ -n "$MAX_TOTAL_TOKENS" ]]; then
  ARGS+=("--max-total-tokens" "$MAX_TOTAL_TOKENS")
fi

if [[ -n "$PREFILL_MAX_REQUESTS" ]]; then
  ARGS+=("--prefill-max-requests" "$PREFILL_MAX_REQUESTS")
fi

if [[ "$DISABLE_RADIX_CACHE" == "1" ]]; then
  ARGS+=("--disable-radix-cache")
fi

if [[ "$ENABLE_CACHE_REPORT" == "1" ]]; then
  ARGS+=("--enable-cache-report")
fi

# Qwen reasoning / tool calling
if [[ -n "$REASONING_PARSER" ]]; then
  ARGS+=("--reasoning-parser" "$REASONING_PARSER")
fi

if [[ -n "$TOOL_CALL_PARSER" ]]; then
  ARGS+=("--tool-call-parser" "$TOOL_CALL_PARSER")
fi

if [[ -n "$GRAMMAR_BACKEND" ]]; then
  ARGS+=("--grammar-backend" "$GRAMMAR_BACKEND")
fi

# Quantization / dtype
ARGS+=("--dtype" "$DTYPE")
ARGS+=("--kv-cache-dtype" "$KV_CACHE_DTYPE")
ARGS+=("--load-format" "$LOAD_FORMAT")

if [[ -n "$QUANTIZATION" ]]; then
  ARGS+=("--quantization" "$QUANTIZATION")
fi

if [[ "$TRUST_REMOTE_CODE" == "1" ]]; then
  ARGS+=("--trust-remote-code")
fi

if [[ -n "$MODEL_LOADER_EXTRA_CONFIG" ]]; then
  ARGS+=("--model-loader-extra-config" "$MODEL_LOADER_EXTRA_CONFIG")
fi

# Speculative decoding
if [[ "$ENABLE_SPECULATIVE" == "1" ]]; then
  ARGS+=("--speculative-algo" "$SPECULATIVE_ALGO")
  ARGS+=("--speculative-num-steps" "$SPECULATIVE_NUM_STEPS")
  ARGS+=("--speculative-eagle-topk" "$SPECULATIVE_EAGLE_TOPK")
  ARGS+=("--speculative-num-draft-tokens" "$SPECULATIVE_NUM_DRAFT_TOKENS")
fi

# Observability / metrics
if [[ "$ENABLE_METRICS" == "1" ]]; then
  ARGS+=("--enable-metrics")
fi

if [[ "$ENABLE_MFU_METRICS" == "1" ]]; then
  ARGS+=("--enable-mfu-metrics")
fi

if [[ "$COLLECT_TOKENS_HISTOGRAM" == "1" ]]; then
  ARGS+=("--collect-tokens-histogram")
fi

if [[ -n "$BUCKET_TIME_TO_FIRST_TOKEN" ]]; then
  read -r -a _ttft_buckets <<< "$BUCKET_TIME_TO_FIRST_TOKEN"
  ARGS+=("--bucket-time-to-first-token" "${_ttft_buckets[@]}")
fi

if [[ -n "$BUCKET_INTER_TOKEN_LATENCY" ]]; then
  read -r -a _itl_buckets <<< "$BUCKET_INTER_TOKEN_LATENCY"
  ARGS+=("--bucket-inter-token-latency" "${_itl_buckets[@]}")
fi

if [[ -n "$BUCKET_E2E_REQUEST_LATENCY" ]]; then
  read -r -a _e2e_buckets <<< "$BUCKET_E2E_REQUEST_LATENCY"
  ARGS+=("--bucket-e2e-request-latency" "${_e2e_buckets[@]}")
fi

ARGS+=("--log-level" "$LOG_LEVEL")
ARGS+=("--log-level-http" "$LOG_LEVEL_HTTP")

if [[ "$LOG_REQUESTS" == "1" ]]; then
  ARGS+=("--log-requests")
  ARGS+=("--log-requests-level" "$LOG_REQUESTS_LEVEL")
  ARGS+=("--log-requests-format" "$LOG_REQUESTS_FORMAT")
  ARGS+=("--log-requests-target" "stdout")
fi

if [[ -n "$UVICORN_ACCESS_LOG_EXCLUDE_PREFIXES" ]]; then
  read -r -a _exclude_prefixes <<< "$UVICORN_ACCESS_LOG_EXCLUDE_PREFIXES"
  ARGS+=("--uvicorn-access-log-exclude-prefixes" "${_exclude_prefixes[@]}")
fi

if [[ "$ENABLE_REQUEST_TIME_STATS_LOGGING" == "1" ]]; then
  ARGS+=("--enable-request-time-stats-logging")
fi

if [[ -n "$CRASH_DUMP_FOLDER" ]]; then
  mkdir -p "$CRASH_DUMP_FOLDER"
  ARGS+=("--crash-dump-folder" "$CRASH_DUMP_FOLDER")
fi

# Runtime / stability
ARGS+=("--watchdog-timeout" "$WATCHDOG_TIMEOUT")

if [[ -n "$SOFT_WATCHDOG_TIMEOUT" ]]; then
  ARGS+=("--soft-watchdog-timeout" "$SOFT_WATCHDOG_TIMEOUT")
fi

if [[ "$DISABLE_CUDA_GRAPH" == "1" ]]; then
  ARGS+=("--disable-cuda-graph")
fi

if [[ "$DISABLE_CUSTOM_ALL_REDUCE" == "1" ]]; then
  ARGS+=("--disable-custom-all-reduce")
fi

if [[ "$ENABLE_P2P_CHECK" == "1" ]]; then
  ARGS+=("--enable-p2p-check")
fi

if [[ "$SLEEP_ON_IDLE" == "1" ]]; then
  ARGS+=("--sleep-on-idle")
fi

# Extra
if [[ "${#EXTRA_ARGS[@]}" -gt 0 ]]; then
  ARGS+=("${EXTRA_ARGS[@]}")
fi

############################################
# Pretty print config
############################################
echo "SGLang model:             $MODEL_PATH"
echo "listen:                   $HOST:$PORT"
echo "api key:                  $([[ -n "$API_KEY" ]] && echo enabled || echo disabled)"
echo "served model name:        $([[ -n "$SERVED_MODEL_NAME" ]] && echo "$SERVED_MODEL_NAME" || echo "$MODEL_PATH")"
echo
echo "tp:                       $TP_SIZE"
echo "pp:                       $PP_SIZE"
echo "dp:                       $([[ -n "$DP_SIZE" ]] && echo "$DP_SIZE" || echo disabled)"
echo
echo "context length:           $CONTEXT_LENGTH"
echo "mem fraction static:      $MEM_FRACTION_STATIC"
echo "max running requests:     $MAX_RUNNING_REQUESTS"
echo "max total tokens:         $([[ -n "$MAX_TOTAL_TOKENS" ]] && echo "$MAX_TOTAL_TOKENS" || echo auto)"
echo "max prefill tokens:       $MAX_PREFILL_TOKENS"
echo "chunked prefill size:     $CHUNKED_PREFILL_SIZE"
echo "prefill max requests:     $([[ -n "$PREFILL_MAX_REQUESTS" ]] && echo "$PREFILL_MAX_REQUESTS" || echo unlimited)"
echo "schedule policy:          $SCHEDULE_POLICY"
echo "schedule conservatism:    $SCHEDULE_CONSERVATIVENESS"
echo
echo "radix cache:              $([[ "$DISABLE_RADIX_CACHE" == "1" ]] && echo disabled || echo enabled)"
echo "radix eviction policy:    $RADIX_EVICTION_POLICY"
echo "cache report:             $([[ "$ENABLE_CACHE_REPORT" == "1" ]] && echo enabled || echo disabled)"
echo
echo "reasoning parser:         $([[ -n "$REASONING_PARSER" ]] && echo "$REASONING_PARSER" || echo disabled)"
echo "tool call parser:         $([[ -n "$TOOL_CALL_PARSER" ]] && echo "$TOOL_CALL_PARSER" || echo disabled)"
echo "grammar backend:          $GRAMMAR_BACKEND"
echo
echo "dtype:                    $DTYPE"
echo "quantization:             $([[ -n "$QUANTIZATION" ]] && echo "$QUANTIZATION" || echo auto/model)"
echo "kv cache dtype:           $KV_CACHE_DTYPE"
echo "trust remote code:        $([[ "$TRUST_REMOTE_CODE" == "1" ]] && echo enabled || echo disabled)"
echo
echo "metrics:                  $([[ "$ENABLE_METRICS" == "1" ]] && echo enabled || echo disabled)"
echo "request logging:          $([[ "$LOG_REQUESTS" == "1" ]] && echo enabled || echo disabled)"
echo "request log level:        $LOG_REQUESTS_LEVEL"
echo "request log format:       $LOG_REQUESTS_FORMAT"
echo "crash dump folder:        $([[ -n "$CRASH_DUMP_FOLDER" ]] && echo "$CRASH_DUMP_FOLDER" || echo disabled)"
echo
echo "disable cuda graph:       $([[ "$DISABLE_CUDA_GRAPH" == "1" ]] && echo yes || echo no)"
echo "disable custom allreduce: $([[ "$DISABLE_CUSTOM_ALL_REDUCE" == "1" ]] && echo yes || echo no)"
echo "p2p check:                $([[ "$ENABLE_P2P_CHECK" == "1" ]] && echo enabled || echo disabled)"
echo

LAUNCHER=(python3 -m sglang.launch_server)

echo "full command:"
printf ' %q' "${LAUNCHER[@]}" "${ARGS[@]}"
echo
echo

exec "${LAUNCHER[@]}" "${ARGS[@]}"
