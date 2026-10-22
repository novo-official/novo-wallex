# Strategy, AI role and risk management

> **خلاصه:**
> - **استراتژی:** موتور روی سه بازار USDTTMN، BTCTMN و ETHTMN فقط با قوانین روند معامله می‌کند. شرط ورود روند صعودی ۱ ساعته و شکست سقف ۵ دقیقه است، با استاپ ثابت ۲٪ و هدف ۶٪.
> - **نقش هوش مصنوعی:** یک مدل زبانی محلی و رایگان (Qwen3 8B روی Ollama، با نام و digest ثابت) دو نقش دارد: افسر رژیم و ریسک، و عامل تأیید معامله.
> - **حرف آخر با RiskGuard است:** کد خالصی که LLM نمی‌تواند از آن عبور کند و فقط اجازه دارد ریسک را کم کند.
> - **تکرارپذیری:** همهٔ پاسخ‌های LLM در ریپو ذخیره شده‌اند، پس داوران بدون نصب هیچ مدلی می‌توانند نتایج را دقیقاً بازتولید کنند.
>
> این سند در فاز ۷ کامل می‌شود.

Evidence for every claim is in `docs/RESEARCH_P2.md` (data, costs, rules), `docs/RESEARCH_P3.md` (ML, objective, activity) and `docs/RESEARCH_P4.md` (LLM, RiskGuard).

## 1. What trades

The trend rules, on USDTTMN, BTCTMN and ETHTMN. These are the three TMN markets with the lowest slippage (2–4 bps) and the only ones that accept MARKET orders.

- **Buy:**
  - **regime:** the 1h close above the 1h EMA200, EMA50 above EMA200, and EMA50 rising;
  - **trigger:** a 5m bar that traded and closed above the highest high of the previous 12 bars and above the 15m EMA20;
  - **order:** a maker limit at the bid, cancelled after 3 bars.
- **Sell:**
  - a fixed 2% stop;
  - a 6% take-profit (maker);
  - otherwise the end-of-window flatten to TMN.
- **What was tested and rejected, with evidence:**
  - mean reversion;
  - ML-only entries;
  - ML gates on the rules;
  - USDT regime allocation (raw and detrended);
  - SOL/XRP;
  - activity trades for 3 per day.

## 2. AI design: zero-cost, offline, pinned, reproducible

This is a deliberate choice (team decision, 2026-10-22). The AI that takes part in trading decisions has five properties:

- **Zero cost:** no paid API. The model runs on the team's own machine.
- **Offline:** the decision path needs no internet. That matters on an Iranian network, where foreign APIs are blocked or need a VPN.
- **Pinned:** `config/llm.toml` names the model **and** its digest: `qwen3:8b`, `500a1f067a9f…`, Q4_K_M, 8.2B parameters. If the installed model differs, the engine refuses it. Every call uses a JSON schema (`format`), temperature 0 and seed 42.
- **Auditable:** every decision records `provider/model@digest`, the prompt version (a content hash of the prompt file) and the request hash. The log is `logs/llm_journal.jsonl`, and the provenance also appears in each trade's decision record.
- **Reproducible:**
  - **What is committed:** every model answer is stored in `data/llm_cache/`, keyed by the sha256 of (model identity + full request).
  - **`make reproduce`:** replays the AI decisions from that cache and checks every metric against the committed report. It needs **no model and no network**.
  - **`make llm-setup` + `make reproduce-llm-live`:** installs the pinned model and regenerates every answer from scratch.

### Roles

| | Regime/Risk Officer | Trade Agent |
|---|---|---|
| when | every hour live (every 4 h in historical replay, for runtime) | each core rule entry |
| sees | market-only summary: returns, volatility, trend, fair-value premium of TMN over USDT × USDTTMN, breadth, Tehran hour/weekday; news (free RSS, local summary) in shadow/live | the candidate's stop/target distances and signal ratios, the same summary, read-only Wallex tools in live (order book, trades, market rules; ≤ 3 calls) |
| decides | risk_mode NORMAL / DEFENSIVE (half size) / OFF (no new entries), markets to avoid, activity on/off | approve or reject, smaller size, tighter stop, closer target |
| if it fails | OFF: no new entries; exits keep running | reject |

- **Blind historical replay:** no dates and no price levels reach the model, so it cannot recall what happened in that period.
- **Optional fallbacks (shadow/live only, never the backtest):** the free tiers of Groq and Gemini Flash, through one OpenAI-compatible client with rate-limit backoff. If every provider fails, the engine falls back to "no new entries, keep managing exits".

## 3. Risk and capital management (code, final word)

RiskGuard (`risk/guard.py`) checks every entry. The LLM's output passes through it and can only *reduce* risk:

- **Position limits:**
  - at most 20% of equity per position and 60% total;
  - 0.5% of equity at risk per trade, and stops at most 2%;
  - one position per market, no averaging down;
  - a minimum order of 500k TMN.
- **Account loss limits:** −3% in a Tehran day blocks new entries until the next day; −6% from the start flattens everything and halts.
- **End of the challenge:** no new entries in the last 2 h, and everything back to TMN in the last 60 min.
- **Exchange-side guards:** the market policy (allowlist and blocklist) before every order, the self-trade guard, and a 20% buffer above the prop account's `liquidity_limit`.

## 4. Results so far

See `docs/RESEARCH_P2.md` §4, `docs/RESEARCH_P3.md` §4 and §7, and `docs/RESEARCH_P4.md` (the local-LLM ablation). Fees are the defaults (maker 0.25%, taker 0.35%) until the organizers confirm them, and every table also shows 0.15/0.20 and ×1.5.
