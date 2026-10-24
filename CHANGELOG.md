# Changelog

Every change to the trading code after the `live-v1` tag gets an entry here and a new tag (code freeze,
RUNBOOK.md §2). Before go-live, `git log` is the history.

## live-v2 stopped: 2026-10-24 17:01 Tehran (team decision, before the 2026-10-26 23:00 end)

- live-v2 ran 2026-10-24 10:12 → 17:01 Tehran on `f6878ec` (AI activity + smc_long). No trade: in every slot the
  Officer's avoid list or the Trade Agent rejected the candidates; no SMC setup reached the live module.
- The Mac slept 10:54–15:50 (lid closed, on battery): network errors halted entries, which resumed by themselves
  at 15:59 after 40 clean loops.
- Two engine crashes at 16:57 and 16:58 Tehran while writing the state file (`persist`, exit status 120); the
  supervisor restarted within 10 s and the state was restored (restart-safe path of live-v1.1); nothing was open.
  Cause not established (the disk was 98% full at the time).
- Stopped by the team: live and shadow engines and the orderbook recorder, with no position and no open order.
  Account at the stop: 4,989,101 TMN + 0.0000085 ETH dust (≈ 5,400 TMN), −0.11% vs 5,000,000 (all from
  2026-10-23's run and smoke tests).

## live-v2 (2026-10-24, team decision: SMC alongside AI activity; tagged at go-live)

- `smc_long` (ICT: 1h bullish structure, 15m sweep, displacement + BOS, FVG/OB retrace entry, stop under the
  sweep, swing-high target ≥ 1.5 R, discount only) runs **alongside** AI activity on BTCTMN/ETHTMN, variant
  `p2fvgob_t24`. **It failed the research gate** (docs/RESEARCH_SMC.md: 14 days −0.15% at 0.14 trades/day); it is
  enabled by the team's explicit decision, not by the evidence.
- Every SMC entry goes through the Officer, the Trade Agent (which now sees named SMC fields: pattern, sweep
  depth, target distance, R, discount) and RiskGuard; 250k TMN per trade like AI activity; same risk limits.
- The SMC features are computed on the live 1m candles with the same `strategy.smc.smc_on_clock` as the
  backtest; new setups are journaled as SMC_SETUP. Code moved from research/smc into strategy/ (frozen paths).
- Includes live-v1.2 (repriced take-profit rounding, P&L cost basis).

## live-v1.2 (2026-10-24, bug fixes found in the live run; not deployed yet)

- A repriced entry's take-profit (and stop) were not rounded to the price step: Wallex rejected the take-profit
  (422, "price has more than 0 decimals"), so trade 2 of 2026-10-23 had no take-profit (the stop still worked).
  The reprice now rounds the take-profit down and the stop up, and every LIMIT sell is floored to the price step
  right before it is sent.
- The journal's approximate P&L used the position quantity, which the reconcile had set to the leftover balance
  while the stop's sell was running (+193,323 TMN instead of about −3,250). P&L now uses the position's cost basis;
  the reconcile no longer shrinks a position whose exit order is working.

## live-v1.1 deployed: 2026-10-23 12:33 Tehran (controlled restart, tag `live-v1.1-deploy`)

- Switched the running live engine from live-v1 to live-v1.1 by a controlled restart: no open position, no resting
  order, 10.4 h to the end. Reason: **safety fixes only** (restart-safe preflight, loss-limit references kept
  across restarts, stale-state guard). **No strategy or risk change**: the trading code is live-v1.1; the deploy
  commit adds only the inert `[smc] enabled = false` block and `config/smc.toml`, which live does not read.

## live-v1.1 (2026-10-23, safety fix while live-v1 runs; the running process is not restarted)

- A supervisor restart of the live engine could not come back: its preflight failed on our own resting
  take-profit ("no open orders") and, in the last hour, on "challenge end > 1 h", leaving a position without
  stops. A restart (state file present) now accepts our own open orders (restored and reconciled) and only
  needs the end to be in the future.
- After a restart without an open position the kill switch's reference was reset to the current equity, and the
  day's starting equity was not saved: both loss limits could forget losses made before a crash. The start
  equity, the day's start equity and the daily block are now kept across restarts.
- go-live refuses to start while a state file from an earlier live run exists (it would be restored).

## live-v1 (2026-10-23 11:38 Tehran): first live start


- 2026-10-23 (safety, user-approved): USDTTMN removed from the live AI activity markets (`config/live.toml`):
  the prop account refuses orders on it (422 «امکان ثبت سفارش در بازار USDTTMN وجود ندارد»). Live trades BTCTMN
  and ETHTMN only (both passed the place/cancel smoke test). USDTTMN stays in the data universe (features, fair
  value). Not a tuning change: candidates on USDTTMN could never have been filled.

- 2026-10-23 (smoke test on the prop account): MARKET orders are refused (422), so every forced sell (stop, max
  hold, flatten fallback, kill switch) is an aggressive LIMIT 0.2% under the best bid that expires after 30 s and
  is re-placed at the new bid until closed; the engine never sends MARKET. USDTTMN orders are refused too.

- 2026-10-23: every SELL (take-profit, stop, max-hold, flatten, kill switch) goes through one path: the
  available balance read at that moment, floored to the amount step, never the bought quantity; below the
  minimum order it is DUST (logged, not sold, left out of the ≥ 99% TMN check but reported); a rejected sell is
  retried once with a fresh, smaller balance. Shadow rejects oversells too. The smoke test adds one real BTCTMN
  round trip (typed ROUNDTRIP) that measures the fee currency and rates and aborts go-live if the sell cannot
  close.

- 2026-10-23: AI activity module is the only live module (trend OFF); entry at the best bid after the LLM
  review, 3 min timeout from the order, one reprice; error halts lift after 40 clean loops; the feed clock no
  longer freezes on quiet markets; manual `CHALLENGE_END`; `make go-live` with the G3 smoke test.
