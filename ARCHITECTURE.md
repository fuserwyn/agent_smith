# Agent Smith — архитектура

## 1. Что делает агент

Агент получает задачу (JSON от moulinette) и **сам** решает её, работая как программист:
изучает задачу → пишет код → запускает → смотрит результат → исправляет → сдаёт ответ.

Каждая итерация — один цикл **Thought → Code → Observation**:

1. **Thought** — LLM рассуждает, что делать дальше.
2. **Code** — LLM пишет Python-код, в котором вызывает тулы как обычные функции:
   ```python
   hits = search_code("def _print_Mul", "*.py")
   print(hits)
   print(read_file("/testbed/sympy/printing/latex.py", 380, 440))
   ```
3. **Observation** — код выполняется в песочнице, stdout/ошибки возвращаются LLM следующим сообщением.

Цикл заканчивается, когда код вызывает `final_answer(...)`, или когда кончился бюджет
(итерации / токены / время). Результат всегда пишется в `solution.json` (`SolutionOutput`).

### MBPP (алгоритмические задачи)
Вход: описание задачи, сигнатура функции, список `assert`-тестов.
1. Пишет функцию.
2. Проверяет её `run_tests(code)` (MCP-тул).
3. Если тесты упали — читает ошибку, правит.
4. `final_answer(code)`.

Обычно 1–3 итерации; жёсткие лимиты: 10 итераций, 6k input / 1.5k output токенов, 120 с.

### SWE-bench (реальные баги в Docker)
Вход: issue, Docker-образ, eval-скрипт, репозиторий в `/testbed`.
1. **Понять** issue, найти связанные файлы (`search_code`, `search_function_or_class_definition_in_code`, `list_files`).
2. **Прочитать** код (`read_file`), найти использования (`find_references`).
3. **Воспроизвести** баг (`run_command("python -c ...")`) или запустить `run_tests()` → baseline.
4. **Исправить** (`edit_file`) — после правки тул сам проверяет синтаксис/lint.
5. **Проверить** `run_tests()`; если не прошло — вернуться к 2–4.
6. `final_answer(get_patch())` — сразу, как только тесты прошли.

Лимиты: 30 итераций, 300k input / 10k output токенов, 900 с. Контейнер удаляется в конце **всегда**.

Запрещено: искать решение в PR/issues/интернете, обходить песочницу, выходить за контекст задачи.

---

## 2. Общая схема

```
                 task.json                                         solution.json
                     │                                                   ▲
                     ▼                                                   │
┌───────────────────────────────────────────────────────────────────────────────┐
│ CLI: agent_mbpp / agent_swebench                                              │
│   парсинг аргументов, .env, загрузка TaskInput, сборка зависимостей           │
└──────────────┬────────────────────────────────────────────────────────────────┘
               ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ AgentLoop (orchestrator)                                                      │
│                                                                               │
│   PromptBuilder ──► LLMClient ──► CodeExtractor ──► Sandbox ──► Observation   │
│        ▲            (метрики)     (4 формата→py)    (execute)        │        │
│        └──────────────────── история сообщений ◄────────────────────┘        │
│                                                                               │
│   Budget (итерации / токены / время)     MetricsRecorder → StepMetrics       │
└──────┬─────────────────────────────────────────┬──────────────────────────────┘
       │                                         │
       ▼                                         ▼
┌──────────────────────────┐        ┌─────────────────────────────────────────────┐
│ LLM layer                │        │ Sandbox (родительская часть)                │
│  ProviderRouter          │        │  MCPClient (stdio | streamable HTTP)        │
│   ├ Provider(OpenAI-     │        │  ToolRegistry  ← list_tools()/resources/    │
│   │  compatible)         │        │                  prompts                    │
│   ├ KeyPool (ротация)    │        │  ManualGenerator → текст для system prompt  │
│   └ Retry/backoff        │        │  Executor ──spawn──┐                        │
│  UsageTracker            │        └────────────────────┼────────────────────────┘
└──────────┬───────────────┘                             │ pipe (JSON-сообщения)
           ▼                                             ▼
   OpenRouter / Groq /              ┌─────────────────────────────────────────────┐
   Gemini / Mistral ...             │ Worker-процесс (недоверенный код)           │
                                    │  rlimit RAM/CPU, блок сети, import-hook,    │
                                    │  guard путей, урезанные builtins, AST-фильтр│
                                    │  namespace: final_answer + прокси MCP-тулов │
                                    └─────────────────────────────────────────────┘
                                                         │ вызов тула = сообщение
                                                         │ родителю → MCPClient
                                                         ▼
                                    ┌─────────────────────────────────────────────┐
                                    │ MCP server (отдельный процесс)              │
                                    │  mcp_tools_mbpp.py      — run_tests         │
                                    │  mcp_tools_swebench.py  — 9 тулов ──► docker│
                                    │                           exec в контейнер  │
                                    └─────────────────────────────────────────────┘
```

**Ключевые решения:**

| Решение | Почему |
|---|---|
| Недоверенный код — в **отдельном процессе** | можно убить по таймауту, `setrlimit` на RAM не трогает агента, падение не роняет цикл |
| MCP-клиент живёт в **родителе**, в worker — только прокси-функции | worker не получает сокетов/сети; MCP-вызовы вне таймаута песочницы, как требует сабжект |
| Worker **долгоживущий** на задачу | переменные сохраняются между шагами (сабжект: persistent variables) |
| Для SWE-bench песочница на **хосте**, тулы ходят в Docker через `docker exec` | проще отладка, одна реализация песочницы для обоих бенчмарков |
| Все форматы тул-вызовов → **Python** до песочницы | песочница не знает про форматы, любая модель работает |
| Тулы берутся только из `list_tools()` | на защите подключат неизвестный MCP-сервер |

---

## 3. Один шаг цикла (последовательность)

```
AgentLoop            LLMClient          Extractor          Sandbox/Worker        MCP server
   │ messages ──────────►│                   │                    │                    │
   │                     │ POST /chat (stop=["<end_code>", "</tool_call>", "Observation:"])
   │◄── text, usage ─────│                   │                    │                    │
   │ text ─────────────────────────────────►│                    │                    │
   │◄── code + notes (формат, исправления) ─│                    │                    │
   │ code ─────────────────────────────────────────────────────►│                    │
   │                     │                   │  exec(code)        │ read_file(...) ───►│
   │                     │                   │                    │◄── result ─────────│
   │◄── ExecResult(stdout, error, timeout, truncated, final_answer?) ──────────────────│
   │ Observation = notes + stdout (обрезка + пометка) + ошибка                          │
   │ StepMetrics(...) → recorder; budget.check()                                        │
   │ если final_answer → SolutionOutput(success=True); иначе следующий шаг             │
```

Observation всегда явно сообщает: не найден код; код был кривой и как его поняли;
таймаут и частичный вывод; вывод обрезан (сколько символов); правка сломала синтаксис/lint.

---

## 4. Модули

```
agent_smith/
├── pyproject.toml                 # scripts: sandbox = agent_smith.sandbox.cli:main
├── sandbox_template.json
├── config/models.json             # провайдеры → base_url, модели, имена env-переменных ключей
├── .env.example
├── mcp_tools_mbpp.py              # MCP-сервер MBPP (корень репо — требование)
├── mcp_tools_swebench.py          # MCP-сервер SWE-bench
├── agent_mbpp/__main__.py         # тонкий CLI → agent_smith.runners.mbpp
├── agent_swebench/__main__.py     # тонкий CLI → agent_smith.runners.swebench
└── src/agent_smith/
    ├── schemas.py                 # MBPPTaskInput, SWEBenchTaskInput, StepMetrics,
    │                              # SolutionOutput, SandboxConfig, AgentConfig
    ├── llm/
    │   ├── base.py                # LLMProvider (Protocol): complete(messages, stop, max_tokens) → LLMResponse
    │   ├── openai_compat.py       # реализация через httpx для всех OpenAI-совместимых API
    │   ├── key_pool.py            # список ключей, cooldown по 429, ротация round-robin
    │   ├── router.py              # fallback между провайдерами, retry + exponential backoff
    │   └── usage.py               # токены, latency, retries, кол-во запросов
    ├── extraction/
    │   ├── base.py                # Extractor → ExtractionResult(code, format, notes)
    │   ├── python_block.py        # ```python ... ``` / <end_code>, незакрытый блок
    │   ├── xml_invoke.py          # <invoke name=..><parameter ..>
    │   ├── hermes_json.py         # <tool_call>{...}</tool_call>
    │   ├── react.py               # Action: / Action Input:
    │   └── pipeline.py            # пробует по очереди, конвертирует в python-вызовы
    ├── sandbox/
    │   ├── executor.py            # Sandbox: старт worker, execute(code) → ExecResult, kill/restart
    │   ├── worker.py              # точка входа дочернего процесса: применяет ограничения, exec-цикл
    │   ├── guards/
    │   │   ├── imports.py         # meta_path-хук + свой __import__ по allowlist (с "math.*")
    │   │   ├── filesystem.py      # обёртка open/os.path: realpath ∈ allowed_directories
    │   │   ├── network.py         # подмена socket.* на исключение, до загрузки кода
    │   │   ├── builtins.py        # безопасный набор builtins (без eval/exec/compile/globals/...)
    │   │   ├── ast_check.py       # запрет dunder-атрибутов (__subclasses__, __globals__, ...)
    │   │   └── limits.py          # resource.setrlimit(RLIMIT_AS, RLIMIT_CPU)
    │   ├── protocol.py            # JSON-сообщения parent⇄worker: exec, tool_call, tool_result, done
    │   ├── final_answer.py        # FinalAnswerSignal (наследник BaseException, не Exception)
    │   ├── manual.py              # генерация мануала из схем MCP-тулов
    │   └── cli.py                 # `uv run sandbox` REPL: --mcp-stdio / --mcp-server / config.json
    ├── mcp_client/
    │   ├── client.py              # обёртка над mcp SDK: connect stdio | streamable HTTP
    │   └── registry.py            # list_tools/resources/prompts → ToolSpec(name, doc, params)
    ├── agent/
    │   ├── loop.py                # AgentLoop.run(task) → SolutionOutput
    │   ├── prompts/               # system_mbpp.md, system_swebench.md + few-shot примеры
    │   ├── prompt_builder.py      # system prompt = инструкции + мануал тулов + пример цикла
    │   ├── history.py             # сообщения, сжатие старых observation под бюджет токенов
    │   ├── observation.py         # форматирование ExecResult → текст для LLM
    │   ├── budget.py              # лимиты; считает остаток, режет max_tokens следующего запроса
    │   └── metrics.py             # MetricsRecorder → StepMetrics / итоги SolutionOutput
    ├── docker/
    │   └── container.py           # pull/run/exec/rm; контекстный менеджер с гарантированной очисткой
    ├── runners/
    │   ├── mbpp.py                # сборка: config → sandbox(mcp_tools_mbpp) → loop → solution.json
    │   └── swebench.py            # + контейнер, передача container_id в MCP-сервер через env/args
    └── tools_impl/                # чистая логика тулов, независимая от MCP и агента
        ├── fs.py                  # read_file, edit_file, list_files
        ├── search.py              # search_code, find_definition (ast/regex), find_references
        ├── exec.py                # run_command, run_tests, get_patch
        └── lint.py                # py_compile / ruff после edit_file
```

Принцип: `tools_impl` не знает про MCP, MCP-серверы не знают про агента, песочница не знает про
бенчмарк, цикл не знает про конкретного провайдера. Каждый слой тестируется отдельно
(требование: «tools must work independently of the agent loop»).

---

## 5. Песочница подробнее

**Уровни защиты (defense in depth):**

| Угроза | Защита |
|---|---|
| `import os, subprocess` | allowlist в собственном `__import__` + `sys.meta_path`-хук |
| `open("/etc/passwd")` | подменённый `open`: `os.path.realpath` должен лежать внутри `allowed_directories` |
| сеть | `socket.socket`/`create_connection` подменены до запуска кода; `socket` не в allowlist |
| `while True: pass` | `execute()` ждёт ответ с таймаутом → `SIGKILL` worker, перезапуск, частичный stdout возвращается |
| `x = " " * 10**10` | `RLIMIT_AS = max_memory_mb` → `MemoryError`/kill, сообщение LLM |
| `().__class__.__base__.__subclasses__()` | AST-фильтр dunder-атрибутов + урезанные builtins (нет `type`-трюков, `getattr` на `__*`) |
| `eval/exec/compile/globals/vars/breakpoint/input` | удалены из builtins |
| `KeyboardInterrupt`, `SystemExit` | не перехватываются, пробрасываются в цикл |

**Протокол parent ⇄ worker** (строки JSON через pipe):
```
parent → worker : {"type":"exec", "code": "..."}
worker → parent : {"type":"tool_call", "id":1, "name":"read_file", "args":{...}}
parent → worker : {"type":"tool_result", "id":1, "ok":true, "value":"..."}
worker → parent : {"type":"done", "stdout":"...", "error":null, "final_answer":null}
```
Таймаут считается только по времени выполнения кода в worker; пока родитель выполняет MCP-вызов,
таймер стоит на паузе.

**Конфиг:** `SandboxConfig` (Pydantic) читается из JSON, неизвестные поля → ошибка валидации.

---

## 6. LLM-слой

```
ProviderRouter.complete(messages, stop, max_tokens)
   for provider in [primary, *fallbacks]:
       for attempt in retries:
           key = provider.key_pool.next_available()
           try:  resp = provider.complete(...)      # httpx, timeout
                 usage.record(resp, retries=attempt); return resp
           except RateLimit:  key_pool.cooldown(key, retry_after)
           except Transient:  sleep(backoff(attempt))
   raise AllProvidersFailed           # цикл ловит → SolutionOutput(success=False, error=...)
```
Ключи: `OPENROUTER_API_KEY`, `OPENROUTER_API_KEY_2`, … (или через запятую) — только из env/`.env`.

---

## 7. Бюджеты и токены

- `Budget` знает лимиты бенчмарка и накопленные токены; перед каждым запросом:
  `max_tokens = min(per_step_cap, output_left)`; если `input_left` мал — `history` сжимает старые
  observation (оставляет последние N полностью, остальные — заголовок + «[сокращено]»).
- Если до лимита ≤ 1 шага — в промпт добавляется «последняя итерация, вызови final_answer».
- MBPP: короткий system prompt (~600–900 токенов), observation обрезается до ~1.5k символов.
- Любой выход из цикла (успех, лимит, исключение) → валидный `solution.json`.

---

## 8. Ошибки

| Где | Что делаем |
|---|---|
| LLM недоступен | retry → другой ключ → другой провайдер → `success=false, error=...` |
| Нет кода в ответе | observation «код не найден, используй ```python ... ```», шаг считается |
| Исключение в коде | traceback (обрезанный) в observation |
| Worker умер (OOM, kill) | перезапуск, сообщение LLM, переменные потеряны — сказать об этом |
| MCP-сервер упал | переподключение один раз, иначе ошибка в observation |
| Docker | `try/finally` + `atexit` + обработчик SIGTERM → `docker rm -f` |
