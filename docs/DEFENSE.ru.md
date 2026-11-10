# Agent Smith — мануал по защите (RU)

> English version: [DEFENSE.en.md](DEFENSE.en.md).
> Цель: прийти на защиту и уметь запустить каждую часть, объяснить каждое решение и сделать
> небольшую «живую правку» за 2–5 минут (сабджект VI.4). Прочитайте целиком один раз, затем отрепетируйте раздел 6.

---

## 0. Что проверяет проверяющий (сабджект VI)

| Скрипт | Что происходит | Проходной балл |
|---|---|---|
| `exam_mbpp.sh` | 5 случайных задач MBPP: dump → `python -m agent_mbpp` → validate | 4/5, лимиты соблюдены |
| `exam_swebench.sh` | 3 случайные задачи SWE-bench: dump → `python -m agent_swebench` → validate → **удаление контейнера** | 2/3, лимиты соблюдены |
| `exam_sandbox.sh` | блок импортов, блок builtins, блок сети, ограничение путей, таймаут, лимит памяти, MCP-протокол | **все** |
| Ревью | обязательные тулы, агентный цикл, безопасность песочницы, отчёт бенчмарка, качество кода, **живые правки**, AI safety | — |

Жёсткие лимиты (суммарно на задачу, reasoning-токены тоже считаются):
MBPP — 10 итераций · 6 000 входных · 1 500 выходных · 120 с. SWE-bench — 30 · 300 000 · 10 000 · 900 с.
Лежат в `src/agent_smith/models.py` (`MBPP_LIMITS`, `SWEBENCH_LIMITS`) и совпадают с кодом moulinette
(`moulinette/moulinette/models.py`, `MetricsLimits`). В README moulinette указаны старые, меньшие цифры — верен код.

Мгновенный провал: API-ключ в коде; решение из PR/issues/интернета; обход песочницы; падение программы.

---

## 1. Чек-лист подготовки (накануне)

- [ ] `git status` чистый, всё запушено, вы на той ветке, которую будут проверять.
- [ ] Первая строка `README.md` содержит правильные логины 42.
- [ ] Локально есть `.env` с ключами **бесплатных** моделей (по возможности несколько ключей на провайдера), **не в git**
      (`git check-ignore -v .env` должен вывести правило).
- [ ] `uv sync && uv run pytest -q` → всё зелёное.
- [ ] Docker запущен; на macOS `export DOCKER_HOST=unix://$HOME/.docker/run/docker.sock`, иначе Docker SDK в moulinette
      не находит Docker Desktop.
- [ ] Образы стартовых задач SWE-bench скачаны заранее (несколько ГБ, x86_64; на Apple Silicon — эмуляция).
- [ ] `BENCHMARK_REPORT.md` заполнен реальными цифрами; `solution.json` закоммичены в `benchmarks/`.
- [ ] Умеете открыть `ui/index.html` (или опубликованную страницу) и показать трассу.
- [ ] Хотя бы раз прогнали раздел 6 (живые правки), затем `git checkout .`.

---

## 1a. Рекомендуемая связка на экзамен (всё бесплатно, проверено генеральной репетицией)

| Экзамен | Модель | Provider URL | Ключ | Результат репетиции |
|---|---|---|---|---|
| MBPP | `qwen/qwen3.8-27b` | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` | 5/5, 1 итерация, ~1 с на задачу |
| MBPP (запасной) | `north-mini-code-1.0` | `https://api.cohere.ai/compatibility/v1` | `COHERE_API_KEY` (trial: 1000 запросов/месяц, 20/мин) | 5/5, 1 итерация, ~1 с на задачу |
| SWE-bench | `nvidia/nemotron-3.5-lightning-30b-a3b` | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` | бенчмарк 3/3, 146 с на 3 задачи |
| SWE-bench (запасной) | `nvidia/nemotron-3-ultra-550b-a55b` | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` | репетиция 3/3 (случайные задачи), бенчмарк 2/3 |

Почему не одна модель на оба: у Groq лимит 8 000 токенов в минуту (промпт SWE-bench — 5–15 тыс.); NVIDIA медленнее,
но такого лимита нет. Бесплатные модели OpenRouter: только 50 запросов в сутки на аккаунт. Mistral: ключи только на
платном плане. Cerebras: нужна привязка карты. Рассуждения отключены по провайдерам в `config/models.json` (`extra_body`).
Репетиция: `./scripts/mock_exam.sh all --student-path . --moulinette-path ./moulinette --env-file .env`.

## 2. Сценарий демонстрации (что запускать, что говорить)

### 2.1 Песочница (категории exam_sandbox.sh)

```bash
uv run sandbox sandbox_template.json
```

Вводите по одной строке; справа — ожидаемый результат:

| Ввод | Ожидаемо | Какой слой останавливает |
|---|---|---|
| `import os` | `SecurityError: … import of 'os' is not allowed` | AST-политика + allowlist импортов |
| `__import__('os')` | `SecurityError: use of name '__import__'` | AST-политика (dunder-имена) |
| `eval('1+1')` | `NameError: name 'eval' is not defined` | урезанные builtins |
| `().__class__.__bases__[0].__subclasses__()` | `SecurityError … '__subclasses__'` | AST-политика (dunder-атрибуты) |
| `getattr((), '__class__')` | `SecurityViolation` | безопасный `getattr` |
| `import random; random._os.system('id')` | `AttributeError: access to private attribute 'random._os'` | прокси модулей |
| `open('/etc/passwd').read()` | `PermissionError: Sandbox: access to path '/etc/passwd' is denied` | audit hook |
| `open('/tmp/agent/x.txt','w').write('ok')` | `Out: 2` (разрешённая директория) | — |
| `while True: pass` | `TimeoutError … [NOTE] … PARTIAL` через 30 с | SIGALRM (+ жёсткий kill из родителя) |
| `x = bytearray(1024*1024*1024)` | `MemoryError: … memory limit of 512 MB` | RLIMIT_AS (Linux) / watchdog RSS в родителе |
| `final_answer("done")` | `[final_answer] done` | встроенная функция, не MCP |
| `exit` / Ctrl+D | REPL корректно завершается | — |

Сеть: `socket` нет в allowlist. Чтобы показать второй слой, запустите с конфигом, где он разрешён:
`{"authorized_imports": ["socket"]}`, затем `import socket; socket.create_connection(("1.1.1.1", 80))` →
`PermissionError: Sandbox: network access is disabled (socket.connect)` (audit hook).

MCP-протокол:

```bash
uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py" sandbox_template.json --manual   # сгенерированный мануал тулов
uv run sandbox --mcp-stdio "python mcp_tools_swebench.py" sandbox_template.json        # 9 тулов как функции
python mcp_tools_mbpp.py --http --port 8000 &                                          # streamable HTTP
uv run sandbox --mcp-server http://127.0.0.1:8000/mcp
```

Внутри: `print(run_tests("def f(): return 1", ["assert f() == 1"]))`, `print(list_resources())`.

Что сказать: *«Песочница — отдельный процесс с пустым окружением. Пять слоёв: статическая проверка AST,
урезанные builtins, read-only прокси модулей, неснимаемый audit hook и лимиты ОС, которые применяет родитель.
MCP-тулы работают вне песочницы: воркер шлёт JSON-запрос хосту, а MCP-клиент живёт в хосте.
Таймаут ставится на паузу на время вызова тула.»*

### 2.2 MBPP

```bash
cd moulinette && uv run moulinette_eval dump mbpp --output ../cache/mbpp_task.json && cd ..
uv run python -m agent_mbpp --task-file cache/mbpp_task.json --output cache/mbpp_solution.json \
  --model-name "<бесплатная модель>" --provider-url "https://openrouter.ai/api/v1" --env-file .env --trace-file runs/live.jsonl
cd moulinette && uv run moulinette_eval validate mbpp ../cache/mbpp_task.json ../cache/mbpp_solution.json
uv run moulinette_eval display ../cache/mbpp_solution.json      # проверки консистентности должны быть чистыми
```

(на этом Mac moulinette нужен `PYTHONPATH=.` перед `uv run`.)

Покажите в `solution.json`: `system_prompt`, у каждого шага `llm_output` / `sandbox_input` / `sandbox_output` /
`retries` / `api_url` / `model_name`; итоги = суммы по шагам.

### 2.3 SWE-bench

```bash
uv run python -m agent_swebench --task-file cache/swebench_task.json --output cache/swebench_solution.json \
  --model-name "<бесплатная модель>" --provider-url "https://openrouter.ai/api/v1" --env-file .env --trace-file runs/swe.jsonl
docker ps -a --filter label=agent-smith      # после прогона должно быть ПУСТО: контейнер удалён
cd moulinette && uv run moulinette_eval validate swebench ../cache/swebench_task.json ../cache/swebench_solution.json
```

### 2.4 Визуализатор

`uv run agent-ui --trace runs/live.jsonl` → http://127.0.0.1:8765, агента запускать с тем же `--trace-file`.
Показывает цикл, вызов тула по хопам, diff между ответом LLM и исполненным кодом, путь выполнения кода.

---

## 3. Архитектура за 60 секунд

```
task.json → CLI (agent_mbpp / agent_swebench) → Оркестратор (agent/loop.py) ⇄ LLM (llm/)
                                                    │ сырой текст
                                                    ▼
                                           Извлечение кода (extraction/)
                                                    │ python
                        ┌──────────────── Песочница ────────────────┐
                        │ воркер ── tool_call ──▶ хост ─────────────┼─ MCP-клиент ── stdio/HTTP ──▶ MCP-сервер
                        │ final_answer() встроен  ◀── результат ──  │                               (mcp_tools_*.py)
                        └───────────────────────────────────────────┘                                    │ docker exec
                                                                                                  контейнер /testbed
```

| Вопрос | Ответ | Файл |
|---|---|---|
| Где цикл? | `CodeAgent.run` | `src/agent_smith/agent/loop.py` |
| Где проверяются лимиты? | `Budget.can_start_step` перед каждым вызовом | `src/agent_smith/agent/budget.py` |
| Где промпт? | `mbpp_system_prompt`, `swebench_system_prompt` | `src/agent_smith/agent/prompts.py` |
| Где генерируется мануал тулов? | `build_manual` из MCP-схем | `src/agent_smith/sandbox/manual.py` |
| Где извлекается код? | `extract_code` | `src/agent_smith/extraction/extractor.py` |
| Где исполняется код? | `Worker.run` (дочерний процесс), `Sandbox.execute` (родитель) | `src/agent_smith/sandbox/worker.py`, `host.py` |
| Где обнаруживаются тулы? | `MCPClient._discover` → `tool_spec_from_schema` | `src/agent_smith/mcp_client/client.py` |
| Где реализованы тулы? | `SWEBenchTools`, `mbpp_tools.run_tests` | `src/agent_smith/tools/` |
| Где ротация ключей? | `KeyPool`, `LLMClient.chat` | `src/agent_smith/llm/key_pool.py`, `client.py` |
| Где Docker? | `DockerEnv` | `src/agent_smith/docker_env.py` |
| Где модели из сабджекта? | `SandboxConfig`, `StepMetrics`, `SolutionOutput`… | `src/agent_smith/models.py` |

---

## 4. Разбор по частям (уметь объяснить каждую)

### 4.1 Агентный цикл (`agent/loop.py`)
1. `_messages()` собирает: системный промпт, задачу, затем пары `(assistant, "Observation: …")`. Полностью
   отправляются только последние `keep_recent_steps` observation; старые сокращаются (`old_observation_chars`).
2. `Budget.can_start_step(estimate)`: остались итерации, времени > 5 с, оценка входа ≤ остатка входного бюджета
   (оценка = символы/3 × 1.1, с запасом), остаток выхода ≥ 64. Если оценка не влезает, цикл сначала сжимает все
   старые observation, и если всё равно не влезает — останавливается. **Вызов, который может пробить лимит, не отправляется.**
3. `max_tokens = min(max_output_tokens_per_call, остаток выхода)`; таймаут HTTP = оставшееся время.
4. `llm.chat(..., stop=STOP_SEQUENCES)`.
5. `extract_code()`; нет кода → `NO_CODE_FEEDBACK` (считается итерацией).
6. `sandbox.execute(code)`; observation = пометки + stdout + `Out: <последнее выражение>` + ошибка + пометки песочницы.
7. `final_answer` → `SolutionOutput(success=True)`. Бюджет кончился → `fallback_answer()` (MBPP: последний код,
   прошедший `run_tests`, иначе последний испробованный; SWE-bench: текущий `get_patch()`), `success=False`, причина в `error`.

Почему `success=False` при fallback? Честность: поле означает «агент считает, что решил». Moulinette это поле
не читает (она сама проверяет решение), так что кандидат всё равно может засчитаться.

### 4.2 Пять ситуаций обратной связи (сабджект V.1, красная рамка) — где обрабатываются
| Ситуация | Сообщение | Код |
|---|---|---|
| Нет валидного блока кода | `[ERROR] No valid code block was found…` + ожидаемый формат | `extraction/extractor.py` `NO_CODE_FEEDBACK` |
| Кривой блок, но интерпретирован | `[NOTE] Your ```python block was not closed…`, блок без тега, `>>>`-подсказки, dedent, XML/JSON/ReAct сконвертированы | заметки `extract_code` |
| Таймаут, частичный вывод | уже полученный вывод + `[NOTE] Execution hit the timeout; the output above is PARTIAL.` | `host.py` `ExecutionResult.render` |
| Вывод обрезан | `[NOTE] Printed output exceeded the sandbox limit…`; тулы пишут `[truncated: …]`; длинные observation — `[... observation truncated …]` | `StreamingStdout` в воркере, `swebench_tools.truncate_middle`, `loop.truncate_observation` |
| Правка внесла синтаксическую ошибку | `Error: the edit was NOT applied because it would introduce a syntax error … SyntaxError … at line N` + контекст | `swebench_tools.SWEBenchTools._syntax_problem` |

Дополнительно: перезапуск песочницы (переменные потеряны), повтор одинакового кода, строка бюджета в SWE-bench.

### 4.3 Форматы извлечения кода (`extraction/extractor.py`)
Порядок: блоки ```python (несколько — склеиваются, с пометкой) → незакрытый блок → блок без тега, который
парсится → `<code>` → XML `<invoke name><parameter>` → Hermes `<tool_call>{json}` → ReAct `Action / Action Input`.
Не-Python форматы превращаются в `result = tool(k=v)\nprint(result)` (или `final_answer(...)`).
Stop sequences: `<end_code>`, `\nObservation:`, `</tool_call>`, `</function_calls>` (4 — максимум у многих провайдеров).
Не дают модели написать фальшивый Observation; экстрактор понимает закрывающие теги, срезанные стопом.

### 4.4 Песочница (`sandbox/worker.py`, `sandbox/host.py`)
**Почему отдельный процесс?** В том же процессе нельзя остановить цикл на C-уровне (`10**10**8`), нельзя применить
лимит памяти ОС, а API-ключи агента лежат в `os.environ`. Дочерний процесс даёт: жёсткий kill, `setrlimit`, пустое
окружение, свою сессию (Ctrl+C приходит только родителю). Цена — прокси вызовов тулов (он же граница безопасности, нужная для MCP).

Слои — в том порядке, в котором код на них натыкается:
1. **AST-политика** (`PolicyChecker`): запрещает dunder-атрибуты, кроме безопасного списка (`__init__`, `__name__`…),
   атрибуты фреймов и кода (`f_globals`, `tb_frame`, `gi_frame`…), dunder-имена (`__builtins__`, `__import__`),
   импорты вне `authorized_imports` (`math.*` = подмодули), относительные импорты.
2. **Урезанные builtins** (`REMOVED_BUILTINS`): нет `eval exec compile input breakpoint globals locals vars help exit quit`;
   `getattr/hasattr/setattr/delattr` отказывают на запрещённых именах; `__import__` — это `guarded_import`.
3. **Прокси модулей** (`make_module_proxy`): импортированные модули — read-only обёртки; `_private` и неразрешённые
   подмодули скрыты (`random._os`, `typing.sys`, `collections._sys`). Настоящий модуль лежит в замыкании, без
   dunder-доступа до него не добраться.
4. **Audit hook** (`Worker.audit_hook`, `sys.addaudithook`, снять нельзя): `open` вне `allowed_directories`
   (realpath, поэтому `..` и симлинки не помогают; stdlib разрешён только на чтение для ленивых импортов), «сырые» fd
   (протокол), `os.listdir/remove/rename…` вне разрешённых путей, все `socket.*`, `subprocess.*`,
   `os.system/exec/fork/kill`, `ctypes`, `sys.settrace`, импорты жёстко запрещённых модулей (`os`, `sys`, `subprocess`…),
   если их явно не разрешили.
5. **Лимиты**: `signal.setitimer` бросает `SandboxTimeout(BaseException)` — `except Exception` его не поймает;
   на паузе в `call_host_tool` во время вызова MCP. Если воркер не ответил за timeout + 2 с (C-код), хост его убивает,
   перезапускает и сообщает, что состояние сброшено. Память: `RLIMIT_AS` на Linux (MemoryError внутри, состояние живо —
   проверено в Linux-контейнере); на всех ОС хост раз в 100 мс смотрит RSS и проверяет пиковый RSS, который приходит
   с каждым результатом (ловит выделения быстрее периода опроса).

Ещё: протокол на приватных dup-fd, fd 0/1 → `/dev/null` (код не может подделать сообщения); `SystemExit` /
`KeyboardInterrupt` из пользовательского кода `Sandbox.execute` ре-рейзит, чтобы цикл корректно остановился
(и всё равно записал `solution.json`); `final_answer` внедряет песочница, это **не** MCP-тул; значение последнего
выражения показывается как в REPL (`Out: …`).

**Таймаут только для кода песочницы** (сабджект): воркер ставит itimer на паузу на время вызова тула, хост
вычитает время тулов из своего дедлайна (`tool_time`). Тест: `test_tool_time_does_not_count_toward_sandbox_timeout`.

### 4.5 MCP
- Клиент: `MCPClient` крутит async SDK в фоновом event loop; `connect()` → `initialize` → `list_tools` → `ToolSpec`
  (имя, описание, параметры с типами из JSON-схемы, обязательные — первыми). Ничего не захардкожено: **неизвестный
  MCP-сервер** на защите работает так же (тест `test_unknown_server_tools_are_discovered_dynamically`).
- Обёртки: `Worker.make_tool` раскладывает позиционные аргументы в именованные по схеме, проверяет недостающие/лишние.
- Resources/prompts → `list_resources() / read_resource(uri) / list_prompts() / get_prompt(name, arguments)`.
- Транспорты: stdio (`--mcp-stdio`) и streamable HTTP (`--mcp-server URL`, серверы: `--http --port`).
- Серверы возвращают `CallToolResult` с `_meta["agent_smith"]` (команды, коды выхода, тайминги, происхождение данных) —
  другие клиенты это игнорируют, UI использует.

### 4.6 Обязательные тулы (`tools/swebench_tools.py`)
Исполнитель: `DockerExecutor` (`docker exec` в `SWEBENCH_CONTAINER`) или `LocalExecutor` (`TESTBED_PATH`) —
интерфейс одинаковый, поэтому тулы тестируются без Docker (`tests/test_swebench_tools.py`).
- `read_file` → `N: строка`, до 400 строк за вызов + подсказка, как листать.
- `edit_file` → нужно ровно одно совпадение; 0 → похожие строки; >1 → номера строк; проверка синтаксиса → правка
  **отклоняется и не пишется**, если ломает Python; удаляет устаревшие `.pyc` (правка того же размера в ту же секунду
  иначе была бы проигнорирована кэшем Python по mtime+size — нашли тестом).
- `list_files` (`find`, без `.git`…), `search_code` (`/abs/path.py:N содержимое`, невалидный regex → поиск строкой),
  `search_function_or_class_definition_in_code` (`def|class имя`), `find_references` (целое слово, заданный файл первым).
- `run_tests` → eval-скрипт задачи (из `SWEBENCH_EVAL_SCRIPT_FILE`, иначе `/eval.sh`), берёт часть между
  `>>>>> Start/End Test Output` как moulinette, сводка упавших тестов + хвост.
- `get_patch` → `git -c core.fileMode=false diff`. `run_command` → код выхода, stdout, stderr, таймаут.

MBPP `run_tests(code, tests=None)`: тесты берутся из **task.json** (moulinette, через `MBPP_TASK_FILE`), а не от LLM;
кандидат + по одному `try/except` на каждый assert выполняются в новой песочнице; отчёт с got/expected.
Первый тест moulinette скрывает — промпт требует общего решения.

### 4.7 LLM-слой (`llm/`)
- `OpenAICompatProvider`: один HTTP-клиент для любого OpenAI-совместимого API; классы ошибок решают: ретрай/ротация/отключить ключ.
- `KeyPool`: по кругу; 429 → cooldown (`Retry-After`) и следующий ключ; 401/403/402 → ключ отключается; все на
  cooldown → ждём ближайший; без ключа — для Ollama.
- `LLMClient.chat`: каждая HTTP-попытка — запрос (`total_requests`), неудачные попытки = `retries`; backoff с джиттером,
  ограниченный дедлайном; опциональные запасные провайдеры (`--fallback`, `config/models.json`).
- Ключи только из env/.env: `<PROVIDER>_API_KEY`, `_2…_9`, `<PROVIDER>_API_KEYS=a,b`; провайдер определяется по хосту URL.
- Учёт: `prompt_tokens`/`completion_tokens` (reasoning-токены входят в completion); нет usage → оценка + предупреждение.

### 4.8 Особенности SWE-bench (`agent_swebench`, `docker_env.py`)
Вариант (b) из сабджекта: песочница на хосте, тулы ходят в Docker. Контейнер: `--network none` (агент не может
скачать фикс — AI safety), label `agent-smith=1`, `--platform linux/amd64` для x86_64-образов, удаляется в `finally`,
через `atexit` и по SIGTERM (превращается в `SystemExit`). Часы стартуют после того, как образ есть локально
(скачивание — инфраструктура). Валидатор применяет патч в **новом** контейнере, так что остатки не помогли бы, но
убирать их всё равно обязательно.

### 4.9 AI safety (VI.4.1 — нарушение = 0)
Раздел «Integrity» в системном промпте; контейнер без сети; тулы видят только репозиторий; каждый шаг логируется
(`llm_output`, `sandbox_input`, `sandbox_output`), проверяющий видит исследование кода; веб- и PR-тулов нет вообще.

---

## 5. Вероятные вопросы и короткие ответы

1. **Почему не smolagents/LangGraph?** Запрещено (оркестрация). Наш цикл ~200 строк в `agent/loop.py`; используем
   только официальный MCP SDK (протокол, не оркестрация), pydantic, httpx.
2. **Почему код вместо JSON tool calls?** Переменные живут между шагами, циклы/условия, несколько вызовов тулов за шаг →
   меньше итераций и токенов (статья CodeAct).
3. **Как понять, что модель не выдумала observation?** Stop sequences + observation — только то, что вернула песочница;
   `llm_output` в `solution.json` — сырой текст до извлечения.
4. **Модель пишет в другом формате?** Экстрактор конвертирует XML/JSON/ReAct и сообщает модели, что сделал.
5. **Как считаются токены?** usage от провайдера на каждый запрос; итоги — суммы по шагам; бюджет не пускает вызов,
   если оценка больше остатка входного бюджета.
6. **А reasoning-модели?** Их рассуждения входят в `completion_tokens` → считаются; для MBPP (1 500 выходных) лучше
   non-reasoning модели.
7. **Как таймаут не учитывает время MCP?** itimer на паузе в `call_host_tool`; хост вычитает `tool_time`.
8. **Как убить `while True` в C-коде?** Хост жёстко убивает через timeout + 2 с, перезапускает воркер, сообщает о сбросе.
9. **Может ли код прочитать API-ключ?** Нет: окружение воркера — только `PATH, LANG, PYTHONIOENCODING, PYTHONHASHSEED`,
   `os` не импортируется, MCP-серверы тоже получают env без `*API_KEY*`.
10. **Побег через `().__class__.__subclasses__()`?** AST запрещает dunder-атрибуты, `getattr` тоже; даже доберись код
    до `os`, опасные операции заблокирует audit hook.
11. **Зачем audit hook, если есть фильтры?** Фильтры обходятся через объекты, о которых не подумали; audit-события
    срабатывают внутри CPython на саму операцию (`open`, `socket.connect`…), и хук нельзя снять.
12. **Неизвестный MCP-сервер?** Тулы через `list_tools`, обёртки и мануал — из схем.
13. **Почему `final_answer` не MCP-тул?** Он управляет циклом (сабджект); его внедряет песочница, он есть при любом сервере.
14. **Что будет при `sys.exit()` в коде?** `sys` не импортируется; `raise SystemExit` → ре-рейз в `Sandbox.execute` →
    цикл останавливается → `solution.json` пишется с причиной.
15. **Rate limit на экзамене?** Несколько ключей по кругу; backoff; опционально запасной провайдер.
16. **Почему mcp 1.x?** В 2.x поменяли API; 1.x — стабильная ветка; протокол версионируется и согласуется.
17. **Почему первая итерация MBPP иногда только определяет функцию?** Модель игнорирует паттерн «одним шагом»;
    observation подсказывает «no output — use print()»; пример в промпте показывает тест + `final_answer` в одном блоке.
18. **Как не пробить 6k входа на MBPP?** Компактный промпт (~600 токенов), короткие observation (`MBPP_SETTINGS`),
    паттерн одного шага; вызов, который пробил бы лимит, не отправляется.
19. **Где конфигурация?** `sandbox_template.json` (SandboxConfig), `config/models.json` (провайдеры), `.env` (ключи), флаги CLI.
20. **Как тулы тестируются независимо?** `LocalExecutor` на временном git-репо (`tests/test_swebench_tools.py`) и через
    настоящий MCP stdio (`tests/test_mcp.py`).

---

## 6. Тренировка живых правок (2–5 мин на каждую, потом `git checkout .`)

После каждой правки перезапуск:
`uv run python -m agent_mbpp --task-file cache/mbpp_task.json --output cache/s.json --model-name … --provider-url … --env-file .env`
и показать эффект в `cache/s.json` (или `moulinette_eval display`).

| # | Просьба | Где | Как |
|---|---|---|---|
| 1 | Максимум 3 итерации | `src/agent_mbpp/__main__.py` или CLI | `--max-iterations 3` (без правки кода) или `limits.max_iterations = 3` в `body()` |
| 2 | Поменять системный промпт | `agent/prompts.py` `mbpp_system_prompt` | правка текста; показать в `solution.json["system_prompt"]` |
| 3 | Логировать что-то на каждом шаге | `agent/loop.py` `CodeAgent.run`, после `self._record_step(...)` | `print(f"step {step_no}: {chat.input_tokens} in", file=sys.stderr)` |
| 4 | Добавить поле в каждый observation (например, время) | `agent/loop.py` `_observation` | `parts.append(f"[time] {self.budget.elapsed():.1f}s")` |
| 5 | Разрешить ещё импорт (например, `statistics`) | `sandbox_template.json` / `models.py` `SandboxConfig.authorized_imports` | добавить имя; передать `--sandbox-config sandbox_template.json` |
| 6 | Запретить ещё builtin (например, `sorted`) | `sandbox/worker.py` `REMOVED_BUILTINS` | добавить имя; `uv run sandbox -c "sorted([2,1])"` → NameError |
| 7 | Новый MCP-тул для MBPP | `mcp_tools_mbpp.py` | `@mcp.tool()\n@report\ndef count_lines(code: str) -> str: return str(len(code.splitlines()))` → сам появится в мануале |
| 8 | Поменять temperature | `llm/client.py` `LLMClient.__init__(temperature=0.0)` или в `build_client` | `LLMClient(endpoints, temperature=0.2, ...)` |
| 9 | Поменять таймаут кода песочницы | `SandboxConfig.max_execution_time_seconds` | JSON-конфиг, `uv run sandbox cfg.json`, затем `while True: pass` |
| 10 | Короче observation | `src/agent_mbpp/__main__.py` `MBPP_SETTINGS.observation_max_chars` | например 500; показать `[... observation truncated …]` |
| 11 | Другая stop sequence | `extraction/extractor.py` `STOP_SEQUENCES` | у многих провайдеров максимум 4: заменить одну |
| 12 | Выводить ретраи | `llm/client.py` `chat`, ветка `except LLMError` | `print("retry", exc, file=sys.stderr)` |
| 13 | Другая модель | CLI `--model-name`; запасной список `config/models.json` + `--fallback` | — |
| 14 | Короче вывод `run_tests` | `tools/mbpp_tools.py` `run_tests` (сборка отчёта) | оставить только строку вердикта |
| 15 | Размер страницы read_file | `tools/swebench_tools.py` `MAX_READ_LINES` | 400 → 100; вызвать `read_file` на длинном файле |

Откат: `git checkout . && git clean -fd cache` (не удалите свой `.env`!).

---

## 7. Отчёт бенчмарка — как представлять
- Setup: ≥5 бесплатных моделей × одни и те же ≥3 задачи SWE-bench (`scripts/run_benchmark.sh`).
- Таблица результатов, надёжность провайдеров (средняя задержка, ретраи, отказы), промежуточные метрики
  (первое касание файла из патча, первое снижение числа падающих тестов, шагов между первым зелёным прогоном и
  `final_answer`) — генерирует `scripts/benchmark_table.py` из `benchmarks/**/solution.json`.
- Ablation: та же модель и задачи, одно изменение (например, без раздела «Method» в SWE-промпте).
- Выводы: какая модель выбрана и почему; какие отбросить и почему — **только по своим данным**.

---

## 8. Честные ограничения (скажите раньше, чем спросят)
- Часть задач MBPP противоречит своему скрытому тесту. Пример — задача 452: в условии «вернуть none, если убытка нет»,
  а скрытый первый тест ждёт `loss_amount(1500,1200)==0`. Агент следует условию; поэтому порог — 4/5, а не 5/5.
- Песочница в `run_tests` для MBPP строже, чем Docker-Python в moulinette (allowlist): решение с неразрешённым импортом
  не пройдёт наши тесты, хотя прошло бы их (проверено: все 425 эталонных решений проходят наши).
- Оценка токенов перед вызовом эвристическая (символы/3 с запасом); реальные числа — от провайдера.
- На macOS вместо `RLIMIT_AS` воркер убивает watchdog RSS (состояние теряется, об этом сообщается); на Linux — MemoryError внутри.
- `get_patch` включает только отслеживаемые файлы (новые нужно `git add -N`; агенту сказано держать черновики в /tmp).
- Образы SWE-bench — x86_64: на Apple Silicon эмуляция, медленно.

## 9. Словарь
CodeAct — использование тулов через код. MCP — Model Context Protocol (JSON-RPC: tools, resources, prompts).
Stop sequence — строка, на которой генерация обрывается. Audit hook — колбэк CPython на события, важные для безопасности.
RLIMIT_AS — лимит адресного пространства. RSS — резидентная память. Observation — то, что песочница возвращает LLM.
