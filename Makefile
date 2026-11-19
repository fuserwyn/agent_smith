# Agent Smith: short commands.  `make` = this list.
# On 42 computers .school.env (written by `make school`) is loaded by every command: no `source` needed.
#
# Variables (on the command line, e.g. `make swe T="sympy__sympy-13480"`):
M          ?=
ENV        ?= .env
MOULINETTE ?= ./moulinette
PORT       ?= 8765
T          ?=
K          ?=

SH   := $(if $(wildcard .school.env),. ./.school.env &&,)
EXAM := $(SH) ./scripts/mock_exam.sh
ARGS  = --student-path . --moulinette-path $(MOULINETTE) --env-file $(ENV) $(if $(T),--tasks "$(T)",)

.DEFAULT_GOAL := help
.PHONY: help check install up down test sandbox mbpp swe exam exam-or clean clean-logs clean-all school purge rm-env

help: ## this list
	@echo "Agent Smith"
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  make %-10s %s\n", $$1, $$2} /^# ---- / {sub(/^# ---- /, ""); printf "\n %s\n", $$0}' $(MAKEFILE_LIST)
	@echo
	@echo " Options: T=\"task ids\" (swe, exam)  K=word (test)  M=moulinette (school)  PORT=$(PORT)  ENV=$(ENV)  MOULINETTE=$(MOULINETTE)"

# ---- start
check: ## is everything ready? (uv, Docker, .env, moulinette)
	@$(SH) ok=1; \
	command -v uv >/dev/null && echo "  ok    uv $$(uv --version | cut -d' ' -f2)" || { echo "  FAIL  uv not installed (make school / make install)"; ok=0; }; \
	docker info >/dev/null 2>&1 && echo "  ok    Docker is running" || { echo "  FAIL  Docker is not running"; ok=0; }; \
	[ -f $(ENV) ] && echo "  ok    $(ENV) present" || { echo "  FAIL  $(ENV) missing (cp .env.example .env)"; ok=0; }; \
	[ -f $(MOULINETTE)/pyproject.toml ] && echo "  ok    moulinette at $(MOULINETTE)" || { echo "  FAIL  no moulinette at $(MOULINETTE)"; ok=0; }; \
	[ -e .venv ] && echo "  ok    .venv" || echo "  warn  no .venv yet: make install"; \
	[ $$ok = 1 ]

install: ## install dependencies (agent + moulinette)
	$(SH) uv sync
	@if [ -f $(MOULINETTE)/pyproject.toml ]; then $(SH) cd $(MOULINETTE) && uv sync; \
	 else echo "note: no moulinette at $(MOULINETTE)"; fi

up: ## start the UI  ->  http://127.0.0.1:8765
	$(SH) uv run agent-ui --port $(PORT)

down: ## stop the UI
	-@lsof -ti tcp:$(PORT) | xargs kill 2>/dev/null && echo "UI stopped" || echo "UI not running"

# ---- tests (offline, no API key used)
test: ## all tests;  make test K=ui  = only matching, stop at 1st failure
	$(SH) uv run pytest -q $(if $(K),-x -k "$(K)",)

# ---- mock exam (real AI calls)
sandbox: ## sandbox security checks (no AI)
	$(EXAM) sandbox $(ARGS)

mbpp: ## 5 MBPP tasks
	$(EXAM) mbpp $(ARGS)

swe: ## 3 random SWE-bench tasks  (or T="id id")
	$(EXAM) swebench $(ARGS)

exam: ## full mock exam: sandbox + MBPP + SWE-bench
	$(EXAM) all $(ARGS)

exam-or: ## full mock exam on the OpenRouter model
	$(EXAM) all $(ARGS) --paid

# ---- cleanup
clean: ## remove runs, cache, evaluations, stray containers
	./scripts/clean.sh

clean-logs: ## clean + traces/logs of benchmark results
	./scripts/clean.sh --traces

clean-all: ## also Docker images and .venv (asks first)
	@printf "Remove Docker images and virtualenvs too? [y/N] "; read a; [ "$$a" = y ] && ./scripts/clean.sh --all || echo "cancelled"

# ---- 42 school computers
school: ## set up here, heavy data in sgoinfre  (M=moulinette folder/archive if not found)
	./scripts/school_setup.sh $(if $(M),--moulinette $(M),)

purge: ## after the defense: remove images, sgoinfre data (keeps .env)
	./scripts/school_setup.sh --uninstall

rm-env: ## delete the API keys: ./.env and ~/.agent_smith.env (asks first)
	@ls -l $(ENV) $(HOME)/.agent_smith.env 2>/dev/null | awk '{print "  " $$NF}' || true
	@printf "Delete the API keys above? [y/N] "; read a; \
	if [ "$$a" = y ]; then rm -f $(ENV) $(HOME)/.agent_smith.env && echo "keys deleted"; else echo "cancelled"; fi
