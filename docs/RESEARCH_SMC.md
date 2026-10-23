# SMC / ICT research: rejected for live

> **خلاصه:**
> - **تعریف‌ها:** مفاهیم ICT/SMC (سوئینگ، BOS/CHoCH، جمع‌آوری نقدینگی، displacement، FVG، اوردربلاک، premium/discount) به‌صورت توابع خالص و علّی تعریف و تست شدند.
> - **ماژول قانون‌محور `smc_long`:** با تعریف‌های داده‌شده تقریباً هیچ‌وقت معامله نمی‌کند (۰ تا ۰٫۰۶ معامله در روز). در ۱۴ روز اخیر ۲ معامله کرد، هر دو زیان‌ده (−۰٫۱۵٪).
> - **ویژگی‌های SMC در مدل ML:** IC را بهتر نکردند (۰٫۰۷۰ به ۰٫۰۶۹) و در SHAP رتبه ۳۱ تا ۴۸ از ۴۸ گرفتند.
> - **نتیجه:** دروازه رد شد (FAIL). live-v1 بدون تغییر می‌ماند و SMC به‌عنوان ماژول بررسی‌شده و ردشده مستند می‌شود.

All runs: measured fees (TMN maker 0.25% / taker 0.30%, `GET /v1/account/fee`; USDT 0.10/0.13 does not apply,
both markets are TMN-quoted), the live risk limits (15% per position, 45% total, 50k minimum order), BTCTMN +
ETHTMN (USDTTMN orders are refused on the prop account). 18 configurations in 6 logged experiments
(`reports/experiments.jsonl`, names starting "SMC"); the holdout was evaluated once.

## 1. Definitions

`research/smc/features.py`, documented in `STRATEGY.md` §7, parameters in `config/smc.toml`. Pure functions,
closed bars only: `research/smc/tests` checks that no value changes when later bars arrive and that a swing
point is known only N bars after it.

## 2. Setup frequency (selection period)

Raw 15m setups (sweep → displacement + BOS → zone), then each entry condition:

| feature variant | setups | 1h bullish | R ≥ 1.5 | target ≥ 0.7% | discount | all conditions |
|---|---|---|---|---|---|---|
| N=2, FVG | 32 | 17 | 4 | 8 | 14 | 2 |
| N=2, FVG or OB | 87 | 48 | 10 | 28 | 54 | 4 |
| N=3, FVG | 22 | 9 | 0 | 3 | 6 | 0 |
| N=3, FVG or OB | 50 | 26 | 2 | 15 | 25 | 1 |

**R ≥ 1.5 is the binding condition.** The fee floor forces a stop of at least 0.8%, so 1.5 R means a target at
least 1.2% away, and the nearest 15m swing high above a fresh setup is rarely that far. Last 14 days: 1.36 raw
setups a day (both markets), 0.14 trades a day. We did not loosen the definitions after seeing this: that would
be fitting the rules to the data.

## 3. Experiment A: `smc_long` (rule module)

**Selection** (143 windows, 8 configs): the four FVG-only configs never traded; FVG-or-OB traded 0.02–0.06 times a
day with means of −0.004% to −0.012% per window. Chosen for the holdout: `smc_long_p2fvgob_t24`, the most active
on selection (activity is a gate condition; the config with the best objective made zero trades).

| | holdout: mean per 3-day window | P(>+0.2%) | p10 DD | worst DD | trades/day | windows with no trade |
|---|---|---|---|---|---|---|
| SMC `p2fvgob_t24` | −0.01% | 0% | −0.07% | −0.08% | 0.06 | 81% |
| live config (a), ML-only proxy | −0.51% | 0% | −0.84% | −1.09% | 5.38 | 0% |

Fee table (holdout mean): SMC low −0.01% / base −0.01% / stress −0.02%; (a) −0.35% / −0.51% / −0.72%.

**Last 14 days** (to 2026-10-22):

| | return | max DD | trades (per day) | win rate | avg R | setups/day |
|---|---|---|---|---|---|---|
| SMC `p2fvgob_t24` | −0.15% | −0.15% | 2 (0.14) | 0% | −1.84 | 1.36 |
| live config (a), ML-only proxy | −1.82% | −1.86% | 53 (3.79) | 26% | −0.70 | — |

(a) is shown without its LLM roles; with them, the 7-day replay cut its loss roughly in half (RESEARCH_P4 §6).

## 4. Experiment B: SMC features in the h24 model

Same labels, costs and purged walk-forward; seven features: distance to the nearest unfilled bullish FVG (ATR),
inside-OB, bars since BOS, sweep in the last 8 bars, premium/discount %, 15m and 1h structure.

| | Spearman IC (selection, out of fold) | daily IC mean | daily IC > 0 |
|---|---|---|---|
| h24 | 0.0703 | 0.188 | 97% |
| h24 + SMC | 0.0691 | 0.196 | 96% |

SHAP: the SMC features rank 31st to 48th of 48 (the 15m structure is the strongest, at 0.005% mean |SHAP|). The
top five are fair-value premium, distance to the 15m EMA20, USDT volatility, quote volume and the USDT return.
Live config (a) with the new predictions: selection mean −0.44% (unchanged), last 14 days −1.93% (vs −1.82%).

## 5. Gate (all must pass)

| condition | result |
|---|---|
| last 14 days ≥ break-even at measured fees | **FAIL** (−0.15%) |
| holdout P(3-day > +0.2%) ≥ the current config's, worst DD no worse | pass (0% vs 0%; −0.08% vs −1.09%), only because it barely trades |
| ≥ 1 trade/day over the last 14 days | **FAIL** (0.14) |

**FAIL: live stays on live-v1 (now live-v1.1); `smc.enabled = false`.** Nothing was added to the Trade Agent
context and no SMC shadow instance was started.

## 6. Honest reading

- As quantified here, the ICT setups are rare on Wallex's TMN markets, and the few that pass every filter did not
  make money. The fee floor (0.55% round trip) is what makes the R requirement so hard: it forces wide stops
  relative to 15m structure.
- As ML features they add nothing the model did not already know (trend, distance to EMAs, volatility).
- What would be worth testing next, as a new pre-registered experiment rather than by loosening these rules:
  SMC on the USDT pairs (2.5× cheaper fees, so tighter stops pass the fee floor), and 1h setups with 4h structure
  (larger swings, so 1.5 R targets exist).

## Reproduce

```bash
.venv/bin/python scripts/smc_experiment.py frequency
.venv/bin/python scripts/smc_experiment.py selection
.venv/bin/python scripts/smc_experiment.py final smc_long_p2fvgob_t24    # logs a holdout evaluation
.venv/bin/python scripts/smc_experiment.py recent smc_long_p2fvgob_t24
.venv/bin/python scripts/smc_experiment.py ml
```
