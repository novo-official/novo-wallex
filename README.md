# NOVO Team: agentic AI trading engine for Wallex

> **خلاصه:** موتور معاملاتی تیم نوو برای چالش والکس در هکاتون Starkoch. بازار اسپات، فقط خرید، و بدون دخالت انسان.
> - **تصمیم‌گیری:** مدل ML بازارها را رتبه‌بندی می‌کند، دو نقش مدل زبانی محلی (افسر ریسک و عامل معامله) تأیید می‌کنند و RiskGuard حرف آخر را می‌زند.
> - **یک مسیر کد:** شادو و زنده یک کد مشترک دارند و همه چیز در DecisionLog ثبت می‌شود.
> - **نتیجه اجرای زنده (۱ آبان، ۱۱:۳۸ تا ۲۳:۰۰):** ۵٬۰۰۰٬۰۰۰ تومان به ۴٬۹۹۴٬۵۵۵ تومان رسید (−۰٫۱۰۹٪)، با ۲ معامله روی ETHTMN. بررسی پایانی تومانی بودن قبول شد.
> - **مستندات:** معماری و استراتژی در `STRATEGY.md`، راه‌اندازی زنده و خرابی‌ها در `RUNBOOK.md`، و ویدیوی دمو در `DEMO_SCRIPT.md`.

Django 5.2 + DRF + PostgreSQL, Python 3.12, LightGBM, a local LLM (Ollama, `qwen3:8b` pinned by digest) and a
Next.js dashboard (Persian, RTL). The backtester, shadow and live share the same strategy, risk and feature code.

| document | contents |
|---|---|
| `STRATEGY.md` | architecture (Mermaid), trading logic, AI role and ablation, buy/sell rules, risk, results, lessons |
| `RUNBOOK.md` | go-live checklist, smoke test, manual challenge end, failure playbook |
| `DEMO_SCRIPT.md` | 5-minute demo with Persian narration |
| `docs/RESEARCH_P2.md` … `P4.md` | data, costs, rules, ML, LLM roles: every experiment and its numbers |
| `reports/experiments.jsonl` | append-only log of every experiment run (configs tried, holdout looks) |
| `reports/decision_logs/` | exported decision logs (shadow; live once it runs) |

## Live result (prop account, 2026-10-23)

| | |
|---|---|
| period | 11:38 → 23:00 Tehran (11.4 h); live-v1 (`e285a7f`), then live-v1.1 by a controlled restart at 12:33 (`d6cb336`, tag `live-v1.1-deploy`) |
| start → end | 5,000,000 TMN → **4,989,101 TMN + 0.0000085 ETH dust (≈ 5,455 TMN) = 4,994,555 TMN** |
| return | **−0.109%** with the dust at the bid (TMN only: −0.218%) |
| final check | `FINAL ok: true` at 23:00:02: 100% TMN excluding dust, 99.89% including it; dust is below the 50,000 TMN minimum order |
| trades | 2 round trips, both ETHTMN: −993 TMN (2 h max-hold exit) and ≈ −3,250 TMN (stop) |
| pre-live tests | smoke tests + two real BTCTMN round trips: −873 TMN |
| AI decisions | 6 Officer decisions (all NORMAL; BTC/ETH avoided in 2 slots); 3 Trade Agent reviews, all approved with a smaller size; 4 candidates rejected by the Officer's avoid list |
| operations | 0 crashes; 4 automatic entry halts, all resumed by themselves (the Mac slept on battery and with the lid closed; one DNS drop on the phone hotspot) |

Decision logs: `reports/decision_logs/live_decisions.{jsonl,csv}` (every candidate, LLM decision, RiskGuard verdict,
order, fill, exit and the final check). What the live run taught us is in `STRATEGY.md` §5.

## 1. Setup

```bash
cp .env.example .env                       # WALLEX_API_KEY, POSTGRES_PASSWORD, CHALLENGE_END (see RUNBOOK §3)
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt ruff
brew install libomp postgresql@16           # macOS: LightGBM runtime, local Postgres (role/db "wallex")
.venv/bin/python manage.py migrate
make test                                   # ~250 tests
make llm-setup                              # Ollama + the pinned model (only for shadow/live and LLM regeneration)
```

Docker (`docker compose up --build`) exists but was not used for the runs.

## 2. Data

Everything the backtests need is committed: 180 days of 1m candles for 21 symbols (`data/candles/1m/`,
sha256 in `data/MANIFEST.json`), market rules, frozen slippage (`config/slippage.json`) and every LLM answer
(`data/llm_cache/`).

```bash
.venv/bin/python manage.py import_candles                    # verify sha256, load into the DB
.venv/bin/python manage.py download_history --resolution 1 --days 180 --resume   # refresh from Wallex (GET only)
nohup scripts/run_recorder.sh >/dev/null 2>&1 &              # order book recorder (not committed: data/orderbook/)
```

## 3. Backtests and experiments

```bash
.venv/bin/python scripts/ai_activity_fix_check.py            # live config, 3 variants, last 14 days, measured fees
.venv/bin/python scripts/backtest_ai_activity.py             # live config, 14 + 7 days, the 3-row fee table
.venv/bin/python manage.py window_study                      # rolling 3-day windows (trend rules)
.venv/bin/python manage.py p3_experiment <name> [--final]    # ML experiments (docs/RESEARCH_P3.md)
.venv/bin/python manage.py p4_experiment ablation --llm replay [--final]   # LLM ablation from the cache
make reproduce                                               # replay the LLM ablation: no model, no network
```

Overfitting guard: at most 30 configurations per experiment, selection windows only, the holdout only with
`--final`, every run appended to `reports/experiments.jsonl`.

## 4. Shadow, live and the dashboard

```bash
scripts/run_engine.sh shadow &              # simulated fills on the live public book; no API key used
make preflight                              # live readiness (RUNBOOK §1)
make go-live                                # preflight -> smoke test (typed YES) -> GO LIVE -> tag live-v1 -> start
make api                                    # read-only DRF API on 127.0.0.1:8000
make dashboard-install && make dashboard    # http://localhost:3000  (Overview, Decision Timeline; ?mode=shadow|live)
.venv/bin/python scripts/export_decisions.py --mode shadow   # reports/decision_logs/shadow_decisions.{jsonl,csv}
```

Files the engine writes: `journal/<mode>/decisions-YYYYMMDD.jsonl` (the DecisionLog, committed),
`logs/engine_state_<mode>.json`, `logs/heartbeat_<mode>.txt`, `logs/llm_journal.jsonl` (not committed).

## 5. Safety

- **Live needs all of:** `--mode live`, `LIVE_TRADING_CONFIRMED=true`, a passing preflight and the team's
  explicit go-ahead (`make go-live` asks for it twice).
- **Market policy:** default-deny allowlist (`config/trading.toml`) plus the organizer blocklist
  (`blocked_markets.txt`, OP/TMN), checked at startup and immediately before every order.
- **RiskGuard:** has the final word over every entry (STRATEGY.md §4); the LLM can only reduce risk.
- **Secrets:** only in `.env` (git-ignored); the key is never logged, and shadow does not send it.
- **Probes:** probe scripts are GET only and redact identifiers and balances.
