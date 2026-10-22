# Phase 3: per-bar ML, USDT allocation, and the ablation under the new objective

> **خلاصه:** طبق تصمیم‌های ۲۲ اکتبر، هدف جدید این است: بیشینه کردن احتمال بازدهٔ ۳ روزهٔ بالای +۰٫۲٪، به شرط افت سرمایهٔ p10 حداقل ‎−۳٫۵٪‎ و دست‌کم ۳ معامله در روز. پنجره‌های بدون معامله شکست حساب می‌شوند.
> - **ML روی همهٔ barها:** مدل روی بازده خالص ۱، ۲ و ۴ ساعته آموزش دید و **توان رتبه‌بندی واقعی** دارد (Spearman IC ‏۰٫۱۲، و IC روزانه در ۱۰۰٪ روزها مثبت). ولی حرکتِ قابل پیش‌بینی از هزینهٔ رفت‌وبرگشت (حدود ۰٫۶٪) کوچک‌تر است، و حتی دهک برترِ پیش‌بینی‌ها بعد از کارمزد زیان‌ده است.
> - **ML تنها** تقریباً هیچ معامله‌ای نمی‌کند، و هرچه آستانه را پایین بیاوریم زیانش بیشتر می‌شود.
> - **ترکیب قوانین + ML** در selection کمی بهتر است، ولی در holdout نه.
> - **ماژول تخصیص USDT** نه روی دادهٔ خام لبه دارد و نه روی دادهٔ بدون رانش. یعنی رانش تومان واقعی است، ولی زمان‌بندی آن با سیگنال‌های تکنیکال کار نمی‌کند.
> - **SOL/XRP** با اجرای maker هم بهتر نشد.
> - **فعالیت:** هیچ پیکربندی‌ای به ۳ معامله در روز نرسید. بیشترین فعالیت حدود ۱٫۷ در روز است، و فعالیت بیشتر با این کارمزد یعنی زیان بیشتر.
> - **پیشنهاد:** فقط ماژول روند (قوانین) روی USDT، BTC و ETH با ورود maker برای live مناسب است. ML و تخصیص USDT خاموش می‌مانند، ولی کد و شواهدشان در ریپو هست.

## 0. Rules for this phase

- **Objective:** maximize P(3-day return > +0.2%), subject to p10 drawdown ≥ −3.5% and ≥ 3 round trips per day on average. Windows with no trade count as failures. Mean and median are reported too.
- **Fee table** in every report:
  - low = 0.15% / 0.20%;
  - base = 0.25% / 0.35% (the default until the organizers confirm);
  - stress = base × 1.5.
- **At most 30 configs per experiment**, enforced in `backtest/experiment.py`.
- **Holdout:** the selection windows are the first 70% (to 2026-09-01). Holdout windows run only with `--final`, once per model, and every run is logged in `reports/experiments.jsonl`.
- **Disclosure:** that holdout (2026-09-01 → 10-22) had already been looked at **11 times in P2 and P3a** (see the log), so it is not clean. In P3b each model was chosen on selection windows only and evaluated on the holdout once. The only truly untouched test is the data after 2026-10-22, i.e. the shadow run (P5).
- **Experiment count so far:** 32 experiments, 1,684 configurations, 17 holdout evaluations (P2: 10 / 1,450 configs; P3a: 3 / 34; P3b: 14 / 131; P3c activity and the re-runs after the sizing fix: 5 / 69).

## 1. Maker entries: what they save (`reports/p3-entry_cost`)

P2 rules, 233 selection windows:

| entry | P(>+0.2%) | mean | fees paid per window (median) | worst DD |
|---|---|---|---|---|
| maker limit at the bid, 3-bar timeout | 22.3% | +0.12% | 0.24% of equity | −4.5% |
| taker at the next open | 21.5% | +0.07% | 0.28% of equity | −4.7% |

Maker entries save about **0.04% of equity per window (14% of the fee bill)** and add about 0.05% to the mean. Take-profits are maker; stops and time exits are taker, which is how the engine models them.

## 2. Per-bar ML (`ml/perbar.py`, `reports/p3-ml_targets`)

**Setup:**
- **Samples:** every 5m bar of BTCTMN, ETHTMN and USDTTMN, about 63k bars in the selection period.
- **Features:** 41 causal, price-free features:
  - multi-timeframe trend, volatility and volume;
  - USDTTMN momentum, regime and volatility;
  - fair-value premium;
  - Tehran hour, Thursday and Friday flags.
- **Orderbook features** (`market/orderbook_features.py`) are excluded from training because the history is too short. They will be logged from P5 on.
- **Walk-forward:** a 14-day block is predicted by a model trained on labels resolved more than **1 day (embargo)** before the block.
- **Model and output:** LightGBM, with an isotonic map from the score to the *realized net return of the trade*. So every model outputs a calibrated **E[net return]** after both fees and slippage.

Target choice (selection period only, out of sample):

| target | Spearman IC | daily IC > 0 | bars with E[net] > 0 | their realized net | top decile realized net |
|---|---|---|---|---|---|
| h12: hold 1h | **0.118** | **100%** | 0.003% | −0.21% | −0.42% |
| h24: hold 2h | 0.065 | 97% | 0.07% | −0.19% | −0.38% |
| **h48: hold 4h (chosen)** | 0.031 | 82% | 0.17% | **+0.01%** | −0.29% |
| tb48: 1% stop / 2% target / 4h | 0.054 | 80% | 0.16% | −0.16% | −0.38% |

What this shows:
- **The model ranks short-term returns well.** Part of the 1h skill is probably bid/ask bounce in the close, which cannot be captured at that price.
- **The predictable move is smaller than the cost.** The mean net return of a random bar is −0.55%. Even the best decile loses 0.3–0.4% after fees. At the low fees the top decile would be about break-even, not positive.
- **h48 was chosen** because it is the only target whose "E[net] > 0" bars are not negative.

**SHAP** (`reports/p3-explain_h48`, LightGBM TreeSHAP on the final model). The top drivers:
1. USDTTMN 1-day volatility (−).
2. USDTTMN 1-day return (+, momentum).
3. Distance from the 15m EMA20 (−, short-term mean reversion).
4. **Fair-value premium** (−: buying at a premium of TMN over USDT × USDTTMN means a lower future return).
5. 1-day volume (−).
6. **Friday** (−, as in P2).

All effects are about 0.03–0.07% per bar, one order of magnitude below the round-trip cost. The final h48 model is saved in `models/ml_net_h48/` and is **not live**.

## 3. USDT regime allocation (`strategy/modules/usdt_allocation.py`)

**Module:**
- **Holding rule:** hold USDTTMN while its regime is up. Three definitions: 4h (close > EMA50 and EMA20 > EMA50), 1h200, and 4h200.
- **Stop:** 2 or 3 × the 1h ATR, capped at 2%.
- **Exit:** when the regime turns down, followed by a cooldown.
- **Return to TMN:** the engine's end-of-window flatten guarantees it.
- **Size cap:** 20% or 40% of equity. The 40% is a risk-layer override (`position_caps`), evaluated only as an experiment.

**Drift check:** USDTTMN prices are detrended (× exp(−μt), μ = the 180-day average drift) and the same 12 configs are re-run on that data.

| (base fees) | selection P(>+0.2%) / mean / worst DD | holdout P(>+0.2%) / mean / worst DD |
|---|---|---|
| chosen: 4h regime, 3×ATR, 40% cap, raw | 26% / −0.07% / −2.3% | 27% / −0.20% / −3.0% |
| same, detrended USDTTMN | 11% / −0.17% / −2.0% | 21% / −0.06% / −2.8% |
| buy & hold USDTTMN, raw | 43% / −0.31% / −11.6% | 46% / +0.46% / −6.9% |
| buy & hold USDTTMN, detrended | 27% / −1.26% / −12.2% | 38% / −0.49% / −7.5% |

All 12 raw configs have a negative mean in selection. So the allocation keeps the drawdown far below buy-and-hold (−3.0% vs −6.9%), as required, but only because it is out of the market most of the time. It **does not capture the drift**: the devaluation comes in jumps that a closed-bar trend filter enters late and leaves on the pullback. With the drift removed it is negative as well, so there is **no timing edge** either.

That is why the drift is the LLM's natural target in P4. News-driven moves are the part that price data cannot anticipate.

## 4. Ablation on the same windows (`reports/p3-rules_plus_ml`, `reports/p3-ml_only`)

These are the 142 selection windows that start after the first ML prediction (2026-06-20) and the 96 holdout windows. "rules" is the P2 trend module (#22: USDT/BTC/ETH, 1h uptrend + 5m breakout, 2% stop, 6% target, maker entries).

| variant | split | fees | P(>+0.2%) | trades/day | mean | median | p10 DD | worst DD | no-trade windows |
|---|---|---|---|---|---|---|---|---|---|
| rules | selection | low | 38.7% | 1.20 | +0.52% | 0.00% | −1.8% | −4.3% | 19% |
| rules | selection | base | 33.8% | 1.20 | +0.34% | 0.00% | −2.1% | −4.5% | 19% |
| rules | selection | stress | 28.9% | 1.20 | +0.13% | 0.00% | −2.5% | −4.8% | 19% |
| rules | **holdout** | low | 42.7% | 1.28 | +0.25% | 0.00% | −2.5% | −3.6% | 10% |
| rules | **holdout** | base | **37.5%** | 1.28 | **+0.06%** | 0.00% | −2.7% | −3.9% | 10% |
| rules | **holdout** | stress | 33.3% | 1.28 | −0.16% | −0.08% | −3.1% | −4.3% | 10% |
| rules + ML h48 (both modules) | selection | base | 35.2% | 1.23 | +0.37% | 0.00% | −2.1% | −3.7% | 19% |
| rules + ML h48 | **holdout** | low | 43.8% | 1.69 | +0.19% | 0.00% | −2.9% | −3.6% | 7% |
| rules + ML h48 | **holdout** | base | **38.5%** | 1.69 | **−0.05%** | 0.00% | −3.1% | −3.9% | 7% |
| rules + ML h48 | **holdout** | stress | 29.2% | 1.69 | −0.35% | −0.33% | −3.5% | −4.3% | 7% |
| ML-only h48, E[net] > +0.1% | selection | base | 7.7% | 0.12 | +0.02% | 0.00% | −0.3% | −0.4% | 76% |
| ML-only h48, E[net] > +0.1% | **holdout** | base | 3.1% | 0.58 | −0.13% | 0.00% | −1.2% | −1.8% | 39% |
| rules gated by ML (best) | selection | base | 19.7% | 0.31 | +0.10% | 0.00% | −1.0% | −2.4% | 52% |
| SOL/XRP rules | **holdout** | base | 24.0% | 1.20 | −0.26% | 0.00% | −2.7% | −4.4% | 25% |

**Notes on the table:**
- The rules row has a higher P(>+0.2%) here than in §1 (33.8% vs 22.3%) because §1 covers all 233 selection windows from May. The ablation uses only the 142 windows from late June, which were a better period for the rules.
- **ML-only:** lower thresholds (E[net] > −0.1% or −0.2%) trade up to 0.9 times a day and lose more (mean −0.07% to −0.18% per window).
- **SOL/XRP with maker exits:** the ML variants were negative with either exit type, so maker-only execution does not rescue those markets.

## 5. Honest recommendation for live

| module | go live? | why |
|---|---|---|
| **Trend rules on USDTTMN/BTCTMN/ETHTMN, maker entries** | **yes, as the core** | The only module that is not negative in the holdout at base fees: +0.06% mean, 37.5% P(>+0.2%), worst DD −3.9% vs −6.9% for buy-and-hold. Around +0.25% at the low fees. |
| ML h48 as a second module | no (optional in shadow) | It adds activity (1.28 → 1.69 trades/day) and cuts no-trade windows (10% → 7%), which helps the activity score. But the holdout mean falls from +0.06% to −0.05%. Run it in shadow only. |
| ML-only | no | Almost never active; negative in the holdout. |
| ML gate on rule entries | no | Fewer trades, lower return (P3a and P3b agree). |
| USDT regime allocation | no | Negative raw and detrended. Low drawdown only because it is mostly flat. |
| SOL/XRP (maker or taker) | no | Negative in the holdout. |

**Two decisions are the user's:**
1. **The activity constraint (≥ 3 trades/day) cannot be met profitably at these fees.** No configuration exceeds about 1.7/day. Each extra round trip of the 500k minimum costs about 0.06% of equity at base fees, so 3/day means about −0.5% per window in fees alone.
   - Activity is worth 15 points; return is worth 90, and a loss scores 0.
   - **My recommendation:** treat ≥ 1 trade/day plus "as few no-trade windows as possible" as the activity rule, rather than paying for activity.
2. **The real prop-account fee** moves every number above more than any model does. At 0.15% / 0.20%, the rules give +0.25% mean in the holdout.

## 6. Phase 4 preview: interfaces ready, nothing built

`agent/context.py` defines the context the LLM roles will see:
- per-market snapshot (returns, volatility, 1h regime, fair-value premium, optional ML E[net]);
- breadth;
- portfolio state;
- a **news** section from a pluggable `NewsFeed` protocol. The default `NullNewsFeed` says explicitly "no news available", so the model never assumes calm.

The strongest LLM role the evidence points to is **macro/news risk for USDTTMN plus trade approval**, because price data cannot time the devaluation jumps. It can only be evaluated in shadow/live, and the report will say so.

## 7. What the 3 trades/day rule costs (`reports/p3-activity_cost`)

The user kept the ≥ 3 round trips/day rule and asked for the cheapest way to reach it and its cost. Design:
- **Main strategy:** the rules keep full size.
- **Extra trades:** small ML trades on USDT/BTC/ETH. Every one has a real, if weak, signal: the model's E[net] above a threshold, held 2h or 4h.
- **Size:** 100k, 250k or 500k TMN. Below our 500k default the minimum order is lowered to the exchange's 50k.
- **Not tested:** pure churn without a signal. It would be cheaper, but it risks being judged "artificial volume", which is grounds for disqualification.

16 configs, chosen on selection windows, holdout run once:

| (base fees) | split | P(>+0.2%) | trades/day | mean | median | p10 DD | worst DD | no-trade | fees/window |
|---|---|---|---|---|---|---|---|---|---|
| rules only | selection | 33.8% | 1.20 | +0.34% | 0.00% | −2.1% | −4.5% | 19% | 0.36% |
| **rules + h24 E[net] > −0.5% at 100k** (chosen, feasible) | selection | 33.8% | 6.52 | +0.15% | −0.13% | −2.1% | −4.0% | 2% | 0.58% |
| rules only | holdout | 37.5% | 1.28 | +0.06% | 0.00% | −2.7% | −3.9% | 10% | 0.41% |
| rules + h24 E[net] > −0.5% at 100k | holdout | **26.0%** | 9.47 | **−0.39%** | −0.45% | −3.3% | −4.6% | 0% | 0.80% |

Fee table of the chosen variant, holdout: low −0.08%, base −0.39%, stress −0.75% mean. For the rules alone: +0.25%, +0.06%, −0.16%.

Other results:
- At 250k the extra trades already make the selection mean negative (−0.11% to −0.20%).
- The 500k variants are rejected at the minimum-order rounding boundary, so they behave like the rules.
- Thresholds of −0.3% or higher do not reach 3/day.

Findings:
- **Cost of the rule:** in the holdout, about **−0.45 pp of mean 3-day return and −11 pp of P(>+0.2%)**. A roughly break-even strategy becomes a losing one, even at the low fees. Since a losing team scores 0 of the 90 return points, the 15 activity points are not worth it.
- **Activity set by a threshold is unstable.** The model's output distribution shifted in the holdout, and the extra trades rose from about 5 a day to 8 a day.
  - A daily quota of extra trades would be the robust version.
  - It has **not** been tested, because tuning it now would mean a second look at this holdout.
  - If wanted, it can only be judged on forward (shadow) data.
- **The extra trades have a negative expected net return,** about −0.5% each before sizing. They are signal-driven, but their purpose is activity, and the decision log will say so. The judges could still read them as volume-padding.

**Bug found while doing this:** `CompositeStrategy._apply_fair_value` replaced every module's `size_fraction` with 1.0 when fair value was disabled. Consequences:
- The ML-only and rules + ML variants in §4 traded at full size instead of confidence-scaled size.
- After the fix (with a regression test), their selection results changed by ≤ 0.02 pp and the conclusions hold.
- Their holdout numbers in §4 are from the run before the fix, kept in `reports/p3-*/final_holdout_run_before_sizing_fix.txt`. The holdout was not re-run, to avoid another look.
- The P2 rules and P3a were not affected: their size was the default 1.0.

## 8. Re-run

```bash
brew install libomp     # macOS, for LightGBM
for e in ml_targets entry_cost rules_grid ml_only rules_plus_ml usdt_allocation usdt_allocation_detrended solxrp_maker; do
  .venv/bin/python manage.py p3_experiment $e
done
.venv/bin/python manage.py p3_experiment rules_plus_ml --final   # holdout: logged, once per model
.venv/bin/python manage.py p3_experiment explain_h48
```

Walk-forward predictions are cached in `.cache/ml/` (keyed by the data manifest), so re-runs take seconds. Every run appends to `reports/experiments.jsonl`.
