# CLAUDE.md: NOVO Team, Wallex Agentic Trading

Read this first in every session. Keep it under ~250 lines and update it at the end of every phase.
Full rules: `docs/CHALLENGE.md`. Ground truth for the API: `docs/wallex_spot_swagger.json` + probe output.

## 1. The challenge (condensed)

- Starkoch "Agentic AI" hackathon, Wallex challenge. Build an AI trading engine that trades Wallex **spot**
  with no human intervention for **3 days**, on a prop sub-account of **5,000,000 TMN** (no leverage, no withdrawals).
- Return = (final net value − 5M) / 5M. **Must be realized in TMN at the end.** Losses cost the team nothing;
  net profit goes to the team. Top team: 80M TMN prize.
- Prohibited (disqualification): manual trades, self-trading, coordinated accounts, wash trading, blocked markets.
- Allowed: all spot markets except `blocked_markets.txt` (`OP/TMN` → `OPTMN`).
- Deliverables: ~5 min demo video, the repo with exactly the code that traded, a `.md` (architecture, AI role,
  buy/sell rules, risk, re-run guide), **a decision log for every trade**. Judges may ask for a re-run.
- Judging 300 pts. Trading 150: return rank 90 (**losers get 0**), drawdown 30, activity 15, stability 15.
  Technical 150: strategy logic 40, real AI role 35, risk mgmt 30, re-runnable 25, backtest + shadow 20.
- **Objective:** high probability of a small positive 3-day return with low drawdown, plus strong evidence.
  Not max backtest profit. Taker round trip ≈ 0.5–0.7%: no naive scalping. A boring strategy positive in 70%
  of 3-day windows beats a brilliant one positive in 40%. Drop modules with no edge and say so.

## 2. Environment

- Runs locally in Iran. `api.wallex.ir` is reachable (GET verified 2026-10-22). `api.wallex.com` (the Swagger
  host) does **not resolve** from this machine: DNS failure, not a block. If Wallex becomes unreachable, tell the
  user (VPN split tunnel); do not work around it.
- LightGBM needs `brew install libomp` on macOS (Docker installs `libgomp1`).
- **LLM = local Ollama** (`brew services start ollama`), model `qwen3:8b` pinned by digest in `config/llm.toml`.
  No paid API (user decision). 16 GB M4: ~10–20 s per call; heavy apps slow it (swap).
- Python 3.12 in `.venv/` (`python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt ruff`).
  `venv/` (Python 3.14) is an old local env; ignore it.
- Postgres: Homebrew Postgres on localhost:5432, role/db `wallex` (password in `.env`). Docker compose exists but is
  untested and would clash on port 5432.
- `.env` holds `WALLEX_API_KEY` (46 chars, works since 2026-10-23 on `x-api-key`; all balances were 0 then).
  Never print, log or commit it. Shadow deliberately runs without the key.
- Background jobs: start detached (`subprocess.Popen(..., start_new_session=True)`); jobs tied to the agent
  session were all stopped together on 2026-10-22 13:41 UTC.
- Optional free LLM fallbacks (shadow/live only): `GROQ_API_KEY`, `GEMINI_API_KEY` in `.env`.
- Shell: `rm` is aliased to interactive; use `/bin/rm -f` in scripts.

## 3. Working rules

- **Network:** until Phase 5 is approved, GET only. **Never POST/DELETE to Wallex** unless the user approves that
  specific action in chat. Probe/capture scripts send GET only.
- **Secrets:** never print/log/commit `.env` or the key. The repo will be **public**. Probe output is redacted by
  default (`exchange/redact.py`); `scratchpad/` is gitignored; private-endpoint captures never go into fixtures.
- **Branch:** work directly on `main` (user decision 2026-10-22). Commit locally; push only when asked.
- **Overfitting guard:** at most 30 configs per experiment (`backtest/experiment.py` enforces it); choose on
  selection windows only; holdout only with `--final`, once per model; every run is appended to
  `reports/experiments.jsonl` (never edit past entries). The Sep–Oct holdout was seen 11× before the guard existed;
  say so in reports. The truly untouched test is data after 2026-10-22 (shadow).
- **Recorder output never goes into git** (`data/orderbook/` is ignored); tests use `tests/fixtures/orderbook_sample/`.
- **Phase checkpoints:** after each phase, stop and report **in Persian** (built, evidence/numbers, how to verify,
  exact commands, decisions needed). Update this file. Wait for approval.
- Code, comments and docs in English; docs get a short Persian summary at the top. Talk to the user in Persian.
- Ruff (120 cols) and pytest green on every commit. Decimal for all money/quantities. Never invent API fields:
  anything not in Swagger or a probe capture is UNVERIFIED and blocked from live use.
- Make design decisions yourself and justify them with evidence; escalate only decisions that are the user's.

## 4. Hard constraints (enforce in code + tests)

- **Final value in TMN:** no new entries in the last 2h; flatten everything to TMN in the last 60 min (limit at best
  price, then a crossing order after a timeout); verify ≥ 99% of final value is TMN.
  Note: MARKET orders exist on only 7 markets (see §7), so the fallback must be an aggressive LIMIT elsewhere.
- **Challenge clock:** `CHALLENGE_START` / `CHALLENGE_END` in config, Asia/Tehran.
- **Market policy:** default-deny allowlist; blocklist checked at startup and immediately before every order.
- **Self-trade guard:** cancel our own resting opposite orders on a market before sending a crossing order.
- **Prop limits:** stay far above `liquidity_limit`.
- **Numbers:** Decimal everywhere; respect price/amount decimals, `minQty` and min order value.
- **Live mode** requires all of: `--mode live`, `LIVE_TRADING_CONFIRMED=true`, a passing `preflight`, and the
  user's explicit go-ahead in chat.
- **RiskGuard (P4, pure, final word):** ≤ 20% equity per position, ≤ 60% total exposure, one position per symbol,
  no averaging down, daily loss −3% → no new entries until next day, kill switch −6% → flatten + halt, buffer above
  `liquidity_limit`, min order checks, end-of-challenge rules, self-trade guard. The LLM can never exceed it.

## 5. Defaults until the organizers confirm

- **Objective (user, 2026-10-22):** maximize P(3-day return > +0.2%) s.t. p10 drawdown ≥ −3.5% and **≥ 1 round
  trip/day**; zero-trade windows are failures (minimize them); report mean/median, DD and worst window too.
- **Every report has the 3-row fee table:** low 0.15/0.20, base 0.25/0.35, stress ×1.5 (`config/strategy.toml`).

- Fees: TMN maker 0.25% / taker 0.35%; USDT maker 0.15% / taker 0.20%. Always also run fee stress ×1.5.
- Strategy min order: 500,000 TMN / 5 USDT (exchange minimum is 50,000 TMN / 1 USDT, per `/v1/markets`).
- Balance access behind a `BalanceProvider` abstraction (no balance endpoint verified yet).

## 6. Architecture map

| Path | Responsibility |
|---|---|
| `config/settings.py` | Django settings from env; Wallex client settings; policy file paths; `LIVE_TRADING_CONFIRMED` |
| `config/trading.toml` | allowlist, universe thresholds, history chunking |
| `blocked_markets.txt` | organizer blocklist (BASE/QUOTE) |
| `exchange/client.py` | `WallexClient`: GET only, `x-api-key`, timeout, backoff on 429/5xx/network, rate limit |
| `exchange/schemas.py` | Pydantic models (Decimal): hector markets, `/v1/markets` rules, depth, trades, UDF |
| `exchange/symbols.py` | fail-closed blocklist loader, `to_api_symbol`, default-deny `MarketPolicy.check()` |
| `exchange/checks.py` | system checks `exchange.E001` (bad policy) / `W001` (empty allowlist) |
| `exchange/redact.py` | redaction of identifiers/balances for probe output and logs |
| `exchange/factory.py` | builds client and policy from settings |
| `exchange/symbols.py` (data) | `[data] symbols` = read-only data universe (USDT pairs); `check_data()` before every data request |
| `market/models.py` | `Market` (precision, min_order_value, min_qty, market_orders_enabled, flags), `Candle`, `OrderbookSnapshot` |
| `market/services.py` | `sync_markets`, `sync_orderbook` (per-symbol `/v1/depth`), `download_candles`, `build_universe` |
| `market/history.py` | pure: resolution maths, non-overlapping windows, bar parsing + OHLC sanity |
| `market/universe.py` | pure `evaluate()` with a reason per rejection |
| `market/parquet_store.py` | hourly Parquet store (zstd, decimal128(38,18)), atomic part files, hourly compaction |
| `market/recorder.py` | `OrderbookRecorder`: depth top-20 every 10 s, trades round-robin every 30 s, tickers 60 s, events |
| `market/recorder_status.py` | coverage, error rate, latency, clock offset, spread per symbol |
| `market/candles_io.py` | candle Parquet export/import, `data/MANIFEST.json` (sha256), `export_candles`/`import_candles` |
| `strategy/` | pure (no Django): `data.py` (load/resample, 5m clock on close times), `features.py` (causal, as-of joins), `base.py` (`Strategy.on_cycle(ctx, portfolio) -> list[Intent]`), `modules/trend.py`, `modules/mean_reversion.py`, `composite.py` (fair-value filter), `factory.py` |
| `risk/limits.py` | pure caps: 20%/position, 60% total, 0.5% risk/trade, 2% stop cap, 500k min order, −3% day, −6% kill |
| `backtest/` | `engine.py` (event-driven, conservative fills), `costs.py` (fees + slippage from recorded book), `event_study.py`, `windows.py` (3-day study), `pipeline.py`, `report.py`; commands `measure_costs`, `edge_study`, `window_study`, `run_backtest` |
| `backtest/experiment.py` | `run_experiment`: ≤ 30 variants, objective, fee table, holdout guard, logging |
| `backtest/experiments.py` | append-only experiment log `reports/experiments.jsonl` |
| `ml/` | P3a: `features.py` (41 causal features), `labels.py` (triple barrier), `model.py` (purged walk-forward, isotonic), `study.py`; P3b: `perbar.py` (per-bar E[net] models, embargo, SHAP), `p3_experiments.py` + `p3_experiment <name> [--final]` |
| `strategy/modules/ml_signal.py`, `usdt_allocation.py` | ML-only module (E[net] > threshold, confidence sizing); USDT regime allocation |
| `market/orderbook_features.py` | spread, imbalance, depth, microprice from a snapshot (logged later, not trained on) |
| `risk/guard.py` | **RiskGuard** (pure, final word): policy, kill/daily/end, risk_mode, one position/symbol, self-trade, LLM clamps, caps, liquidity buffer; the engine routes every entry through it |
| `agent/llm.py` | provider-neutral client: `OllamaProvider` (pinned digest, `format` schema, temp 0, seed), `OpenAICompatibleProvider` (Groq/Gemini, backoff), `ReplayProvider`; cache key = identity + request; ≤ 3 tools; retry → fallback; budget; journal |
| `config/llm.toml` | pinned local model + digest, options, officer cadence (1 h live / 4 h replay), fallbacks, news sources |
| `agent/news.py` | news interface + stub: free RSS feeds, local-model digest (shadow only) |
| `Makefile`, `scripts/check_reproduce.py`, `scripts/llm_check.py` | `make reproduce` (no model needed), `make llm-setup`, `make reproduce-llm-live` |
| `agent/roles.py`, `agent/prompts/` | Regime/Risk Officer (hourly, market-only, ceiling, fallback OFF), Trade Agent (core entries, fallback reject), `AgentLayer` (engine hook), blind replay context (no dates/prices) |
| `agent/tools.py` | WallexTools adapter: live GET / recorded Parquet / unavailable (historical) |
| `agent/replay.py`, `agent/experiments.py` | client modes (replay / live-local / live), resumable officer precompute, benchmark, ablation; `p4_experiment benchmark/plan/precompute/ablation [--llm] [--final]` |
| `agent/context.py` | context builder with pluggable `NewsFeed` (news role for P5+) |
| `models/` | `ml_net_h48/` (P3b final, not live), `aligned_3d/` (P3a) |
| `config/strategy.toml` | fees, objective, tradable markets, windows, canonical `[strategy]` (rules + activity), `[grid]` (≤ 30), `[shadow_variants]` |
| `config/slippage.json` | frozen slippage per symbol (from `measure_costs`); part of the config hash |
| `scripts/run_recorder.sh` | supervisor: restart on crash, `caffeinate -is`, `logs/recorder.pid` / `logs/recorder.log` |
| `scripts/probe_api.py` | GET-only probe, both hosts, redacted by default, `--save-dir` |
| `scripts/capture_fixtures.py` | GET-only, policy-checked capture of public fixtures |
| `tests/fixtures/wallex/` | real captures only (see its README) |

| `strategy/modules/ai_activity.py` | **the live module**: ML-ranked candidates per 60 min slot, one approved entry per slot |
| `execution/` | `engine.py` (one loop for shadow + live), `broker.py` (Live/Shadow), `feed.py`, `journal.py` (DecisionLog), `runner.py`, `preflight.py`, `smoke.py`; commands `run_engine`, `preflight`, `smoke_test` |
| `config/live.toml` | live/shadow config: challenge, risk profile, engine, `[ai_activity]`, `[llm]` |
| `journal/<mode>/` | DecisionLog (committed); `reports/decision_logs/` = exports (`scripts/export_decisions.py`) |
| `api/`, `dashboard/` | read-only DRF API (`/api/overview`, `/api/decisions`) + Next.js 15 Persian RTL dashboard |
| `scripts/go_live.sh` | `make go-live`: freeze check → preflight → smoke test → GO LIVE → tag → start live |
| `README.md`, `STRATEGY.md`, `RUNBOOK.md`, `DEMO_SCRIPT.md`, `CHANGELOG.md` | deliverable docs |

## 7. Wallex API facts (verified unless marked)

- Swagger host `api.wallex.com` (unresolvable here); everything below verified on `api.wallex.ir`.
- `GET /hector/web/v1/markets` (Swagger): symbol, base/quote_asset, is_spot, price, price_precision,
  amount_precision, volume_24h, quote_volume_24h. 469 markets; ~152 have "" price or null volume → None.
- `GET /v1/markets` (probe-verified, not in Swagger): 373 symbols. `stepSize`/`tickSize` are **decimal places**
  (equal amount/price_precision for all 373). `minNotional` 50,000 for all TMN markets, 1 for all USDT markets.
  `minQty` per market. **`isMarketTypeEnable` true only for BTCTMN, ETHTMN, USDTTMN, XAUTTMN, BTCUSDT, ETHUSDT,
  XAUTUSDT.** `stats`: bid/ask/last, 24h volumes ("" when empty). `isZeroFee` only USDSUSDT.
- `GET /v1/depth?symbol=` (probe-verified): `{success, message, result: {ask, bid}}`, 50 levels each, asks ascending,
  bids descending. price/quantity are JSON numbers, **sum is a string = price × quantity of that level** (not cumulative).
- `GET /depth/all` (Swagger): HTTP 503 text/html on every probe. Not used.
- `GET /v1/trades?symbol=` (probe-verified): `result.latestTrades[50]`, newest first; strings for price/quantity/sum,
  `isBuyOrder` bool (assumed taker side, UNVERIFIED), ISO timestamp. 50 BTCTMN trades ≈ 13 min.
- `GET /v1/udf/history?symbol=&resolution=&from=&to=`: `t` seconds (bar open), o/h/l/c/v strings, zero-volume bars
  for quiet minutes. Valid resolutions only `1, 15, 60, 240, 720, 1D`; **15 returns 1m bars, 240/720 return 1h bars**;
  `60` returns only the latest 696 bars; a 1m window wider than ~34 h **silently returns one bar** (we use 1440-bar chunks
  and a truncation guard). 1m history goes back ≥ 500 days.
- `GET /v1/currencies/stats`: 422, requires a `key` parameter. Unused.
- Account, verified with the key 2026-10-23: `/v1/account/balances` → `result.balances.{ASSET}` with `value`,
  `locked` (strings; whether `value` includes `locked` is UNVERIFIED until the smoke test), 283 assets.
  `/v1/account/openOrders` → `result.orders[]`. `/v1/account/fee` → per symbol `makerFeeRate`/`takerFeeRate`:
  **TMN 0.25% / 0.30%, USDT 0.10% / 0.13%**. `/v1/prop/user/packages/history` → 401 code 1201 «ابتدا وارد شوید»
  (needs a web login, not the API key).
- **Prop account, smoke test 2026-10-23 (real orders):** LIMIT place/read/cancel on BTCTMN works (status NEW →
  CANCELED, `raw_status` = status). `value` = total (includes `locked`): available = value − locked. **MARKET
  orders are refused (422 «امکان ثبت سفارش قیمت بازار در سکو وجود ندارد»)**: forced sells are aggressive LIMITs.
  **USDTTMN orders are refused (422)** on this account. Buy fee is taken in the coin (0.30% taker, balance
  floored to 8 dp); sell fee in TMN (0.30%). A LIMIT at the best bid fills at once. Round trip cost 425 TMN on 55k.
- UDF sends no bars for minutes without trades. At night even USDTTMN can go 6 h without a trade (book still
  live): the feed extends quiet symbols flat and the engine refuses entries on a market quiet > 120 min.
- Orders (Swagger, P5 only): `POST /v1/account/orders` {symbol, side BUY/SELL, type LIMIT/MARKET/STOP_LIMIT/STOP_MARKET,
  price, quantity, `stop_Price`, client_id}; response uses `stopPrice`. Cancel: `DELETE /v1/account/orders`
  {clientOrderId} or `DELETE /v1/account/orders/{client_id}`. OrderResource: status (enum undocumented), executedQty,
  executedPrice, fee, fills[] with fee/feeAsset/maker/takerFeeCoefficient.
- Open questions to organizers: fees, min order, prop balance endpoint, official MCP endpoint, exact start/end time.

## 8. Commands

```bash
.venv/bin/python -m pytest -q                 # tests (needs local Postgres)
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python manage.py migrate
.venv/bin/python manage.py check              # market policy system checks
.venv/bin/python scripts/probe_api.py --out scratchpad/probe.txt --save-dir scratchpad/probe_json
.venv/bin/python scripts/capture_fixtures.py  # refresh public fixtures
.venv/bin/python manage.py sync_markets
.venv/bin/python manage.py sync_orderbook
.venv/bin/python manage.py download_history --resolution 1 --days 180   # whole data universe; --resume to top up
.venv/bin/python manage.py export_candles                                 # DB -> data/candles + MANIFEST + market_rules
.venv/bin/python manage.py import_candles                                 # verify sha256, load into the DB
.venv/bin/python manage.py measure_costs                                  # refresh config/slippage.json from the recorder
.venv/bin/python manage.py edge_study                                     # event study -> reports/edge-*/
.venv/bin/python manage.py window_study                                   # 3-day windows -> reports/windows-*/
.venv/bin/python manage.py run_backtest --start ... --end ...             # one run with trades + decision log
.venv/bin/python manage.py p3_experiment <name> [--final]                 # P3 experiments (see docs/RESEARCH_P3.md)
.venv/bin/python manage.py p4_experiment benchmark                       # local model: latency, JSON validity, determinism
.venv/bin/python manage.py p4_experiment plan --final --seconds-per-call 15  # officer calls cached vs to compute, runtime
.venv/bin/python manage.py p4_experiment precompute --final               # resumable; progress in logs/p4_precompute_progress.jsonl
.venv/bin/python manage.py p4_experiment ablation --llm live-local [--final]
make reproduce                                                           # replay from the committed cache, no model
.venv/bin/python manage.py build_universe
nohup scripts/run_recorder.sh >/dev/null 2>&1 &   # start the recorder (survives the terminal)
scripts/stop_recorder.sh                          # stop it cleanly
.venv/bin/python manage.py recorder_status --hours 1
```

Recorder data: `data/orderbook/{depth,trades,tickers,events}/YYYY-MM-DD/HH.parquet` (UTC hours). Part files of the
current hour are gitignored. Trades: consecutive polls are de-duplicated by multiset difference; after a restart the
first poll may repeat trades already written, so loaders de-duplicate on (symbol, trade_time, price, quantity, side)
across adjacent polls. ~2.8 req/s, no rate-limit headers seen; a 429 is retried with backoff.

## 9. Phase status

- **P0 done** (merged to `main`, PR #1): Django/DRF scaffold, read-only client, market policy.
- **P1 done** (branch `claude/bold-dirac-pqnp9u`, not yet merged to `main`): markets/orderbook/candles/universe.
- **First actions done (2026-10-22):** this file, `docs/CHALLENGE.md`, repo hygiene (scratchpad untracked, probe
  redaction), probe of both hosts + 6 candidate endpoints, real depth/rules/trades fixtures, `/v1/markets` rules
  stored on `Market`, `sync_orderbook` switched to `/v1/depth`. 95 tests green.
- **P2 done (2026-10-22), awaiting review.** Recorder running since 08:51 UTC. 180 days of 1m candles for 21
  symbols in `data/` (64 MB, sha256 manifest). Backtester, event study, 3-day window study. Findings
  (`docs/RESEARCH_P2.md`): default fees kill short-horizon signals; trend edge only on BTC/ETH/USDT-TMN; chosen
  params (#22: 1h uptrend + 5m breakout, fixed 2% stop, 6% target, no trail) are about break-even per window
  (+0.12% sel / +0.06% holdout, worst DD −3.9%); the 60%-positive constraint is not met by any combination;
  results depend strongly on the real fee. USDTTMN +76% in 180 days: always show buy-and-hold benchmarks.
- **P3 done (2026-10-22), awaiting review** (`docs/RESEARCH_P3.md`, `docs/RESEARCH_P3a_filter.md`). Per-bar ML has real
  ranking skill (IC 0.12 at 1h) but the predictable move < 0.6% round trip; ML-only, ML gate, USDT allocation (raw and
  detrended) and SOL/XRP are negative or inactive; rules + ML adds activity but not holdout return. Recommendation:
  live = trend rules (USDT/BTC/ETH, maker entries) only. 32 experiments / 1,684 configs / 17 holdout evaluations.
  Fixed: fair-value step reset module sizes to 1.0 (affected P3b ML sizing only).
- **P4 in progress (2026-10-22)** (`docs/RESEARCH_P4.md`, `STRATEGY.md`). No paid LLM: local `qwen3:8b` via Ollama,
  pinned digest. Canonical = trend rules (activity ≥ 1/day); activity-quota variant shadow-only. RiskGuard wired.
  Benchmark: 100% valid JSON, 100% deterministic decisions, ~11 s/call. Officer precompute (1,008 calls, 4 h cadence)
  then the ablation (39 selection + 17 holdout non-overlapping windows). News: interface + stub only.
- **P5/P6/P7 (2026-10-23):** engine, brokers, reconcile, preflight, smoke test, `make go-live`; trend OFF in live,
  AI activity only (fix check: (a) −1.34%, (b) E[net] > 0 +0.03% at 0.14 trades/day, (c) no trades); dashboard;
  docs. Shadow running (G2 from 2026-10-23 05:26 Tehran, commit 861307c). Blocked on the prop account: the key is the main
  account's (all balances 0); CHALLENGE_END to be set by hand. Live config = (a) with both LLM roles (user decision 2026-10-23).
- Triage if short on time: keep backtester + 3-day study, RiskGuard, execution + reconcile + end flatten,
  DecisionLog, LLM agent with ablation, docs + reproduce. Cut first: triarb, ML polish, dashboard polish, Telegram.
