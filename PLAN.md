# Agent Smith — план работ

> Источник: `en.subject.pdf` (v1.1). Python 3.10, менеджер пакетов `uv`.

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
