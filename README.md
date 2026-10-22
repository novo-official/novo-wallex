# Wallex Algorithmic Trading System

Spot-only, long-only algorithmic trading for the Wallex hackathon challenge.
Django 5.2 + DRF + PostgreSQL. The trading engine is a set of management
commands that share the same ORM and code path as the backtester.

Status: **Phase 4 (local LLM roles, RiskGuard) in progress**. See `STRATEGY.md`. Strategy, ML, agent, execution and the dashboard
are not implemented yet.

## Setup

```bash
cp .env.example .env          # then fill in WALLEX_API_KEY and passwords
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

Or with Docker (PostgreSQL + web):

```bash
docker compose up --build
```

## Tests

```bash
pytest
```

Covers the Wallex client (auth header, retries, backoff, rate limiting) and the
market policy (blocklist parsing, default-deny allowlist, startup check).

## Probe the Wallex API (read-only)

```bash
python scripts/probe_api.py
python scripts/probe_api.py --symbol BTCTMN --resolution 60 --days 2 --out scratchpad/probe.txt
```

Sends GET requests only. Output is redacted by default (identifiers everywhere,
balances and ids on account/prop endpoints); `--no-redact` is for a local look
only. Endpoints marked UNVERIFIED are not in the Swagger file. Real public
fixtures are refreshed with `python scripts/capture_fixtures.py`.

## Market data (Phase 1)

```bash
python manage.py sync_markets                       # spot markets, precision, min order, market-order flag, policy flags
python manage.py sync_orderbook                     # top-of-book snapshot per allowlisted market (GET /v1/depth)
python manage.py download_history --days 30         # 1h candles for allowed markets (idempotent)
python manage.py download_history --symbol BTCTMN --start 2026-10-01T00:00:00+00:00 --end 2026-10-08T00:00:00+00:00
python manage.py build_universe                     # ELIGIBLE / REJECTED with reasons
```

Every command passes the market policy before it makes a request. Blocked or
non-allowlisted symbols are refused.

Responses are validated with Pydantic models in `exchange/schemas.py`. The test
fixtures in `tests/fixtures/wallex/` are real captures (see the README there).
Fields the Swagger does not document are not used.

## Orderbook recorder (Phase 2)

Order book history cannot be downloaded later, so it is recorded from day one
(GET only, public endpoints, no API key sent):

```bash
nohup scripts/run_recorder.sh >/dev/null 2>&1 &   # restarts after crashes, keeps the Mac awake
.venv/bin/python manage.py recorder_status --hours 1
scripts/stop_recorder.sh
```

Every 10 s it stores the top 20 levels of `/v1/depth` for the 21 data-universe
symbols (11 TMN markets and their USDT pairs). Each symbol's public trades are
polled every 30 s and the bid/ask of every market every 60 s. Every request is
logged as an event with its latency, the server clock offset and any error. Files:
`data/orderbook/<stream>/YYYY-MM-DD/HH.parquet` (UTC).

## Data, backtest and evidence (Phase 2)

```bash
.venv/bin/python manage.py download_history --resolution 1 --days 180   # 21 symbols, ~25 min
.venv/bin/python manage.py export_candles                                 # data/candles/1m/*.parquet + data/MANIFEST.json
.venv/bin/python manage.py edge_study                                     # does any signal beat random entries after costs?
.venv/bin/python manage.py window_study                                   # rolling 3-day windows, selection/holdout, fee stress
.venv/bin/python manage.py run_backtest --start 2026-10-19T00:00:00Z --end 2026-10-22T00:00:00Z
```

The backtester reads only the committed `data/` and `config/`, so no network is needed. The
strategy code (`strategy/`) and the risk caps (`risk/limits.py`) are the same ones the live
engine will use. Results and their interpretation are in `docs/RESEARCH_P2.md`.

## ML and the ablation (Phase 3)

```bash
brew install libomp                                    # macOS only (LightGBM)
.venv/bin/python manage.py p3_experiment ml_targets    # which ML target has skill net of costs (selection only)
.venv/bin/python manage.py p3_experiment rules_plus_ml # rules vs rules + ML on 3-day windows
.venv/bin/python manage.py p3_experiment rules_plus_ml --final   # holdout, logged once per model
```

Every experiment has at most 30 configurations, reports a 3-row fee table and is appended to
`reports/experiments.jsonl`. Findings: `docs/RESEARCH_P3.md`.

## AI: local, pinned, reproducible (Phase 4)

The AI roles (Regime/Risk Officer, Trade Agent) run on a **local open-weight model** through Ollama:
zero cost, offline, pinned by name and digest in `config/llm.toml` (`qwen3:8b`), JSON-schema outputs,
temperature 0, fixed seed. Every answer is cached in `data/llm_cache/` (committed), so the AI's
decisions can be replayed without any model installed. Rationale and evidence: `STRATEGY.md` §2.

```bash
make reproduce            # replay the LLM ablation from data/ + data/llm_cache/ and check every metric
make llm-setup            # install Ollama + the pinned model and run a health check (needed only to regenerate)
make reproduce-llm-live   # regenerate every LLM answer with the pinned model into a fresh cache (hours)
.venv/bin/python manage.py p4_experiment benchmark   # 20 calls: latency, JSON validity, determinism
```

Optional free-tier fallbacks (Groq, Gemini Flash) for shadow/live only: set `GROQ_API_KEY` /
`GEMINI_API_KEY` in `.env`. They are never used by the backtest.

## Market policy (default-deny)

- `blocked_markets.txt`: markets that must never be traded (BASE/QUOTE, one per line).
- `config/trading.toml`: `markets.allowlist`. The default-deny rule is enforced in code. An empty list means nothing is tradable.
- `config/trading.toml`: `data.symbols`. Read-only data universe (the USDT pairs for fair value). Never traded; a blocked symbol here stops the policy from loading.
- `manage.py check` runs `exchange.E001` (invalid policy file, error) and `exchange.W001` (empty allowlist, warning).

## Safety

- Live trading requires both `--mode live` and `LIVE_TRADING_CONFIRMED=true`. The default is `false`.
- Secrets live only in `.env`, which is git-ignored.
