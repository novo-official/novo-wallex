# Phase 4: LLM roles, RiskGuard and the LLM ablation (design and status)

> **خلاصه:** دو نقش LLM ساخته شد.
> - **افسر رژیم و ریسک:** ساعتی تصمیم می‌گیرد که حالت ریسک NORMAL باشد، DEFENSIVE یا OFF، کدام بازارها کنار گذاشته شوند، و معاملات فعالیت روشن باشند یا نه.
> - **عامل معامله:** هر ورود اصلی را تأیید یا رد می‌کند، و می‌تواند حجم را کم کند، استاپ را تنگ‌تر کند یا هدف را نزدیک‌تر بیاورد.
>
> RiskGuard (کد خالص) حرف آخر را می‌زند: LLM فقط می‌تواند ریسک را کم کند، هرگز زیاد.
> - **ورودی‌ها مسیرمستقل‌اند،** یعنی هر تصمیم فقط به بازار در آن لحظه بستگی دارد، نه به وضعیت حساب. برای همین پاسخ‌ها cache می‌شوند و اجرای مجدد رایگان و یکسان است.
> - **در بازپخش تاریخی** هیچ تاریخ و قیمت مطلقی به مدل داده نمی‌شود، تا مدل از حافظه‌اش دربارهٔ آن دوره استفاده نکند.
> - **مقایسهٔ بدون LLM / با افسر / با افسر و عامل معامله** روی پنجره‌های ۳ روزهٔ بدون هم‌پوشانی آماده است و فقط کلید API و تأیید هزینه را لازم دارد (حدود ۱۰ دلار برای selection و ۱۸ دلار با holdout، با Batch API و مدل Opus 5.5).
> - نتیجه‌ای هنوز وجود ندارد، چون هیچ تماسی با LLM گرفته نشده است.

## 1. What changed in the strategy (user decisions, 2026-10-22)

- **Canonical** = trend rules (USDT/BTC/ETH, full size) + **activity trades**: h24 ML signal, E[net] > −0.5%, 100k TMN, 2h hold; minimum order 50k. This keeps ≥ 3 round trips/day. The cost is accepted (holdout mean −0.39% vs +0.06% for the rules alone; `docs/RESEARCH_P3.md` §7).
- **Daily-quota variant** (at most 2 activity entries per Tehran day): `[shadow_variants.activity_quota]` in `config/strategy.toml`. It is judged **only on forward (shadow) data**; `backtest/experiment.py` refuses to run it on history.
- `models/ml_net_h24/` is the model live trading loads (`ml/live.py`).

## 2. RiskGuard (`risk/guard.py`), the final word

For every entry, in order:
1. market policy;
2. kill switch, daily loss block, last 2 h, risk_mode OFF, halt;
3. one position per symbol;
4. self-trade guard (returns a "cancel your resting sell" action);
5. LLM adjustments, clamped: size only down, stop only tighter, take-profit only closer and ≥ 0.7%; DEFENSIVE halves the size;
6. caps: risk per trade, 20% per position, 60% total, minimum order, precision;
7. the buffer above `liquidity_limit`.

The backtest engine sends every decision through it. A re-run of the P2 rules on 233 windows gave the identical result after the refactor.

## 3. The roles (`agent/roles.py`, prompts in `agent/prompts/*.md`, versioned by content hash)

| | Regime/Risk Officer | Trade Agent |
|---|---|---|
| when | first tick of every hour | each core (trend) entry; activity trades only via the officer's hourly switch |
| input | market-only summary: returns, volatility, trend, fair-value premium, breadth, Tehran hour/weekday; news when a feed is plugged in | the candidate (stop/target distances, signal ratios) plus the same market summary; read-only WallexTools when available (≤ 3 calls) |
| output (schema-validated) | risk_mode NORMAL/DEFENSIVE/OFF (code ceiling), avoid_symbols, activity_trades, usdt_risk, rationale | approve, size_fraction, stop_fraction, take_profit_fraction, rationale |
| on failure | OFF: no new entries, exits continue | reject |

- **Why market-only and path-independent:** account risk is RiskGuard's job in code. Keeping the LLM inputs free of account state makes every call depend only on the hour (or the entry), so calls are cacheable, batchable and identical across overlapping windows.
- **Blind replay (anti-leakage):** historical contexts carry no date, no price level and no free-text rule reason (those contain prices). A model trained on data up to 2026 could otherwise recognize the period and trade on hindsight. A test checks this.
- **Decision log:** every LLM call is journaled with role, prompt version, prompt hash, cache hit, tokens and output (`logs/llm_journal.jsonl`). RiskGuard writes `LLM[<prompt hash>]: <rationale>` into each trade's decision record.

## 4. LLM client (`agent/llm.py`)

- **Model and SDK:** `claude-opus-5-5` through the official Anthropic SDK. Server-side refusal fallback is on for online calls; it is not available on batches.
- **Proxy:** `LLM_HTTPS_PROXY` is used only by this client.
- **Requests:** structured output (`output_config.format`, strict JSON schema), effort `low`, at most 3 tool calls.
- **Failures:** invalid output gets one retry; after that the safe fallback applies.
- **Daily token budget.**
- **Disk cache keyed by the sha256 of the full request.** `ReplayProvider` never calls out: a cache miss is a fallback, not a network call. Opus 5.5 takes no `temperature`, so determinism comes from the cache.
- **Tools (`agent/tools.py`):**
  - live: GET `/v1/depth`, `/v1/trades`, `/v1/markets`, policy-checked;
  - recorded: the recorder's Parquet as of a time;
  - unavailable: historical replay.
  - It is an adapter, so the official Wallex MCP can replace it later.

## 5. The LLM ablation (ready, not run)

- **Setup:** non-overlapping 3-day windows, one every 3 days, so every hour is seen once. There are 23 selection windows (2026-06-20 → 08-29) and 17 holdout windows.
- **Variants:** canonical without LLM, + officer, + officer + trade agent. Three configs, logged, holdout once.
- **Replay:** the officer is precomputed in one Message Batch (`p4_experiment plan` → `submit --confirm-spend` → `collect`). The trade agent calls the API only for entries that happen (no tools in historical replay: no order book exists before 2026-10-22).
- **Cost plan** (offline estimate, `p4_experiment plan`, Opus 5.5 at batch prices):
  - selection: 1,656 officer calls, about $10;
  - with holdout: 2,880 calls, about $18;
  - trade agent: about $6.
- **Honest limits:**
  1. Historical replay has **no news**, and news is the role the evidence points to (USDTTMN moves in news-driven jumps). The backtest can only test the price-based judgment. The news role can be judged in shadow and live only.
  2. 23 + 17 windows is a small sample, so differences of a few tenths of a percent will not be significant.
  3. The trade agent has no order book in replay, so its live behavior (with tools) is not what the backtest measures.

## 6. Status

**Built and tested:**
- RiskGuard: 13 tests.
- LLM client:
  - cache, replay without network, retry, refusal, budget;
  - the 3-tool limit;
  - the request built by the precompute has the same hash as the one sent at run time.
- Roles and the agent layer:
  - the officer's ceiling and fallback;
  - one officer call per hour;
  - vetoes;
  - blind context.
- The engine obeys the agent and RiskGuard clamps it: a "go big" size × 5 is ignored.
- Recorded tools.
- The ablation harness and the cost plan.

**Pending:**
- An Anthropic API key and a reachable `LLM_HTTPS_PROXY`.
- `pip install anthropic==1.17.0` (PyPI was unreachable from this machine on 2026-10-22).
- The user's approval of the spend.
- Then the ablation runs.
