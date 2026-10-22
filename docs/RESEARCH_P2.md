# Phase 2 research: data, edge and the 3-day window study

> **خلاصه:** ۱۸۰ روز کندل ۱ دقیقه‌ای برای ۲۱ بازار دانلود و با هش در `data/` ثبت شد. اوردربوک هم از ۲۲ اکتبر ضبط می‌شود. بک‌تستر رویدادمحور با فرض‌های محافظه‌کارانه پر شدن سفارش، و هزینه‌های اندازه‌گیری‌شده از اوردربوک واقعی، ساخته شد. نتیجهٔ صادقانه: با کارمزد پیش‌فرض (۰٫۲۵٪/۰٫۳۵٪) هیچ سیگنال تکنیکال کوتاه‌مدتی بعد از هزینه سودآور نیست. سیگنال‌های روند فقط روی BTCTMN، ETHTMN و USDTTMN از ورود تصادفی بهترند. استراتژی منتخب تقریباً سربه‌سر است: میانگین +۰٫۱۲٪ در هر پنجرهٔ ۳ روزهٔ انتخاب و +۰٫۰۶٪ در holdout، با افت سرمایهٔ کم (بدترین ‎−۳٫۹٪‎ در holdout). شرط «۶۰٪ پنجره‌های مثبت» برآورده نمی‌شود. نتیجه به کارمزد حساس است: با ۰٫۱۰٪/۰٫۱۵٪ میانگین به حدود +۰٫۳٪ می‌رسد. کارمزد واقعی حساب ما مهم‌ترین مجهول است.

All numbers come from committed artifacts and can be re-run (commands at the end).

## 1. Data

| What | Detail |
|---|---|
| Candles | 1m, 2026-04-25 → 2026-10-22 (180 days), 21 symbols: 11 TMN markets and their 10 USDT pairs. About 259k bars per symbol. `data/candles/1m/*.parquet`, sha256 in `data/MANIFEST.json`. |
| Why 1m only | `/v1/udf/history` accepts only `1, 15, 60, 240, 720, 1D`. `15` returns 1m bars, `240`/`720` return 1h bars. `60` returns only the latest 696 bars. A 1m window wider than about 34 h silently returns **one** bar, so we use 24 h chunks with a truncation guard. 5m/15m/1h/4h are resampled locally. |
| Gaps | The server fills minutes without trades with zero-volume bars. A few windows had missing minutes (USDTTMN 2 windows, e.g. 821/1440 bars on 2026-10-19). Missing minutes become flat zero-volume bars (`fill_gaps`), and resampled buckets keep only complete periods. |
| Liquidity | Zero-volume minutes: BTCTMN 38%, BNBTMN 83%, ADATMN 80%, ADAUSDT 95%. These markets are thin. |
| Orderbook | Recorder since 2026-10-22 08:51 UTC: top-20 levels every 10 s, trades, tickers. It is the source of the slippage model. |

**The Toman drift.** USDTTMN rose **+76%** in these 180 days (155k → 270k). Every TMN price carries this drift: BTCTMN +86%, BNBTMN +104%. Results over this period are flattered by the devaluation, so every table shows buy-and-hold benchmarks. The drift is concentrated between 04:00 and 09:00 Tehran time, and Fridays are negative (both in each half of the data). That is too small against costs to trade directly, so it is kept as a candidate ML feature.

## 2. Costs

- **Fees** (defaults until confirmed): maker 0.25%, taker 0.35%; fee stress ×1.5.
- **Slippage:** median of (half spread + book impact) for a 1M TMN taker order, measured on the recorded book (`config/slippage.json`). In bps: USDTTMN 2.0, ETHTMN 3.2, BTCTMN 4.0, TRXTMN 10.5, SOLTMN 11.9, XRPTMN 12.1, BNBTMN 12.5, DOGETMN 12.6, ADATMN 16.5, LTCTMN 21.4, SHIBTMN 42.5.
- **Round trip:** a maker entry plus taker exit on BTCTMN costs about 0.64%. A random entry loses about that much (event-study baseline).
- **MARKET orders** exist only on USDTTMN, BTCTMN and ETHTMN (and XAUT). Every other market needs limit orders for exits.

## 3. Event study: is there any edge? (`reports/edge-2026-10-22`)

Each signal event is simulated as an independent trade with the backtester's fill rules. The first 70% of time is selection; the last 30% is holdout. Mean net return per trade, rule stop 2% / target 6% / max 3 days, maker entry:

| signal | BTCTMN sel / hold | ETHTMN sel / hold | USDTTMN sel / hold | altcoins |
|---|---|---|---|---|
| random entry (baseline) | −0.50 / +0.08 | −0.40 / −0.06 | −0.27 / +0.52 | −0.5 to −1.3 |
| 1h uptrend | +0.53 / +0.41 | +0.56 / −0.04 | +0.04 / −0.07 | mostly negative |
| 1h+4h uptrend + 5m breakout | +0.68 / −0.03 | +0.46 / +0.74 | +0.36 / +0.39 | mostly negative |
| mean reversion (range) | +0.22 / −0.12 | −2.62 / +0.85 | +0.62 / −1.11 | 1–9 events |

Findings:
- **Tight exits never pay.** With a 1% stop and 2% target, every signal is negative on every symbol. The edge needs wide exits, because the round trip is about 0.6%.
- **Trend signals beat random entries by about 1% per trade (selection) and about 0.3% (holdout), but only on BTCTMN and ETHTMN, plus USDTTMN with the 4h filter.** ADA, DOGE, LTC, SHIB, TRX and XRP are negative in every signal and both splits.
- **Statistical strength is weak:** about 25 trades per symbol in selection, with a standard error of about 0.5%/trade.
- **Mean reversion** has too few events and no consistent sign. It is dropped.
- **Fair value** (TMN price vs USDT pair × USDTTMN) as an entry filter is mixed (helps ETH in holdout, hurts BTC). It moves to the ML layer as a feature rather than a hard rule.

## 4. Portfolio backtest and 3-day window study (`reports/windows-2026-10-22`)

**Fill and risk rules:**
- **Fills (conservative):** a limit fills only if price trades through it, and the stop wins a same-bar tie. A stop in the entry bar triggers, and gaps fill at the open. The window is flat in TMN at the end: no entries in the last 2 h, taker exits in the last 60 min.
- **Risk:** at most 20% of equity per position and 60% total, 0.5% risk per trade, stops at most 2%. The minimum order is 500k TMN. There is a daily loss limit of −3% (Tehran day) and a kill switch at −6%.

**Windows:** independent runs from 5M TMN, starting every 12 h. Selection has 233 windows (2026-05-05 → 08-29); holdout has 96 windows (09-01 → 10-19), and none overlap the selection windows.

**Grid** (48 combos):
- symbols {USDT/BTC/ETH, +SOL/BNB};
- 4h filter {on, off};
- regime exit {1h, 4h, none};
- target {2R, 3R};
- trailing stop {off, after 2R}.

Every combination has a fixed 2% stop.

**Objective:** maximize the median 3-day return, subject to at least 60% positive windows and a p10 drawdown of at least −4%. **No combination meets the 60% constraint.** The fallback is the best mean return among combinations that meet the drawdown constraint. That is params #22: USDT/BTC/ETH, 1h-uptrend + 5m breakout, fixed 2% stop, 6% target, no trail, no regime exit, held until the end of the window.

| 3-day windows | positive | mean | median | p10 | p90 | p10 DD | worst DD | trades | no-trade windows |
|---|---|---|---|---|---|---|---|---|---|
| selection, base fees | 30% | +0.12% | 0.00% | −0.97% | +1.69% | −1.9% | −4.5% | 2 | 30% |
| selection, fee ×1.5 | 26% | −0.03% | 0.00% | −1.17% | +1.38% | −2.1% | −4.8% | 2 | 30% |
| **holdout, base fees** | **43%** | **+0.06%** | 0.00% | −1.84% | +1.94% | −2.7% | **−3.9%** | 3.5 | 10% |
| holdout, fee ×1.5 | 36% | −0.16% | −0.08% | −2.25% | +1.69% | −3.1% | −4.3% | 3.5 | 10% |
| buy & hold USDTTMN, holdout | 50% | +0.46% | −0.01% | −2.62% | +4.18% | −4.9% | −6.9% | 1 | – |
| buy & hold BTCTMN, holdout | 51% | +0.85% | +0.30% | −3.80% | +5.81% | −5.5% | −9.6% | 1 | – |

**Fee sensitivity** (same strategy, mean per window, selection / holdout):

| maker / taker | 0.25 / 0.35% | 0.15 / 0.20% | 0.10 / 0.15% | 0.05 / 0.10% | 0 / 0 |
|---|---|---|---|---|---|
| mean | +0.12 / +0.06% | +0.25 / +0.25% | +0.30 / +0.33% | +0.35 / +0.40% | +0.43 / +0.51% |
| positive windows | 30 / 43% | 35 / 46% | 36 / 46% | 38 / 46% | 40 / 46% |
| p10 drawdown | −1.9 / −2.7% | −1.8 / −2.5% | −1.7 / −2.5% | −1.7 / −2.4% | −1.7 / −2.4% |

**What this means:**
1. With default fees, the rules alone are **about break-even with low drawdown**. They beat buy-and-hold on drawdown (worst −3.9% vs −6.9%/−9.6%), not on return.
2. The 60%-positive target is out of reach for a trend strategy with this cost structure. It wins rarely and big; 10–30% of windows have no trade (return exactly 0), and buy-and-hold itself is only about 50% positive.
3. **The real fee of our prop account decides a lot.** At 0.15%/0.20% or lower, the same rules give about +0.25–0.5% per window.
4. The rules-only result is the baseline for the AI ablation (P3/P4). The ML/LLM layers have to earn their place by raising the share of good entries.

## 5. Bugs found by the evidence

- **The breakeven/trailing stop after +1R cut winners.** The first full run had zero take-profits. It is now optional and off.
- **Floor-rounding a 2% stop to price precision made it slightly wider than 2%.** The risk layer then rejected those entries. Stops now round up (tighter); there is a regression test.

## 6. Re-run

```bash
.venv/bin/python manage.py import_candles          # verifies sha256 against data/MANIFEST.json (needs the DB)
.venv/bin/python manage.py edge_study --out reports/edge-2026-10-22
.venv/bin/python manage.py window_study --out reports/windows-2026-10-22   # about 35 s on 8 cores
.venv/bin/python manage.py run_backtest --start 2026-10-19T00:00:00Z --end 2026-10-22T00:00:00Z
```

The backtest reads only `data/` and `config/`: no network, and no DB except for `import_candles`. Config hash of the chosen params: `899129576f1a…`.
