# Wallex Algorithmic Trading System

Spot-only, long-only algorithmic trading for the Wallex hackathon challenge.
Django 5.2 + DRF + PostgreSQL. The trading engine is a set of management
commands that share the same ORM and code path as the backtester.

Status: **Phase 0 (scaffold)**. Market data, strategy, ML, agent, execution
and the dashboard are not implemented yet.

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

Sends GET requests only and prints the raw responses. Use it to confirm real
response shapes before typed models are written.

## Market policy (default-deny)

- `blocked_markets.txt`: markets that must never be traded (BASE/QUOTE, one per line).
- `config/trading.toml`: `markets.allowlist`. Empty by default, so nothing is tradable.
- `manage.py check` runs `exchange.E001` (invalid policy file, error) and `exchange.W001` (empty allowlist, warning).

## Safety

- Live trading requires both `--mode live` and `LIVE_TRADING_CONFIRMED=true`. The default is `false`.
- Secrets live only in `.env`, which is git-ignored.
