# Phase 4: local LLM roles, RiskGuard and the LLM ablation

> **خلاصه:**
> - **بازده:** دو نقش مدل زبانی محلی (افسر ریسک و عامل معامله) بازده قوانین را بهتر نکردند. میانگین بازده هر پنجره ۳ روزه −۰٫۰۴٪ بدون LLM و −۰٫۰۵٪ با LLM است.
> - **ریسک:** بدترین افت سرمایه را کم کردند (−۲٫۳۰٪ به −۱٫۸۱٪) و بدترین پنجره هم بهتر شد (−۱٫۵۸٪ به −۰٫۸۵٪).
> - **آزمون کنترل:** قوانین با همان اندازه متوسط (۰٫۷۶ برابر) و بدون LLM تقریباً همان کاهش افت را می‌دهند (−۱٫۷۵٪). پس بیشتر اثر ریسکی LLM همان کوچک‌تر کردن حجم است، نه قضاوت. فقط بدترین پنجره و p10 افت کمی بهتر است، که با ۱۳ پنجره معنادار نیست.
> - **holdout (۶ پنجره):** قوانین +۰٫۸۸٪، با هر دو نقش +۰٫۷۱٪. بازده به همان نسبتی کم شد که حجم کم شده بود (۲۰٪)، و افت هم از −۱٫۷۰٪ به −۱٫۳۶٪ رسید.

## 1. Decisions (user, 2026-10-22)

- **No paid LLM spend.**
  - **Primary model:** local, open-weight, through Ollama: `qwen3:8b` (Q4_K_M, 8.2B), pinned by digest `500a1f067a9f…` in `config/llm.toml`.
  - **Why this size:** the machine is an Apple M4 with 16 GB, which suits an 8B 4-bit model.
  - **Calls:** JSON schema in `format`, temperature 0, seed 42, `think: false`.
- **Fallbacks:** the free tiers of Groq and Gemini Flash through one OpenAI-compatible client, with rate-limit backoff. They are used in shadow/live only, never in the canonical backtest. If every provider fails: no new entries, keep managing exits.
- **Activity rule:** ≥ 1 round trip per day, with as few zero-trade windows as possible. The canonical strategy is the trend rules alone. The 100k activity trades with a daily quota are a shadow-only variant, which the code refuses on history.
- **News role:** free RSS feeds only, summarized by the same local model. Only the interface and a stub exist (`agent/news.py`); it will be evaluated in shadow.

## 2. Provenance and reproducibility

- **Cache key:** sha256 of the provider identity (provider, model, digest) plus the full request (system prompt, messages, schema, tools, options). Answers from different models can never mix.
- **Journal:** every call logs role, prompt version (file name + content hash), provider, model, digest, request hash, cache hit, latency, tokens and the output.
- **Decision record:** RiskGuard writes `LLM[ollama/qwen3:8b@500a1f067a9f regime_officer_v1@… #<hash>]: <rationale>` into each trade's decision record.
- **`make reproduce`:** replays from the committed `data/llm_cache/`, with no model and no network, and checks every metric against `reports/p4-llm_ablation/fee_table.csv`.
- **`make reproduce-llm-live`:** regenerates all answers with the pinned model into a fresh cache.

## 3. Benchmark (`reports/p4-benchmark`, 20 calls, cache bypassed)

The 20 calls are 5 officer contexts and 5 trade-agent candidates, each sent twice.

| metric | value |
|---|---|
| schema-valid JSON on the first answer | **100%** (20/20) |
| identical decision fields when the same input is sent twice | **100%** (10/10) |
| byte-identical output (rationale text included) | 70% (7/10) |
| latency, median / max | 11.0 s / 20.1 s (officer 9.2 s, trade agent 13.7 s) |
| tokens per call, median | ~1,080 in / ~100 out |

**Observations:**
- **Determinism:** temperature 0 and a fixed seed make the *decisions* deterministic, but the wording of the rationale can still vary. On Apple Metal, llama.cpp is not bit-reproducible across runs. The committed cache is what makes a replay exact.
- **An unsupported claim:** one trade-agent rationale mentioned a "thin order book", although no order book was given (`tools_available: false`). Rationales can contain claims that the data does not support.
- **Default sizing:** the trade agent tends to answer `size_fraction: 0.8`. That is a systematic 20% size cut, and the ablation has to separate it from skill.
- **Runtime under load:** during the long run, throughput fell to ~20 s per call under memory pressure (16 GB, other apps open, 2 GB swap).

## 4. Replay cadence (documented difference)

| | officer cadence | trade agent | context | tools |
|---|---|---|---|---|
| live / shadow | every hour | each core entry | real time, prices, news (when enabled) | live Wallex GET (≤ 3 calls) |
| historical replay | **every 4 h** (runtime: 1,008 calls instead of ~4,000) | each core entry | **blind**: no dates, no price levels, no news | none (no order book before 2026-10-22) |

## 5. Ablation (local model, blind replay)

- **Windows:** the time-boxed subset: every 3rd non-overlapping 3-day window, 13 selection + 6 holdout
  (`agent/experiments.py` `SUBSET_EVERY = 3`). A first run used all 39 selection windows by mistake
  (`llm_ablation` did not pass the subset; fixed in 4209fe4); it was stopped before it reported anything.
- **Variants:** rules (the trend core), rules + Officer, rules + Officer + Trade Agent. Same code, same windows.
- **Calls:** 234 Officer decisions (every 4 h) and 86 Trade Agent reviews in the selection, 0 fallbacks.
- **Fees:** the study's default table (base 0.25/0.35, low 0.15/0.20, stress ×1.5), not yet the measured 0.25/0.30.

### 5.1 Selection (13 windows), base fees

| variant | P(>+0.2%) | mean | median | p10 return | p10 DD | worst DD | worst window | trades/day | no-trade windows |
|---|---|---|---|---|---|---|---|---|---|
| rules | 23% | −0.04% | −0.52% | −0.63% | −1.93% | −2.30% | −1.58% | 0.85 | 23% |
| + Officer | 23% | −0.06% | −0.52% | −0.84% | −1.53% | −2.25% | −1.06% | 0.82 | 23% |
| + Officer + Trade Agent | 23% | −0.05% | −0.42% | −0.68% | −1.21% | −1.81% | −0.85% | 0.82 | 23% |
| control: rules ×0.76 size, no LLM | 23% | −0.03% | −0.39% | −0.48% | −1.47% | −1.75% | −1.20% | 0.85 | 23% |

Low fees: rules +0.08%, + Officer +0.06%, + both +0.05% mean. Stress: −0.19% / −0.20% / −0.16%.
Full tables: `reports/p4-llm_ablation/fee_table.csv`, control: `reports/p4-ablation_size_control/`.

### 5.2 What the roles did

- **Officer:** never OFF. DEFENSIVE (half size) in 118 of 234 blocks, NORMAL in 116. It put BTCTMN on the avoid
  list in 130 blocks and ETHTMN in 122, USDTTMN in 34: a steady preference for USDTTMN.
- **Trade Agent:** approved 68 of 86 candidates (79%). Size answers: 0.8 in 66 cases, 0.5 in 18, 0.7 in 2.
- **Exposure:** average 0.158 with both roles vs 0.209 for the rules (−24%).

### 5.3 Conclusion (honest)

1. **The LLM did not improve returns.** Mean and P(>+0.2%) are unchanged within noise.
2. **It reduced drawdown, but mostly by trading smaller.** The equal-exposure control (rules ×0.76, no LLM)
   gets almost the same worst drawdown (−1.75% vs −1.81%) and a slightly better mean. Only the worst window
   (−0.85% vs −1.20%) and the p10 drawdown (−1.21% vs −1.47%) favor the LLM, on 13 windows: not significant.
3. **So the LLM's real value here is as a risk governor and an auditable veto**, not as alpha. We keep it in
   the live path because it can only reduce risk (RiskGuard), it explains every decision, and in live it sees
   real-time context (order book tools, news) that the blind replay cannot test.
4. **Size bias:** the Trade Agent's habitual 0.8 is a uniform size cut. A future prompt should ask for an
   explicit reason before any size below 1.

### 5.4 Holdout (6 windows, `--final`, evaluated once)

| variant | P(>+0.2%) | mean | median | p10 return | p10 DD | worst DD | worst window | avg exposure |
|---|---|---|---|---|---|---|---|---|
| rules | 50% | +0.88% | +0.56% | −1.00% | −1.62% | −1.70% | −1.58% | 0.383 |
| + Officer | 50% | +0.89% | +0.56% | −1.00% | −1.62% | −1.70% | −1.58% | 0.382 |
| + Officer + Trade Agent | 50% | +0.71% | +0.43% | −0.80% | −1.30% | −1.36% | −1.27% | 0.305 |

Low fees: +1.03% / +1.05% / +0.83%; stress: +0.69% / +0.71% / +0.56%. 19 trades in every variant, 0 fallbacks.

- **Same picture as the selection:** the Officer changed almost nothing; the Trade Agent cut exposure by 20% and
  the return by the same proportion (0.71 / 0.88 ≈ 0.305 / 0.383), with a matching cut in drawdown.
- **Caveats:** 6 windows only; the Sep–Oct holdout was seen 11× in earlier phases before the guard existed
  (CLAUDE.md §3), and it is a period of strong TMN devaluation that favors long-only rules. The really untouched
  test is the shadow/live data after 2026-10-22.
- **Reproduce:** `make reproduce` replays this run from `data/llm_cache/` without a model.

## 6. The live configuration with the LLM roles (`reports/ai_activity_llm`)

AI activity (the only live module) on the last 7 days (2026-10-15 → 10-22), blind replay, Officer every 4 h,
**measured fees** (TMN 0.25% / 0.30%). These days were already seen by the fix check (§ STRATEGY.md §5).

| variant | return | max DD | worst 3 days | trades (per day) | win rate | fees (TMN) |
|---|---|---|---|---|---|---|
| ML only (proxy) | −0.67% | −0.73% | −0.43% | 20 (2.86) | 10% | 26,356 |
| ML + Officer | −0.53% | −0.57% | −0.42% | 15 (2.14) | 7% | 17,615 |
| **ML + Officer + Trade Agent (live)** | **−0.32%** | **−0.36%** | **−0.33%** | 11 (1.57) | 18% | 11,408 |

- **Trade Agent:** approved 25 of 245 candidates (10%). Its typical reason: the 1h trend is not up while the ML
  edge is negative. Unlike the trend rules (§5), here it acts as a real filter, not a size cut.
- **Officer:** DEFENSIVE in 24 of 42 blocks (half size), never OFF.
- **Reading:** the LLM roles halve the loss and the drawdown of the live module and keep ≥ 1 trade/day, but the
  module is still negative: the roles reduce damage, they do not create an edge. 7 days, 11 trades: small sample.
