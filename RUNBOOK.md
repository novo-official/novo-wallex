# Runbook: shadow, go-live, failures

> **خلاصه:**
> - **راه‌اندازی زنده با یک دستور:** `make go-live`. این دستور به ترتیب بررسی فریز کد، preflight، تست دود (با تأیید تایپی YES) و تأیید نهایی «GO LIVE» را انجام می‌دهد، سپس تگ `live-v1` را می‌زند و push می‌کند و موتور زنده را جدا از ترمینال اجرا می‌کند. اندازه‌ها در ۲ ساعت اول نصف است.
> - **زمان پایان چالش:** اگر endpoint پراپ در دسترس نباشد، `CHALLENGE_END` را دستی در `.env` بگذارید. بدون آن preflight رد می‌شود.
> - **بخش ۵:** برای هر خرابی (قطعی اینترنت/DNS، خطاهای پیاپی، سفارش ناشناس، کلید توقف، کرش، تمام شدن زمان) راهنمای اقدام آمده است.

## 1. Before go-live (once)

| check | how |
|---|---|
| the prop sub-account's API key in `.env` (`WALLEX_API_KEY`) | `make preflight` → `api key PASS`, `balances` shows ~5,000,000 TMN |
| challenge end time | `CHALLENGE_END` in `.env` (see §3) → `challenge end PASS` |
| local LLM running, pinned digest | `brew services start ollama`; `make llm-check` |
| recorder and shadow running | `cat logs/heartbeat_shadow.txt` (≤ 1 min old); dashboard `?mode=shadow` |
| tests green, trading code committed | `make check`; `git status` clean for `execution strategy risk agent config models` |
| chosen live config | `config/live.toml` `[ai_activity]` (variant (b): uncomment `min_edge = 0.0`), commit it |

## 2. Go-live (≤ 15 minutes)

```bash
make go-live
```

`scripts/go_live.sh`, step by step (it stops at the first failure):

1. **Code freeze:** no uncommitted change in the trading code; the tag `live-v1` must not point elsewhere.
2. **Preflight** (`manage.py preflight --mode live`): API key, market policy, challenge end (> 1 h left),
   clock drift < 3 s, balances (TMN > 0; other coins listed), no open orders, the prop package (a 401 passes
   only when the end was set by hand, §3), the local model's digest, the database, an engine build with the
   config hash, and no uncommitted trading code.
3. **Smoke test** (`manage.py smoke_test`, G3): shows the planned order and waits for a typed `YES`. It sends one
   LIMIT BUY about 5% under the best bid on USDTTMN (just above the 50,000 TMN minimum), reads it back by
   client_id, cancels it, confirms the cancel and that no order stays open. It prints every response field and
   the fee endpoint, and writes `logs/smoke-<time>.json` (local only).
   - **Then the round trip (typed `ROUNDTRIP`):** one LIMIT BUY on BTCTMN at the best ask, about 55,000 TMN, then a
     MARKET SELL of the whole available BTC balance. It reports which currency each fee was charged in (BTC or
     TMN), the real fee rates, the leftover dust and the cost (~0.6% of 55k). If the sell cannot close the
     position, go-live **aborts**. Report: `logs/roundtrip-<time>.json`.
   - **Check by eye:** `balance with the order resting`. If `value` dropped by the order notional and `locked` rose,
     `value` is *available*, not total. Tell the developer before going live: the engine's equity would then
     need `value + locked`.
4. **Typed `GO LIVE`.**
5. **Tag and push:** `git tag -a live-v1` on HEAD, `git push origin live-v1`.
6. **Start:** `LIVE_TRADING_CONFIRMED=true scripts/run_engine.sh live`, in its own session (it survives the
   terminal), with sizes ×0.5 for the first 2 h. Shadow keeps running in parallel.

After the start: `cat logs/heartbeat_live.txt`, `make api` + `make dashboard` → http://localhost:3000/?mode=live.

**Code freeze:** after `live-v1`, change engine/strategy/risk code only for a safety fix; each fix gets a new
tag (`TAG=live-v2 make go-live` after stopping the engine) and an entry in `CHANGELOG.md`.

## 3. Challenge end set by hand

The prop endpoint `/v1/prop/user/packages/history` answers 401 «ابتدا وارد شوید» with an API key (it needs a web
login), so `expire_at` cannot be read. Set the end yourself:

```bash
# .env: ISO time; without an offset it is read as Asia/Tehran
CHALLENGE_END=2026-10-26T12:00:00+03:30
```

`CHALLENGE_END` wins over `config/live.toml` `[challenge] end`. The engine stops new entries 2 h before it,
flattens to TMN in the last 60 min, checks ≥ 99% TMN at the end, and exits. Without either value preflight fails.

## 4. Daily routine

```bash
cat logs/heartbeat_live.txt logs/heartbeat_shadow.txt          # both ≤ 1 min old
tail -n 20 journal/live/decisions-$(TZ=Asia/Tehran date +%Y%m%d).jsonl
.venv/bin/python manage.py recorder_status --hours 1
.venv/bin/python scripts/export_decisions.py --mode live         # deliverable: reports/decision_logs/
```

## 5. Failure playbook

| symptom | what the engine does | what you do |
|---|---|---|
| Wallex unreachable / DNS errors (`nodename nor servname`) | after 5 consecutive errors: no new entries, exits keep running; resumes by itself after ~10 min of clean loops | check the VPN split tunnel (`api.wallex.ir` must resolve); do not trade by hand |
| `HALT_ENTRIES` that does not lift | reconcile found an open order the engine does not know | look at `RECONCILE` rows; if it is ours from a lost state file, cancel it on the website only if the organizers allow; restart the engine |
| `KILL` (−2.5% from start) | flattens everything to TMN and halts for good | nothing; leave it halted (a restart keeps it halted: the state file stores `halted`) |
| `BLOCK` (−2% in a Tehran day) | no new entries until the next Tehran day | nothing |
| engine crashed | the supervisor restarts it after 10 s; state and orders are restored, then reconciled | `tail logs/engine_live.log`; if it crash-loops, stop it (below) and report |
| local LLM down or slow | Officer failure → OFF (no new entries); Trade Agent failure → reject | `brew services restart ollama`; exits never depend on the LLM |
| heartbeat older than 2 min | — | `ps -ax | grep run_engine`; restart with `make go-live` steps 5–6 only: `LIVE_TRADING_CONFIRMED=true scripts/run_engine.sh live` |
| end of challenge | flatten in the last 60 min, `FINAL` row with the TMN share | check `FINAL ok: true`; export the decision log |

**Stop the live engine:** `kill -TERM $(cat logs/engine_live.pid)`. It stops after the current loop; open
positions stay on the exchange and are restored at the next start.

**Never** place or cancel orders by hand on the prop account: manual trades disqualify the team.
