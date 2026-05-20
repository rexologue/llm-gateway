#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# serve_vllm.sh
#
# Назначение:
#   Запуск одного OpenAI-compatible vLLM endpoint с:
#   - non-thinking режимом для Qwen3.5
#   - поддержкой tool calling
#   - базовыми параметрами памяти/батчинга
###############################################################################

############################################
# REQUIRED
############################################
MODEL_NAME="/model"
# Путь к директории модели внутри контейнера.
# Меняй, если монтируешь модель в другое место.
# Обычно это либо локальная папка HF-модели, либо имя модели из HF Hub.

############################################
# LISTEN / AUTH
############################################
HOST="0.0.0.0"
# Адрес, на котором слушает API.
# 0.0.0.0 — слушать на всех интерфейсах контейнера.
# 127.0.0.1 — только локально внутри контейнера/хоста.

PORT="8000"
# Порт OpenAI-compatible API внутри контейнера.
# Меняй, если нужно развести несколько серверов или избежать конфликта.

API_KEY=""
# Если строка непустая, сервер будет требовать Bearer token.
# Оставь пустым для отключения авторизации.
# Для публичного или полупубличного доступа лучше задавать непустой ключ.

SERVED_MODEL_NAME="calls-model"
# Имя модели, которое клиент будет указывать в поле "model".
# Это просто внешний alias.
# Меняй, если хочешь красивое или более конкретное имя в API.

############################################
# PARALLELISM
############################################
TENSOR_PARALLEL_SIZE="2"
# На сколько GPU распиливается одна модель по тензорному параллелизму.
# Обычно:
# 1 — одна GPU
# 2 — модель делится на две GPU
# 4 — на четыре GPU
# Увеличивай, если одна GPU не вмещает модель или нужен multi-GPU запуск.

PIPELINE_PARALLEL_SIZE="1"
# Пайплайновый параллелизм.
# Для большинства обычных vLLM запусков оставляют 1.
# Поднимать имеет смысл только в более специфичных схемах распила модели.

############################################
# MODEL / MEMORY
############################################
MAX_MODEL_LEN="28768"
# Максимальная длина контекста, которую сервер резервирует и поддерживает.
# Чем выше значение, тем больше расход памяти, особенно под KV cache.
# Увеличивай, если нужен длинный контекст.
# Уменьшай, если сервер не стартует или слишком жрёт VRAM.

GPU_MEMORY_UTILIZATION="0.92"
# Целевая доля VRAM, которую vLLM старается занять.
# Больше значение — больше места под KV cache и выше потенциальный throughput.
# Слишком высокое значение может привести к OOM или нестабильному старту.
# Типичный диапазон: 0.88–0.95.

MAX_NUM_SEQS="20"
# Максимальное число одновременно живых последовательностей в scheduler.
# Больше — выше конкуренция/параллелизм.
# Но при длинных prompt'ах и больших ответах может резко ухудшить latency и
# увеличить давление на память.
# Уменьшай, если сервер перегружается длинными запросами.
# Увеличивай, если запросы короткие и нужна большая конкурентность.

MAX_NUM_BATCHED_TOKENS="32768"
# Потолок суммарных токенов в одном scheduler batch.
# Один из главных рычагов управления throughput/latency.
# Больше — лучше утилизация GPU, но тяжелее prefill и выше пики нагрузки.
# Меньше — стабильнее и предсказуемее latency, но ниже throughput.
# Меняй вместе с MAX_NUM_SEQS, а не изолированно.

############################################
# QWEN / REASONING / CHAT TEMPLATE
############################################
REASONING_PARSER="qwen3"
# Парсер reasoning-вывода для Qwen3/Qwen3.5.
# Нужен, чтобы сервер корректно понимал reasoning-формат модели.
# Для другой модельной семьи это значение может быть другим.

ENABLE_THINKING="false"
# Логический флаг только для читаемости конфигурации.
# Реально переключение делается через DEFAULT_CHAT_TEMPLATE_KWARGS ниже.
# false — не давать модели уходить в reasoning/thinking режим.
# true — разрешить thinking, если он нужен.

DEFAULT_CHAT_TEMPLATE_KWARGS='{"enable_thinking": false}'
# Серверный дефолт для chat_template_kwargs.
# Для Qwen3.5 это штатный способ жёстко выключить thinking.
# Если нужен thinking по умолчанию — ставь true.
# Если клиент сам передаёт chat_template_kwargs в запросе, он может переопределить это.

############################################
# TOOL CALLING
############################################
ENABLE_AUTO_TOOL_CHOICE="1"
# Включает automatic function calling на стороне vLLM.
# Без этого tool_choice="auto" не будет работать как ожидается.
# 1 — включено
# 0 — выключено

TOOL_CALL_PARSER="qwen3_coder"
# Парсер, который извлекает tool calls из сырого вывода модели.
# Для актуального recipe Qwen3.5 рекомендуется qwen3_coder.
# Для других моделей parser может отличаться.
# Меняй только на parser, который реально поддерживается твоей моделью/vLLM.

############################################
# PERFORMANCE FEATURES
############################################
ENABLE_PREFIX_CACHING="1"
# Включает prefix caching.
# Полезно, когда у многих запросов общий длинный префикс.
# Может заметно снижать цену повторных prefill.
# 1 — включено
# 0 — выключено
# Если профита нет или отлаживаешь поведение — можно отключить.

LANGUAGE_MODEL_ONLY="1"
# Отключает мультимодальные части там, где это применимо.
# Для чисто текстовых моделей/сценариев обычно разумно держать включённым.
# 1 — только LM логика
# 0 — не ограничивать
# Если работаешь с мультимодальной моделью, этот флаг может быть не нужен.

############################################
# DTYPE / EXECUTION
############################################
DTYPE="auto"
# Тип вычислений.
# auto — дать vLLM выбрать подходящий dtype.
# Иногда можно задавать явно, например half / bfloat16 / float16, если это
# поддерживается моделью и нужно жёстко контролировать запуск.
# Обычно auto — безопасный стартовый вариант.

TRUST_REMOTE_CODE="0"
# Разрешение выполнять custom code из model repository.
# 0 — безопаснее, но некоторые модели не заведутся без этого.
# 1 — включай только если модель действительно этого требует и источник доверенный.

############################################
# OBSERVABILITY / LOGGING
############################################
UVICORN_LOG_LEVEL="warning"
# Уровень логирования frontend-сервера.
# error — минимум шума
# warning — обычно оптимально
# info — больше диагностической информации
# debug — для глубокой отладки, может быть очень шумно

############################################
# EXTRA / EXPERIMENTAL
############################################
EXTRA_ARGS=()
# Сюда складываются дополнительные флаги, которые не хочется выносить
# в отдельные переменные.
# Удобно для экспериментов и редких опций.

EXTRA_ARGS+=("--disable-custom-all-reduce")
# Отключает custom all-reduce реализации.
# Иногда это помогает обойти проблемы совместимости/стабильности на multi-GPU.
# Если всё стабильно и хочется проверить максимум производительности —
# можно тестировать запуск без этого флага.

EXTRA_ARGS+=("--generation-config" "vllm")
# Использовать generation config от vLLM.
# Это делает поведение генерации более предсказуемым со стороны сервера.
# Меняй только если осознанно хочешь поведение из model repo или другой режим.

############################################
# Build argv
############################################
ARGS=()

# Core
ARGS+=("$MODEL_NAME")
ARGS+=("--host" "$HOST")
ARGS+=("--port" "$PORT")

# Auth
if [[ -n "$API_KEY" ]]; then
  ARGS+=("--api-key" "$API_KEY")
fi

if [[ -n "$SERVED_MODEL_NAME" ]]; then
  ARGS+=("--served-model-name" "$SERVED_MODEL_NAME")
fi

# Parallelism
ARGS+=("--tensor-parallel-size" "$TENSOR_PARALLEL_SIZE")
ARGS+=("--pipeline-parallel-size" "$PIPELINE_PARALLEL_SIZE")

# Model / memory
ARGS+=("--max-model-len" "$MAX_MODEL_LEN")
ARGS+=("--gpu-memory-utilization" "$GPU_MEMORY_UTILIZATION")
ARGS+=("--max-num-seqs" "$MAX_NUM_SEQS")
ARGS+=("--max-num-batched-tokens" "$MAX_NUM_BATCHED_TOKENS")

# Qwen reasoning / template behavior
ARGS+=("--reasoning-parser" "$REASONING_PARSER")
ARGS+=("--default-chat-template-kwargs" "$DEFAULT_CHAT_TEMPLATE_KWARGS")

ARGS+=("--enable-log-requests")
ARGS+=("--enable-log-outputs")
ARGS+=("--enable-request-id-headers")
ARGS+=("--disable-access-log-for-endpoints" "/health,/metrics,/ping")

export VLLM_LOGGING_LEVEL=INFO

# Tool calling
if [[ "$ENABLE_AUTO_TOOL_CHOICE" == "1" ]]; then
  ARGS+=("--enable-auto-tool-choice")
fi

if [[ -n "$TOOL_CALL_PARSER" ]]; then
  ARGS+=("--tool-call-parser" "$TOOL_CALL_PARSER")
fi

# Performance features
if [[ "$ENABLE_PREFIX_CACHING" == "1" ]]; then
  ARGS+=("--enable-prefix-caching")
fi

if [[ "$LANGUAGE_MODEL_ONLY" == "1" ]]; then
  ARGS+=("--language-model-only")
fi

# Dtype / execution
ARGS+=("--dtype" "$DTYPE")

if [[ "$TRUST_REMOTE_CODE" == "1" ]]; then
  ARGS+=("--trust-remote-code")
fi

# Logging
ARGS+=("--uvicorn-log-level" "$UVICORN_LOG_LEVEL")

# Extra
if [[ "${#EXTRA_ARGS[@]}" -gt 0 ]]; then
  ARGS+=("${EXTRA_ARGS[@]}")
fi

############################################
# Pretty print config
############################################
echo "vLLM model:              $MODEL_NAME"
echo "listen:                  $HOST:$PORT"
echo "api key:                 $([[ -n "$API_KEY" ]] && echo enabled || echo disabled)"
echo "served model name:       $([[ -n "$SERVED_MODEL_NAME" ]] && echo "$SERVED_MODEL_NAME" || echo "$MODEL_NAME")"
echo
echo "tensor parallel:         $TENSOR_PARALLEL_SIZE"
echo "pipeline parallel:       $PIPELINE_PARALLEL_SIZE"
echo
echo "max model len:           $MAX_MODEL_LEN"
echo "gpu mem utilization:     $GPU_MEMORY_UTILIZATION"
echo "max num seqs:            $MAX_NUM_SEQS"
echo "max batched tokens:      $MAX_NUM_BATCHED_TOKENS"
echo
echo "reasoning parser:        $REASONING_PARSER"
echo "enable thinking:         $ENABLE_THINKING"
echo "template kwargs:         $DEFAULT_CHAT_TEMPLATE_KWARGS"
echo
echo "auto tool choice:        $([[ "$ENABLE_AUTO_TOOL_CHOICE" == "1" ]] && echo enabled || echo disabled)"
echo "tool call parser:        $TOOL_CALL_PARSER"
echo
echo "prefix caching:          $([[ "$ENABLE_PREFIX_CACHING" == "1" ]] && echo enabled || echo disabled)"
echo "language model only:     $([[ "$LANGUAGE_MODEL_ONLY" == "1" ]] && echo enabled || echo disabled)"
echo
echo "dtype:                   $DTYPE"
echo "trust remote code:       $([[ "$TRUST_REMOTE_CODE" == "1" ]] && echo enabled || echo disabled)"
echo
echo "uvicorn log level:       $UVICORN_LOG_LEVEL"
echo
echo "full command:"
printf ' %q' vllm serve "${ARGS[@]}"
echo
echo

exec vllm serve "${ARGS[@]}"