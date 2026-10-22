# Phase 3a (first exploration): ML entry filter over rule entries

> Superseded by docs/RESEARCH_P3.md (per-bar ML, new objective). Kept as evidence; its runs are in reports/experiments.jsonl.

> **خلاصه:** یک مدل LightGBM با برچسب triple-barrier، اعتبارسنجی walk-forward با purge، و کالیبراسیون isotonic ساخته شد و روی همان پنجره‌های ۳ روزه با قوانین تنها مقایسه شد. نتیجهٔ صادقانه: ML بازده را بهتر نمی‌کند.
> - برچسب هم‌راستا با خروج‌ها (۳ روز، ۲٪/۶٪) قدرت پیش‌بینی پایداری ندارد: AUC در selection ‏۰٫۵۷ و در holdout ‏۰٫۴۵.
> - برچسب کوتاه (۴ ساعت، ۱٪/۲٪) مهارت پایدار دارد (AUC ‏۰٫۵۴ تا ۰٫۶۶ در هر ۹ بلوک)، ولی معاملهٔ ۴ ساعته حتی در بهترین پنجک هم بعد از کارمزد زیان‌ده است.
> - به‌عنوان فیلتر ورود، ML کیفیت هر معامله را ۲ تا ۴ برابر می‌کند، ولی آن‌قدر معامله را کم می‌کند که بازدهٔ پنجره بالا نمی‌رود.
> - در حالت tilt (کاهش حجم به‌جای حذف)، افت سرمایه کمتر می‌شود، ولی کنترل تصادفی با همان مقدار کاهش حجم تقریباً همین اثر را دارد. پس این بهبود از «حجم کمتر» است، نه از هوشمندی.
>
> تصمیم: استراتژی canonical فعلاً فقط قوانین است و ML خاموش است. نقش AI در P4 باید بازطراحی شود (بخش ۵).

Artifacts: `reports/ml-2026-10-22/` (summary, ablation, walk-forward blocks, feature importance,
windows, chart) and `models/aligned_3d/` (final model of the chosen label, with its metadata).

## 1. Method

- **Population:** the bars the rules trade from. 1h uptrend regime (close > EMA200, EMA50 > EMA200, EMA50 rising), one sample per 30 min. Training uses all 11 TMN markets for more variety (about 34k samples). Prediction and trading are on USDTTMN/BTCTMN/ETHTMN only.
- **Features (37, `ml/features.py`):** all causal and price-free (returns, distances to EMAs, RSI/ADX on 15m/1h/4h, Bollinger position, ATR/close, volume surge, active share, fair-value z and premium). They also include market context (USDTTMN and BTCTMN returns and trend, breadth of the 11 markets) and Tehran calendar (hour, Thursday, Friday). A lookahead test checks truncated == full at T.
- **Labels (`ml/labels.py`):** triple barrier with the backtester's rules. The entry is the maker limit; the stop is a taker exit with slippage and wins ties; the target fills only if a bar trades through it; the time exit is a taker exit at the close. The label is 1 when the fee-adjusted return is above 0.
  - `aligned_3d` uses the trading exits: 2% stop, 6% target, 3 days.
  - `timing_4h` uses 1% / 2% / 4 h. A short horizon has many more independent samples.
- **Walk-forward (`ml/model.py`):** after 45 days of training, the model is refit every 14 days. A training sample is used only if its label was resolved before the block starts (purging). Isotonic calibration is fitted on the latest 20% of the purged training data. Hyperparameters are fixed and conservative (15 leaves, ≥ 200 rows per leaf, 300 rounds at lr 0.03) and are not tuned.
- **Output:** `ml_edge` = calibrated P(win) − the training base rate of that block's model. Using the edge rather than the raw probability matters, because the base rate swings 10–70% between blocks.
- **Ablation (`ml/study.py`):**
  - **Variants:** rules alone, rules + ML filter (block below a threshold), filter + sizing, and tilt. Tilt doesn't block an entry below the threshold; it takes it at 0.55× size.
  - **Grid:** 2 labels × 4 thresholds.
  - **Windows:** the same 3-day windows as P2. Only windows that start after the first ML prediction are used, for every variant (142 selection, 96 holdout).
  - **Objective:** as agreed after P2, the mean 3-day return, subject to p10 drawdown ≥ −4% and ≥ 40% positive windows.
  - **Random control:** a filter that passes the same share of rule triggers at random (5 seeds).

## 2. Is there predictive skill? (out of sample, traded symbols)

| label | AUC selection | AUC holdout | AUC per block | Brier vs base rate |
|---|---|---|---|---|
| aligned_3d (2%/6%/3d) | 0.570 | **0.450** | 0.32–0.68, unstable | 0.276 vs 0.249 (worse) |
| timing_4h (1%/2%/4h) | 0.527 | **0.594** | 0.54–0.66, above 0.5 in 9/9 blocks | 0.183 vs 0.174 (worse) |

- **The 3-day label has no stable skill.** Labels in one period share one future, so the effective sample size is about the number of independent 3-day periods (~50). The label mostly records the regime, which the model cannot foresee.
- **The 4h label has small but consistent skill.** As a standalone trade, though, even the top quintile of predictions loses after fees (−0.36% per trade in selection, −0.47% in holdout).
- **Most useful features:** distance of USDTTMN from its 1h EMA200, 4h ADX, BTC's 1-day return, activity and volume, and breadth. These are market context more than symbol-specific signals.

## 3. Ablation on 3-day windows (base fees, same windows for every variant)

| variant | selection mean / positive / worst DD | holdout mean / positive / worst DD |
|---|---|---|
| rules | **+0.34%** / 40% / −4.5% | **+0.06%** / 43% / −3.9% |
| rules + ML hard filter (timing_4h, t=+0.10) | +0.20% / 23% / −1.8% | +0.07% / 15% / −2.4% |
| rules + ML tilt (chosen: aligned_3d, t=+0.15) | +0.26% / 41% / −2.9% | +0.04% / 45% / −2.6% |
| random tilt control (5 seeds) | +0.20% / 42% / −3.4% | +0.01% / 41% / −3.1% |

Fee stress ×1.5 keeps the order: rules +0.13% / −0.16%, ML tilt +0.12% / −0.10% (selection / holdout).

**Trade quality vs quantity (hard filter, base fees):**

| | return per trade, selection | holdout | win rate sel / hold |
|---|---|---|---|
| rules | 0.095% of equity | 0.016% | 51% / 43% |
| random filter, same pass rate | 0.076% | 0.028% | 50% / 43% |
| ML hard filter | **0.192%** | **0.065%** | **62% / 50%** |

## 4. Conclusion

1. **The ML filter picks better trades.** Per-trade return is 2–4× that of the rules and of the random control, in both periods. But it vetoes so many entries that 68–72% of windows have no trade, so the window return does not improve.
2. **The soft version (tilt) lowers drawdown,** worst −2.6% vs −3.9% in holdout. But a random tilt with the same size cut lowers it nearly as much, so the improvement is mostly *smaller positions*. Its return is slightly below the rules.
3. **Net:** ML adds no return and no drawdown benefit that a plain size cut wouldn't give. Following the brief ("if it adds nothing, say so"), the canonical strategy stays **rules-only** (`[strategy]` has no `ml` section, so ML is disabled). The pipeline, the model and the evidence stay in the repo.

## 5. Implications for the AI role (input to Phase 4)

What the evidence supports:
- **Costs, not prediction, are the binding constraint.** No ML/LLM component can beat a 0.6% round trip with these signals. The real fee of the prop account (still unknown) changes this more than any model.
- **Context features (USDTTMN trend, BTC, breadth) are the most informative.** That fits an AI that judges *regime and risk* more than one that picks entries.
- **The ML layer shows small but real selection skill.** It is best kept as an input to a decision, not as an automatic veto.

Proposed redesign for P4. It needs the user's approval:
- **LLM Regime/Risk Officer:** sets `risk_mode` and per-symbol bias from a cross-market summary that includes the ML edge as one input. It can only *reduce* risk (RiskGuard enforces the ceiling). Its value is evaluated on the drawdown/return trade-off against the random-size-cut control, not on return alone.
- **LLM Trade Agent:** reviews each rule entry with read-only WallexTools (order book, recent trades). It can veto anomalies that historical data cannot capture, such as a thin book, a spread spike or a stale pair. Its effect can only be measured in shadow and live, which the report will say plainly.

## 6. Re-run

```bash
brew install libomp                       # macOS only; Docker image installs libgomp1
.venv/bin/python manage.py ml_study --out reports/ml-check     # about 25 s on 8 cores
```
