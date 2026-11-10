# Agent Smith — план работ (v2)

> Источник: `en.subject.pdf` (v1.1). Python 3.10, менеджер пакетов `uv`.
>
> **v2** — план сверен с сабджектом построчно. Изменения относительно v1 помечены: 🆕 — добавлено, ✏️ — уточнено/исправлено.
> Полный список изменений — в разделе 7.

## 1. Что нужно сделать (коротко)

Собственный **code-agent фреймворк** (без smolagents / langgraph / llama-index / crewai / autogen):
LLM пишет Python-код → код исполняется в **песочнице** → вывод возвращается LLM → цикл
**Thought → Code → Observation** до вызова `final_answer(...)`.

Инструменты агента живут в **своём MCP-сервере** (отдельный процесс, stdio и streamable HTTP),
песочница подключается к нему как MCP-клиент и выставляет тулы как обычные Python-функции.

Агент применяется к двум бенчмаркам:

| Бенчмарк | Что решаем | Лимиты на задачу | Порог сдачи |
|---|---|---|---|
| **MBPP** | простые алгоритмические задачи на Python | 10 итераций, 6k input / 1.5k output токенов, 120 с | 4 из 5 |
| **SWE-bench Verified** | реальные баги в репозиториях внутри Docker | 30 итераций, 300k input / 10k output токенов, 900 с | 2 из 3 |
| **Sandbox** | security-тесты: import, builtins, network, path, timeout, memory, MCP | — | все тесты |

Токены считаются **суммарно** за задачу, reasoning-токены тоже. Ретраев на экзамене нет.

## 2. Компоненты и требования

### 2.1 Агентный цикл (orchestrator)
- Свой цикл: вызов LLM → извлечение кода → sandbox → observation → повтор; `max_iterations` конфигурируемый.
- **Извлечение кода** из разных форматов, всё приводится к Python-вызовам:
  - ```` ```python ... ``` ```` / `<end_code>` (основной);
  - XML-вызовы в стиле Anthropic `<invoke name=...><parameter>...`;
  - JSON/Hermes `<tool_call>{"name":..., "arguments":...}</tool_call>`;
  - ReAct `Action: ... / Action Input: {...}`.
- **Stop sequences** (`<end_code>`, `</tool_call>` …), чтобы модель не выдумывала observation.
- Явная обратная связь LLM в случаях: нет кода; код был кривой, но интерпретирован (как именно); таймаут и частичный вывод; вывод обрезан; правка дала синтаксическую ошибку / lint.
- System prompt: документация тулов (генерируется из MCP-схем), примеры слотов Thought/Code/Observation, пример хорошего цикла отладки.
- Учёт: токены, запросы, ретраи, latency на каждый шаг → `StepMetrics`, итог → `SolutionOutput` (модели заданы в сабжекте, включая `system_prompt`, `llm_output`, `sandbox_input`, `sandbox_output`, `retries`, `api_url`, `model_name`).

### 2.2 Песочница
- CLI: `uv run sandbox [config.json] [--mcp-stdio "python mcp_tools_mbpp.py"] [--mcp-server URL]`.
  Без задачи — REPL: читает код, исполняет в том же namespace, печатает результат/ошибку, выход по `exit` / Ctrl+D.
- `final_answer()` — встроенная функция песочницы (НЕ MCP-тул), сигнализирует циклу о завершении.
- Ограничения **только stdlib** (RestrictedPython запрещён):
  - allowlist импортов (`authorized_imports`, поддержка `math.*`);
  - allowlist директорий (`allowed_directories`, напр. `/testbed`, `/tmp/agent`);
  - запрет сети;
  - таймаут (`max_execution_time_seconds`, только для кода в песочнице, не для MCP);
  - лимит RAM (`max_memory_mb`);
  - урезанные / переопределённые builtins (`open`, `eval`, `exec`, `__import__`, `compile`, `getattr`-обходы и т.п.).
- `KeyboardInterrupt` и `SystemExit` не глотать.
- Конфиг — Pydantic `SandboxConfig` + JSON.
- Тулы обнаруживаются **динамически** с любого MCP-сервера (на защите подключат чужой), из их схем генерируется «sandbox manual» для промпта. Нужно отдавать tools, resources, prompts.
- Решение по изоляции: **отдельный процесс** (subprocess / multiprocessing) с `resource.setrlimit` для RAM/CPU, kill по таймауту, общение через pipe/JSON; вызовы MCP-тулов проксируются из дочернего процесса в родительский (у которого MCP-клиент).

### 2.3 MCP-серверы (в корне репо)
- `mcp_tools_mbpp.py` — `run_tests` (+ по желанию свои).
- `mcp_tools_swebench.py` — обязательные тулы, тестируются независимо:
  - `read_file(filepath, start_line, end_line)` → `N: line`
  - `edit_file(filepath, old_str, new_str)`
  - `list_files(directory, pattern)`
  - `search_code(pattern, file_pattern)` → `/abs/path.py:N line`
  - `search_function_or_class_definition_in_code(name)`
  - `find_references(name, filepath, line)`
  - `run_tests()` — запуск eval-скрипта
  - `get_patch()` — `git -c core.fileMode=false diff`
  - `run_command(command, workdir)` → stdout, stderr, exit code

### 2.4 LLM-провайдеры
- Абстракция провайдера (OpenAI-совместимый клиент как база: OpenRouter, Groq, Together, Gemini, Mistral…).
- **Только бесплатные тарифы.**
- **Несколько API-ключей на провайдера + ротация** при rate limit / исчерпании квоты, fallback между провайдерами, ретраи с backoff.
- Ключи только из env / `.env` (напр. `OPENROUTER_API_KEY`). Ключ в коде = провал.

### 2.5 Агенты
- `uv run python -m agent_mbpp --task-file ... --output ... --model-name ... --provider-url ...`
- `uv run python -m agent_swebench ...` — поднимает Docker-образ задачи, работает в `/testbed`, отдаёт патч через `final_answer(get_patch())`, **удаляет контейнер** после работы.
- Pydantic-модели `MBPPTaskInput`, `SWEBenchTaskInput`, `StepMetrics`, `SolutionOutput` — ровно как в сабжекте.

### 2.6 BENCHMARK_REPORT.md
- ≥ 5 моделей × те же ≥ 3 SWE-bench задачи (стартовые: `sympy__sympy-14711`, `sympy__sympy-13480`, `pydata__xarray-4629`).
- Таблица: pass/fail, итерации, input/output токены, время.
- Надёжность провайдера: среднее время ответа, ретраи, доступность.
- ≥ 2 промежуточные метрики (шаг первого чтения файла из патча, шаг уменьшения падающих тестов, итераций между «тесты прошли» и `final_answer`).
- Ablation: до/после изменения промпта/тулов на той же модели.
- Выводы. Все `solution.json` лежат в репо.

### 2.7 README.md (на английском)
Первая строка курсивом: *This project has been created as part of the 42 curriculum by &lt;login&gt;.*
Разделы: Description, Instructions, Resources (+ как использовался AI), архитектура, агентный цикл, дизайн песочницы, тулы, результаты бенчмарка.

### 2.8 Безопасность / этика (иначе 0)
Агент не должен брать решения из PR/issues/интернета, обходить песочницу, лезть за пределы контекста задачи. Логи шагов это проверяют.

## 3. Предлагаемая структура

```
agent_smith/
├── pyproject.toml            # uv, python 3.10, entrypoint `sandbox`
├── .env.example
├── sandbox_template.json
├── config/models.json        # провайдеры, модели, ключи по именам env-переменных
├── mcp_tools_mbpp.py
├── mcp_tools_swebench.py
├── src/agent_smith/
│   ├── models.py             # Pydantic: TaskInput, StepMetrics, SolutionOutput, SandboxConfig
│   ├── llm/                  # provider, key_pool (ротация), retry, usage tracking
│   ├── extraction/           # парсеры python / xml / hermes / react → python
│   ├── sandbox/              # executor (subprocess), import hook, fs guard, net block, limits, REPL, manual
│   ├── mcp_client/           # stdio + http, discovery, обёртки-функции
│   ├── agent/                # loop, prompts, budgets (iters/tokens/time)
│   └── docker/               # жизненный цикл контейнера
├── agent_mbpp/__main__.py
├── agent_swebench/__main__.py
├── tests/                    # pytest: песочница, тулы, парсеры
├── benchmarks/               # solution.json для отчёта
├── BENCHMARK_REPORT.md
└── README.md
```

## 4. Этапы и оценка времени

Оценка для одного человека, знакомого с Python, ~4–6 ч/день.

| # | Этап | Результат | Время |
|---|---|---|---|
| 0 | Изучение: MCP SDK, MBPP, SWE-bench, smolagents CodeAgent / mini-swe-agent (как референс), установка moulinette + Docker | понимание и окружение | 1–2 дня |
| 1 | Скелет проекта: uv, pyproject, Pydantic-модели, конфиги, `.env` | запускается `uv run` | 0.5 дня |
| 2 | LLM-слой: провайдеры, пул ключей, ротация, ретраи, учёт токенов/времени, stop sequences | вызов модели с метриками | 1–2 дня |
| 3 | Песочница: subprocess-исполнитель, импорт-allowlist, fs-guard, блок сети, timeout, RAM, builtins, `final_answer`, REPL CLI, тесты безопасности | проходит свои security-тесты | 3–4 дня |
| 4 | MCP: клиент (stdio + HTTP), динамическое обнаружение, прокси вызовов из песочницы, генерация manual; `mcp_tools_mbpp.py` | тулы вызываются из песочницы | 2 дня |
| 5 | Агентный цикл + экстракторы 4 форматов + обратная связь + бюджеты | общий цикл | 2 дня |
| 6 | **MBPP-агент**: промпт, укладка в 6k токенов, прогоны через moulinette | ≥ 4/5 стабильно | 2–3 дня |
| 7 | `mcp_tools_swebench.py`: 9 обязательных тулов в Docker, форматы вывода, lint после правки, очистка контейнера | тулы проходят независимые тесты | 3 дня |
| 8 | **SWE-bench-агент**: промпт с методологией, отладка на 3 стартовых задачах, тюнинг | ≥ 2/3 | 4–7 дней |
| 9 | Бенчмарк 5 моделей × 3 задачи, ablation, BENCHMARK_REPORT.md | отчёт + solution.json | 2–3 дня |
| 10 | README, рефакторинг, обработка ошибок, тренировка «живых правок» к защите | готово к сдаче | 1–2 дня |

**Итого: ~23–36 рабочих дней → реалистично 5–7 недель** (3–4 недели при полной занятости или вдвоём).
Самые рискованные этапы — 3 (песочница без внешних библиотек) и 8 (SWE-bench в бесплатных лимитах).

## 5. Порядок и чекпоинты

1. **Неделя 1:** этапы 0–2. Чекпоинт: скрипт отправляет запрос к 2 провайдерам, ротирует ключи, пишет метрики.
2. **Неделя 2:** этапы 3–4. Чекпоинт: `uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py"` работает, блокирует `import os`, `open('/etc/passwd')`, сокеты, `while True`, выделение 1 ГБ.
3. **Неделя 3:** этапы 5–6. Чекпоинт: MBPP 4/5 через `moulinette_eval`.
4. **Недели 4–5:** этапы 7–8. Чекпоинт: SWE-bench 2/3 на стартовых задачах, контейнеры чистятся.
5. **Неделя 6:** этапы 9–10. Чекпоинт: отчёт, README, все `exam_*.sh` зелёные.

## 6. Риски и решения

- **Лимиты бесплатных API** → много ключей, несколько провайдеров, fallback, кэшировать прогоны для отчёта.
- **6k input токенов на MBPP** → короткий промпт, обрезка observation, мало итераций (обычно хватает 1–3).
- **Reasoning-модели съедают бюджет** → использовать non-reasoning (Qwen3-Coder, DeepSeek-V3, Llama-3.3-70B и т.п.).
- **Обход песочницы** (`__subclasses__`, `__builtins__`, `getattr` на dunder) → изоляция процессом + rlimits + фильтр AST на dunder-атрибуты.
- **Неизвестный MCP-сервер на защите** → никаких хардкодов имён тулов, всё из `list_tools()`.
- **Живые правки на защите** → понимать каждый модуль, держать код простым.

## 7. 🆕 Изменения v2 (что не было учтено в v1)

Сверка с сабджектом выявила пробелы. Каждый пункт — с указанием раздела сабджекта и решения.

### 7.1 Песочница (V.2)
| # | Пробел в v1 | Решение в v2 |
|---|---|---|
| S1 ✏️ | «Таймаут только для кода песочницы, не для MCP» — не сказано **как** | Двухуровневый таймаут: `signal.setitimer` внутри воркера (мягкий, состояние сохраняется), **пауза таймера на время MCP-вызова**; жёсткий kill из родителя по «чистому» времени песочницы (+2 с запаса) → перезапуск воркера и явное сообщение LLM «состояние сброшено». |
| S2 🆕 | Частичный вывод при таймауте | stdout воркера стримится в родителя построчно → при таймауте LLM получает реальный частичный вывод с пометкой `partial`. |
| S3 🆕 | На macOS `RLIMIT_AS` не работает | Linux: `RLIMIT_AS` (MemoryError внутри, состояние живо). Все ОС: watchdog в родителе по RSS дочернего процесса → kill + явное сообщение. |
| S4 🆕 | Фильтр builtins/импортов обходится через `random._os`, `typing.sys` и т.п. | Многоуровневая защита: (1) AST-фильтр dunder-атрибутов и frame-атрибутов; (2) свой `__import__` с allowlist; (3) модули отдаются через read-only прокси, скрывающий `_private` и неразрешённые подмодули; (4) **`sys.addaudithook`** (stdlib, неснимаемый) — запрет `open` вне `allowed_directories`, сокетов, `subprocess`, `os.system/exec/fork/kill`, `ctypes`. |
| S5 🆕 | Утечка API-ключей через окружение | Воркер запускается с **пустым окружением** (`-I`, env без ключей), cwd = рабочая директория из allowlist. |
| S6 🆕 | Подделка протокола через fd | Протокол на приватных dup-fd; `open(<int fd>)` запрещён audit-хуком; stdin/stdout воркера перенаправлены в /dev/null. |
| S7 ✏️ | `SystemExit`/`KeyboardInterrupt` | Воркер не глотает их; родитель **ре-рейзит** их в `Sandbox.execute()` → агентный цикл корректно завершается, пишет `solution.json`. REPL: `SystemExit` = выход, Ctrl+C = отмена ввода. Воркер в отдельной сессии, Ctrl+C приходит только в родителя. |
| S8 🆕 | `final_answer(func)` вместо строки | Если передан объект функции, исходник восстанавливается из истории исполненного кода через `ast`. |
| S9 🆕 | Значение последнего выражения (REPL-семантика) | Последний `Expr` вычисляется через `eval`, его `repr` попадает в observation. |
| S10 🆕 | REPL: многострочный ввод, неинтерактивный stdin | `codeop` определяет незавершённый ввод (`...` prompt); флаг `-c CODE` для однократного запуска. |
| S11 🆕 | MCP resources и prompts «должны быть exposed» | В namespace: `list_resources()`, `read_resource(uri)`, `list_prompts()`, `get_prompt(name, **args)` — только если сервер их поддерживает; попадают в manual. |

### 7.2 MCP-серверы (V.2.5, V.5)
| # | Пробел | Решение |
|---|---|---|
| M1 🆕 | Как MCP-сервер узнаёт контекст задачи (тесты MBPP, контейнер SWE-bench) | Через env при запуске сервера: `MBPP_TASK_FILE`, `SWEBENCH_CONTAINER`, `SWEBENCH_EVAL_SCRIPT_FILE`, `TESTBED_PATH`. Без них тулы работают **автономно** (локальная ФС по `TESTBED_PATH`; `run_tests(code, tests)` с явными тестами) — это нужно для независимого тестирования тулов. |
| M2 🆕 | Код MBPP, исполняемый `run_tests`, — тоже недоверенный | `run_tests` гоняет решение **в той же песочнице** (без MCP), а не голым `subprocess`. |
| M3 🆕 | Подход к Docker: (a) песочница в контейнере или (b) на хосте | Выбран (b): песочница на хосте, тулы ходят в контейнер через `docker exec`. Контейнер с `--network none` (AI-safety: нельзя скачать PR/патч), с label `agent-smith` для гарантированной очистки. |
| M4 ✏️ | Lint после правки | `edit_file` проверяет синтаксис результата; если правка сломала синтаксис — **откат** и подробная ошибка с номером строки. |
| M5 🆕 | Поведение `edit_file` при 0 / >1 совпадениях | 0 → ошибка + ближайшие похожие строки; >1 → ошибка + номера строк всех совпадений; пустой `old_str` и нет файла → создание файла. |
| M6 🆕 | Размер вывода тулов | Лимиты (строк/совпадений/символов) с явной пометкой «truncated, используйте start_line/end_line». `run_tests` выдаёт сводку упавших тестов + хвост лога. |
| M7 🆕 | Где взять eval-скрипт, если env не задан | Fallback: `/eval.sh` в контейнере (так его кладёт harness SWE-bench), иначе понятная ошибка. |

### 7.3 Агентный цикл и лимиты (V.1, VI.1)
| # | Пробел | Решение |
|---|---|---|
| A1 🆕 | Лимиты **кумулятивные**; превышение = провал | `Budget`: перед каждым вызовом оценка входных токенов (chars/3, с запасом); `max_tokens` = min(per-call, остаток); таймаут HTTP = остаток дедлайна; не делаем вызов, который может пробить лимит. |
| A2 🆕 | 6k input на MBPP — это ~2 вызова | Компактный промпт (<1k токенов) + паттерн «одна итерация»: в одном блоке код → `run_tests` → `if passed: final_answer(code)`. |
| A3 🆕 | Рост контекста на SWE-bench (300k на 30 итераций) | Сжатие истории: старые observation (кроме последних 3) обрезаются до головы/хвоста с пометкой. |
| A4 🆕 | Что отдавать при исчерпании бюджета | MBPP — последний код, прошедший `run_tests`, иначе последний кандидат; SWE-bench — текущий `get_patch()`. `success=False`, `error` объясняет причину (честно). |
| A5 🆕 | Несколько code-блоков в ответе | Исполняются последовательно одним куском, LLM получает пометку. |
| A6 🆕 | Stop sequences — провайдеры ограничивают их 4 шт. | `["<end_code>", "\nObservation:", "</tool_call>", "</function_calls>"]`; экстрактор умеет незакрытые `<tool_call>`/```` ``` ````. |

### 7.4 LLM-слой и CLI (V.6, VI)
| # | Пробел | Решение |
|---|---|---|
| L1 🆕 | Экзамен передаёт `--env-file` скриптам | CLI агентов принимает `--env-file`, плюс автозагрузка `.env` (свой парсер, stdlib). |
| L2 🆕 | Соглашение об именах ключей | `<PROVIDER>_API_KEY`, `<PROVIDER>_API_KEY_2..9`, `<PROVIDER>_API_KEYS=k1,k2`. Провайдер определяется по хосту `--provider-url` из `config/models.json`. Поддержка keyless (Ollama/self-hosted). |
| L3 🆕 | `total_requests` включает ретраи | Каждая HTTP-попытка = запрос; `StepMetrics.retries` = неуспешные попытки шага. |
| L4 🆕 | Провайдер не вернул `usage` | Оценка токенов по символам + предупреждение в логах. |

### 7.5 Наблюдаемость и инструменты разработки
| # | Что | Зачем |
|---|---|---|
| O1 🆕 | **Event trace** (`--trace-file events.jsonl`): run_start, llm_request/response, extraction, tool_call/result, sandbox_exec, observation, budget, final_answer, docker | Отладка первых 3–5 итераций (V.1.1), промежуточные метрики для отчёта. |
| O2 🆕 | **Интерактивный UI** `ui/index.html` + `uv run agent-ui` | Живая схема архитектуры: подсветка активного компонента, таймлайн шагов, бюджет, детали (LLM output / код / observation / вызовы тулов). Источники: live (SSE из trace-файла), загрузка `events.jsonl` или `solution.json`, демо-трасса. |
| O5 🆕 | **Трасса интерпретатора**: `sys.settrace` в воркере (ставится до audit-хука, включается флагом `trace_execution`) пишет путь выполнения: строки, вызовы/возвраты, изменения переменных (безопасный repr без вызова пользовательского кода), вызовы тулов, исключения | UI показывает выполненные/пропущенные строки и переменные на каждом шаге |
| O6 🆕 | **Отчёт MCP-сервера в `_meta`**: аргументы, полученные сервером, обработчик, каждая операция в репозитории (команда, exit code, время, байты), размер сырого и возвращённого вывода, **происхождение входных данных** (откуда тесты / eval-скрипт) | Хопы «интерпретатор → обёртка → хост → JSON-RPC → сервер → репозиторий → обратно» с таймингами |
| O7 🆕 | **Входы/выходы блоков и diff**: в трассе фиксируется, что реально ушло в LLM (`delivered`, сокращённые observation), что извлечено из ответа, как вызов записан и как отправлен | Git-style diff и маркеры Δ на схеме там, где данные изменились между блоками |
| O3 🆕 | Scripted LLM provider (только для тестов) | Офлайн e2e-тесты цикла без ключей. **Никогда** не используется для данных бенчмарка. |
| O4 🆕 | `scripts/benchmark_table.py` | Генерация таблиц BENCHMARK_REPORT.md из `benchmarks/**/solution.json` — цифры только из реальных прогонов. |

### 7.6 🆕 Выводы из `moulinette/` (приложена к сабджекту)
| # | Факт из кода moulinette | Что изменено |
|---|---|---|
| T1 | `dump mbpp` отдаёт агенту `test_list[1:]` — **первый тест скрыт** и проверяется при валидации | Промпт MBPP требует общего решения без подгонки под тесты; агент видит только публичные тесты. |
| T2 | Валидатор **не смотрит на `success`** — только на решение и метрики | Fallback-кандидат при исчерпании бюджета имеет реальный шанс засчитаться; `success` остаётся честным. |
| T3 | Валидация SWE-bench: **новый** контейнер, `git apply` / `patch --fuzz` патча, `bash /eval.sh`, парсинг между `>>>>> Start/End Test Output` | `run_tests` повторяет этот сценарий и вырезает тот же диапазон вывода; `get_patch` = только отслеживаемые файлы. |
| T4 | Образы только `x86_64` | `--platform linux/amd64` для образов `*x86_64*` (Apple Silicon — эмуляция, медленно). |
| T5 | Решение MBPP исполняется в `python:3.11-slim` без ограничений | Наш `run_tests` строже (allowlist); проверено, что **все 425 эталонных решений** проходят через нашу песочницу (`tests/test_mbpp_references.py`). |
| T6 | `display` ругается на пустой `llm_output`, `sandbox_output` без `sandbox_input`, одинаковый код в соседних шагах, разные `model_name` | Шаги без кода пишут пустой `sandbox_output`; агент получает предупреждение о повторе кода; fallback-провайдер выключен по умолчанию (`--fallback`). |
| T7 | README moulinette указывает MBPP 4k/1k/60 с, но код — 6k/1.5k/120 с (как в сабджекте) | Ориентируемся на код (`MetricsLimits`), лимиты в `models.py`. |
| T8 | Имена tasks: SEED_POOL = django-11066, xarray-4629, sklearn-13439, sympy-13480, sympy-18189; EXAM_POOL + sympy-14711 | Бенчмарк-скрипт по умолчанию гоняет 3 стартовые задачи сабджекта. |

### 7.7 🆕 Решения по структуре (фактическая реализация)
- `agent_mbpp` / `agent_swebench` лежат в `src/` (ставятся вместе с пакетом, `python -m` работает из любой директории).
- Логика тулов в `src/agent_smith/tools/`, MCP-файлы в корне — тонкие обёртки (тулы тестируются без MCP и без цикла).
- `mcp` SDK зафиксирован на 1.x (2.x — другой API), `pytest` < 8.5 (9.x падает с INTERNALERROR на этой версии).
- `moulinette/` не коммитится (в `.gitignore`): это материал школы, на экзамене лежит рядом со `student/`.

### 7.8 Статус выполнения
- [x] Этапы 1–5, 7: каркас, LLM-слой, песочница, MCP, цикл, экстракторы, SWE-bench тулы.
- [x] MBPP/SWE-bench агенты (CLI), Docker-жизненный цикл, гарантированная запись `solution.json`.
- [x] UI-визуализатор + trace + live-сервер `agent-ui`.
- [x] Тесты: 93 офлайн-теста (вкл. выборку 25 эталонов MBPP) (безопасность песочницы, экстракция, LLM-ротация/fallback, тулы на git-репо, MCP stdio+HTTP+«чужой» сервер, e2e агентов); полный прогон 425 эталонов MBPP — `MBPP_ALL=1`.
- [x] `solution.json` из офлайн-прогона проходит `moulinette_eval validate_metrics` и `display` без предупреждений.
- [ ] Прогоны на реальных моделях (нужны API-ключи в `.env`) → этап 6 (MBPP ≥ 4/5).
- [ ] Docker-образы SWE-bench (Docker Desktop не был запущен) → этап 8 (≥ 2/3).
- [ ] BENCHMARK_REPORT.md с реальными цифрами (`scripts/run_benchmark.sh` + `scripts/benchmark_table.py` готовы) → этап 9.
- [ ] Логин(ы) в первой строке README (сейчас `johnytaker` — проверить, что это логин 42).
- [ ] Прочитать и уметь объяснить каждый модуль: на защите будут «живые правки» (VI.4).

### 7.9 Продолжение работы (пауза 2026-11-10)
Сделано на момент паузы: генеральная репетиция на бесплатных моделях пройдена (песочница 19/19, MBPP 5/5 на Groq
`qwen/qwen3.8-27b` и на Cohere `north-mini-code-1.0`, SWE-bench 3/3 на NVIDIA `nemotron-3-ultra`).
Бенчмарк SWE-bench `benchmarks/swebench/main` остановлен на 9 из 15 прогонов; скрипт продолжаемый.

Чтобы продолжить:
1. Запустить Docker Desktop; убрать возможные остатки: `docker ps -aq --filter label=agent-smith | xargs docker rm -f`
2. Досчитать бенчмарк (готовые пары пропускаются):
   `MODELS="https://integrate.api.nvidia.com/v1|nvidia/nemotron-3-ultra-550b-a55b https://integrate.api.nvidia.com/v1|nvidia/nemotron-3.5-lightning-30b-a3b https://integrate.api.nvidia.com/v1|mistralai/mistral-large-4-1t-a52b https://generativelanguage.googleapis.com/v1beta/openai|gemini-3.5-flash-lite https://generativelanguage.googleapis.com/v1beta/openai|gemini-3-flash-preview" ./scripts/run_benchmark.sh main`
3. Ablation (та же модель и задачи, минимальный промпт):
   `MODELS="https://integrate.api.nvidia.com/v1|nvidia/nemotron-3-ultra-550b-a55b" AGENT_ARGS="--prompt-variant minimal" ./scripts/run_benchmark.sh ablation-minimal-prompt`
4. Таблицы для отчёта: `uv run python scripts/benchmark_table.py benchmarks/swebench/main` → BENCHMARK_REPORT.md.
