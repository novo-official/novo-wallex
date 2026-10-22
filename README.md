# Wallex Algorithmic Trading System

Spot-only, long-only algorithmic trading for the Wallex hackathon challenge.
Django 5.2 + DRF + PostgreSQL. The trading engine is a set of management
commands that share the same ORM and code path as the backtester.

Status: **Phase 1 (market data)**. Strategy, ML, agent, execution and the dashboard
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

## Market policy (default-deny)

- `blocked_markets.txt`: markets that must never be traded (BASE/QUOTE, one per line).
- `config/trading.toml`: `markets.allowlist`. The default-deny rule is enforced in code. An empty list means nothing is tradable.
- `manage.py check` runs `exchange.E001` (invalid policy file, error) and `exchange.W001` (empty allowlist, warning).

## Safety

- Live trading requires both `--mode live` and `LIVE_TRADING_CONFIRMED=true`. The default is `false`.
- Secrets live only in `.env`, which is git-ignored.
