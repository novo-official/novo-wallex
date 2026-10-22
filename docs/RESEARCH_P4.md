# Phase 4: local LLM roles, RiskGuard and the LLM ablation

> **خلاصه:** (پس از اجرای ablation تکمیل می‌شود.)

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

## 5. Ablation (pending: runs after the officer precompute)

- **Setup:** non-overlapping 3-day windows, 39 selection and 17 holdout.
- **Variants:** rules, rules + officer, rules + officer + trade agent.
- **What is reported:** the fee table, P(>+0.2%), mean/median, p10 and worst drawdown, worst window, and no-trade windows.
