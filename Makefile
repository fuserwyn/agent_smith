# Agent Smith: shortcuts for setup, UI, tests and the mock exam.  `make` = this help.
#
# Variables (override on the command line, e.g. `make swe TASKS="sympy__sympy-13480"`):
ENV        ?= .env
MOULINETTE ?= ./moulinette
PORT       ?= 8765
TASKS      ?=
K          ?=

EXAM := ./scripts/mock_exam.sh
COMMON = --student-path . --moulinette-path $(MOULINETTE) --env-file $(ENV)
SWE_TASKS = $(if $(TASKS),--tasks "$(TASKS)",)

.DEFAULT_GOAL := help
.PHONY: help install doctor ui ui-stop test test-fast sandbox mbpp swe exam exam-openrouter \
        clean clean-traces clean-all school-setup school-uninstall

help: ## Show this list
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36mmake %-17s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo
	@echo "  Variables: ENV=$(ENV)  MOULINETTE=$(MOULINETTE)  PORT=$(PORT)  TASKS=\"$(TASKS)\"  K=\"$(K)\""

# ---------------------------------------------------------------- setup
install: ## Install dependencies (agent + moulinette)
	uv sync
	@if [ -f $(MOULINETTE)/pyproject.toml ]; then (cd $(MOULINETTE) && uv sync); \
	 else echo "note: no moulinette at $(MOULINETTE) (set MOULINETTE=path)"; fi

doctor: ## Check uv, Docker, .env and moulinette before a run
	@ok=1; \
	command -v uv >/dev/null && echo "  ok    uv $$(uv --version | cut -d' ' -f2)" || { echo "  FAIL  uv not installed"; ok=0; }; \
	docker info >/dev/null 2>&1 && echo "  ok    Docker is running" || { echo "  FAIL  Docker is not running"; ok=0; }; \
	[ -f $(ENV) ] && echo "  ok    $(ENV) present" || { echo "  FAIL  $(ENV) missing (copy .env.example)"; ok=0; }; \
	[ -f $(MOULINETTE)/pyproject.toml ] && echo "  ok    moulinette at $(MOULINETTE)" || { echo "  FAIL  no moulinette at $(MOULINETTE)"; ok=0; }; \
	[ -d .venv ] && echo "  ok    .venv" || echo "  warn  no .venv yet: run make install"; \
	[ $$ok = 1 ]

# ---------------------------------------------------------------- UI
ui: ## Start the UI (http://127.0.0.1:PORT, default 8765)
	uv run agent-ui --port $(PORT)

ui-stop: ## Stop a UI left running on PORT
	-@lsof -ti tcp:$(PORT) | xargs kill 2>/dev/null && echo "stopped UI on port $(PORT)" || echo "nothing on port $(PORT)"

# ---------------------------------------------------------------- tests (offline, no API keys used)
test: ## Run the whole test suite
	uv run pytest -q

test-fast: ## Run tests matching K (e.g. make test-fast K=ui), stop at first failure
	uv run pytest -q -x $(if $(K),-k "$(K)",)

# ---------------------------------------------------------------- mock exam (real LLM calls)
sandbox: ## Sandbox security checks (no LLM)
	$(EXAM) sandbox $(COMMON)

mbpp: ## Mock exam: 5 MBPP tasks
	$(EXAM) mbpp $(COMMON)

swe: ## Mock exam: SWE-bench (random 3, or TASKS="id id ...")
	$(EXAM) swebench $(COMMON) $(SWE_TASKS)

exam: ## Full mock exam: sandbox + MBPP + SWE-bench (standard models)
	$(EXAM) all $(COMMON) $(SWE_TASKS)

exam-openrouter: ## Full mock exam on the OpenRouter model
	$(EXAM) all $(COMMON) $(SWE_TASKS) --paid

# ---------------------------------------------------------------- cleanup
clean: ## Remove runs, cache, evaluations, leftover agent containers
	./scripts/clean.sh

clean-traces: ## clean + traces/logs next to benchmark results
	./scripts/clean.sh --traces

clean-all: ## Everything incl. Docker images and .venv (asks first)
	@printf "Remove Docker images and virtualenvs too? [y/N] "; read a; [ "$$a" = y ] && ./scripts/clean.sh --all || echo "cancelled"

# ---------------------------------------------------------------- 42 school computers
school-setup: ## Set up on a 42 computer (heavy data in sgoinfre)
	./scripts/school_setup.sh --moulinette $(MOULINETTE) --env $(ENV)

school-uninstall: ## Remove everything school-setup created (incl. keys)
	./scripts/school_setup.sh --uninstall
