# SWE-bench prompt text

Loaded by `agent_smith/agent/prompts.py` (`swe_system_prompt`, `swe_task_prompt`). The text is kept here, as plain
data, so that prompt wording can be edited and diffed without touching code. `{name}` placeholders are filled in a
single pass (values are never re-scanned, so braces inside a task's text are safe). Files end with one newline,
which is not part of the text.

| File | Used for |
|---|---|
| `swe_system.md` | system prompt frame (`{repo_root}`, `{format_rules}`, `{manual}`, `{guidance}`) |
| `swe_method.md` + `swe_example.md` | `{guidance}` of the full prompt |
| `swe_minimal.md` | `{guidance}` of the minimal prompt (ablation `--prompt-variant minimal`) |
| `swe_task.md` | first user message (`{repo}`, `{repo_root}`, `{problem_statement}`, `{hints}`) |
