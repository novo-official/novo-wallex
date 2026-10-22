# Common tasks. Python 3.12 venv in .venv (see README).
PY := .venv/bin/python
MODEL := $(shell $(PY) -c "import tomllib; print(tomllib.load(open('config/llm.toml','rb'))['local']['model'])" 2>/dev/null)

.PHONY: setup test lint check llm-setup llm-check reproduce reproduce-llm-live recorder-start recorder-stop

setup:
	python3.12 -m venv .venv && $(PY) -m pip install -r requirements-dev.txt

test:
	$(PY) -m pytest -q

lint:
	.venv/bin/ruff check . && .venv/bin/ruff format --check .

check: lint test

# Local LLM (zero cost, offline): Ollama + the model pinned by name and digest in config/llm.toml.
llm-setup:
	@command -v ollama >/dev/null || brew install ollama
	@curl -sf http://localhost:11434/api/version >/dev/null || (brew services start ollama && sleep 5)
	ollama pull $(MODEL)
	$(PY) scripts/llm_check.py

llm-check:
	$(PY) scripts/llm_check.py

# Re-run the LLM ablation from the committed data and LLM cache: no model and no network needed.
reproduce:
	$(PY) scripts/check_reproduce.py

# Regenerate every LLM answer with the pinned local model into a fresh cache (hours of runtime).
reproduce-llm-live:
	LLM_CACHE_DIR=.cache/llm_regen $(PY) manage.py p4_experiment precompute --final
	LLM_CACHE_DIR=.cache/llm_regen $(PY) manage.py p4_experiment ablation --llm live-local --final

recorder-start:
	nohup scripts/run_recorder.sh >/dev/null 2>&1 &

recorder-stop:
	scripts/stop_recorder.sh
