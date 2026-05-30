#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# serve_vllm.sh
#
# Назначение:
#   Запуск одного OpenAI-compatible vLLM endpoint с:
#   - опциональным reasoning parser
#   - опциональным tool calling parser
#   - опциональными LoRA adapters
#   - опциональным speculative decoding / MTP
#   - базовыми параметрами памяти/батчинга
#
# Скрипт намеренно не привязан к конкретной модели или семейству моделей.
# Модельно-специфичные parser'ы, adapters, speculative config и trust_remote_code включай
# только после проверки документации конкретной модели и версии vLLM.
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

SERVED_MODEL_NAME="local-model"
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
# REASONING
############################################
REASONING_PARSER=""
# Парсер reasoning-вывода.
# Пусто — не добавлять --reasoning-parser.
# Нужен только для моделей, чей reasoning-формат vLLM умеет явно разбирать.
# Значение зависит от модели и версии vLLM; перед включением проверь
# актуальный список parser'ов в документации vLLM.

############################################
# TOOL CALLING
############################################
ENABLE_AUTO_TOOL_CHOICE="0"
# Включает automatic function calling на стороне vLLM.
# Без этого tool_choice="auto" может не работать как ожидается.
# Для универсального старта выключено, потому что обычно требуется parser,
# совместимый с конкретной моделью.
# 1 — включено
# 0 — выключено

TOOL_CALL_PARSER=""
# Парсер, который извлекает tool calls из сырого вывода модели.
# Пусто — не добавлять --tool-call-parser.
# Значение зависит от модели, формата tool calls и версии vLLM.
# Меняй только на parser, который реально поддерживается конкретной моделью/vLLM.

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

ENABLE_PROMPT_TOKENS_DETAILS="1"
# 1 — добавлять prompt_tokens_details в OpenAI-compatible usage.
# При включённом prefix caching это позволяет видеть cached_tokens в usage.
# 0 — не добавлять дополнительные token details.

############################################
# LORA
############################################
ENABLE_LORA="0"
# 1 — включить поддержку LoRA adapters.
# 0 — не добавлять LoRA flags.
# Для vLLM adapters обычно передаются через --lora-modules ниже.

LORA_MODULES=()
# Список LoRA adapters в формате, который понимает vLLM.
# Пример:
#   LORA_MODULES=("adapter_name=/path/to/adapter")
# Для нескольких adapters добавь несколько элементов массива.

MAX_LORAS="1"
# Максимальное число LoRA adapters в одном batch.
# Увеличивай только если реально нужны несколько adapters одновременно.

MAX_LORA_RANK="16"
# Максимальный rank LoRA.
# Должен покрывать rank подключаемых adapters.
# Чем выше rank, тем больше memory overhead.

LORA_DTYPE="auto"
# dtype LoRA adapters.
# auto — использовать dtype базовой модели.

MAX_CPU_LORAS=""
# Сколько LoRA adapters можно держать в CPU memory.
# Пусто — не добавлять флаг.

FULLY_SHARDED_LORAS="0"
# 1 — включить fully sharded LoRA вычисления.
# Может быть быстрее на больших rank/контексте/TP, но требует отдельного теста.

LORA_TARGET_MODULES=""
# Ограничить LoRA конкретными suffix'ами модулей.
# Пусто — vLLM использует все поддерживаемые LoRA modules.
# Если нужно, укажи значения через пробел: "q_proj k_proj v_proj o_proj".

############################################
# SPECULATIVE DECODING / MTP
############################################
ENABLE_SPECULATIVE_DECODING="0"
# 0 — обычный serving.
# 1 — добавить --speculative-config.
# Для MTP/draft/EAGLE это отдельный perf-эксперимент: сначала проверь
# совместимость target model, draft/assistant model и tokenizer.

SPECULATIVE_CONFIG=""
# Готовый JSON для --speculative-config.
# Если непустой, используется как есть и переменные ниже игнорируются.
# Это лучший вариант для сложных MTP/EAGLE конфигураций.

SPECULATIVE_METHOD="draft_model"
# Метод speculative decoding.
# Частые варианты: draft_model, mtp, ngram, eagle3.
# Если model не задан, vLLM может вывести метод из остальных параметров не всегда.

SPECULATIVE_MODEL="/assistant_model"
# Draft/assistant model path или HF repo id.
# Для native MTP или ngram может быть пустым.
# Для draft_model обычно должна быть совместима с tokenizer target model.

NUM_SPECULATIVE_TOKENS="4"
# Сколько draft/speculative tokens предлагать за шаг.
# Слишком большое значение может ухудшить latency при низком acceptance.

DRAFT_TENSOR_PARALLEL_SIZE=""
# Tensor parallel size для draft model.
# Пусто — не добавлять в JSON.

SPECULATIVE_MAX_MODEL_LEN=""
# Max context для draft model.
# Пусто — не добавлять в JSON.

############################################
# DTYPE / EXECUTION / QUANTIZATION
############################################
DTYPE="auto"
# Тип вычислений.
# auto — дать vLLM выбрать подходящий dtype.
# Иногда можно задавать явно, например half / bfloat16 / float16, если это
# поддерживается моделью и нужно жёстко контролировать запуск.
# Обычно auto — безопасный стартовый вариант.

QUANTIZATION=""
# Явно указать quantization backend.
# Пусто — vLLM пытается определить формат из модели/чекпойнта.
# Возможные значения зависят от версии: fp8, awq, gptq, bitsandbytes и т.д.
# Для экспериментов с конкретным backend задавай явно.

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

# Reasoning
if [[ -n "$REASONING_PARSER" ]]; then
  ARGS+=("--reasoning-parser" "$REASONING_PARSER")
fi

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

if [[ "$ENABLE_PROMPT_TOKENS_DETAILS" == "1" ]]; then
  ARGS+=("--enable-prompt-tokens-details")
fi

# LoRA
if [[ "$ENABLE_LORA" == "1" ]]; then
  ARGS+=("--enable-lora")
  ARGS+=("--max-loras" "$MAX_LORAS")
  ARGS+=("--max-lora-rank" "$MAX_LORA_RANK")
  ARGS+=("--lora-dtype" "$LORA_DTYPE")

  if [[ "${#LORA_MODULES[@]}" -gt 0 ]]; then
    ARGS+=("--lora-modules" "${LORA_MODULES[@]}")
  fi

  if [[ -n "$MAX_CPU_LORAS" ]]; then
    ARGS+=("--max-cpu-loras" "$MAX_CPU_LORAS")
  fi

  if [[ "$FULLY_SHARDED_LORAS" == "1" ]]; then
    ARGS+=("--fully-sharded-loras")
  fi

  if [[ -n "$LORA_TARGET_MODULES" ]]; then
    read -r -a _lora_target_modules <<< "$LORA_TARGET_MODULES"
    ARGS+=("--lora-target-modules" "${_lora_target_modules[@]}")
  fi
fi

# Speculative decoding
if [[ "$ENABLE_SPECULATIVE_DECODING" == "1" ]]; then
  if [[ -n "$SPECULATIVE_CONFIG" ]]; then
    ARGS+=("--speculative-config" "$SPECULATIVE_CONFIG")
  else
    _speculative_config="{"
    _speculative_delimiter=""

    if [[ -n "$SPECULATIVE_METHOD" ]]; then
      _speculative_config+="${_speculative_delimiter}\"method\":\"$SPECULATIVE_METHOD\""
      _speculative_delimiter=","
    fi

    if [[ -n "$SPECULATIVE_MODEL" ]]; then
      _speculative_config+="${_speculative_delimiter}\"model\":\"$SPECULATIVE_MODEL\""
      _speculative_delimiter=","
    fi

    if [[ -n "$NUM_SPECULATIVE_TOKENS" ]]; then
      _speculative_config+="${_speculative_delimiter}\"num_speculative_tokens\":$NUM_SPECULATIVE_TOKENS"
      _speculative_delimiter=","
    fi

    if [[ -n "$DRAFT_TENSOR_PARALLEL_SIZE" ]]; then
      _speculative_config+="${_speculative_delimiter}\"draft_tensor_parallel_size\":$DRAFT_TENSOR_PARALLEL_SIZE"
      _speculative_delimiter=","
    fi

    if [[ -n "$SPECULATIVE_MAX_MODEL_LEN" ]]; then
      _speculative_config+="${_speculative_delimiter}\"max_model_len\":$SPECULATIVE_MAX_MODEL_LEN"
    fi

    _speculative_config+="}"
    ARGS+=("--speculative-config" "$_speculative_config")
  fi
fi

# Dtype / execution
ARGS+=("--dtype" "$DTYPE")

if [[ -n "$QUANTIZATION" ]]; then
  ARGS+=("--quantization" "$QUANTIZATION")
fi

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
echo "reasoning parser:        $([[ -n "$REASONING_PARSER" ]] && echo "$REASONING_PARSER" || echo disabled)"
echo
echo "auto tool choice:        $([[ "$ENABLE_AUTO_TOOL_CHOICE" == "1" ]] && echo enabled || echo disabled)"
echo "tool call parser:        $([[ -n "$TOOL_CALL_PARSER" ]] && echo "$TOOL_CALL_PARSER" || echo disabled)"
echo
echo "prefix caching:          $([[ "$ENABLE_PREFIX_CACHING" == "1" ]] && echo enabled || echo disabled)"
echo "language model only:     $([[ "$LANGUAGE_MODEL_ONLY" == "1" ]] && echo enabled || echo disabled)"
echo "prompt token details:    $([[ "$ENABLE_PROMPT_TOKENS_DETAILS" == "1" ]] && echo enabled || echo disabled)"
echo
echo "lora:                    $([[ "$ENABLE_LORA" == "1" ]] && echo enabled || echo disabled)"
echo "lora modules:            $([[ "${#LORA_MODULES[@]}" -gt 0 ]] && printf '%s ' "${LORA_MODULES[@]}" || echo disabled)"
echo "max loras:               $MAX_LORAS"
echo "max lora rank:           $MAX_LORA_RANK"
echo
echo "speculative decoding:    $([[ "$ENABLE_SPECULATIVE_DECODING" == "1" ]] && echo enabled || echo disabled)"
echo "speculative method:      $([[ -n "$SPECULATIVE_METHOD" ]] && echo "$SPECULATIVE_METHOD" || echo auto)"
echo "speculative model:       $([[ -n "$SPECULATIVE_MODEL" ]] && echo "$SPECULATIVE_MODEL" || echo none)"
echo
echo "dtype:                   $DTYPE"
echo "quantization:            $([[ -n "$QUANTIZATION" ]] && echo "$QUANTIZATION" || echo auto/model)"
echo "trust remote code:       $([[ "$TRUST_REMOTE_CODE" == "1" ]] && echo enabled || echo disabled)"
echo
echo "uvicorn log level:       $UVICORN_LOG_LEVEL"
echo
echo "full command:"
printf ' %q' vllm serve "${ARGS[@]}"
echo
echo

exec vllm serve "${ARGS[@]}"
