# Changelog

Every change to the trading code after the `live-v1` tag gets an entry here and a new tag (code freeze,
RUNBOOK.md §2). Before go-live, `git log` is the history.

## Unreleased (before live-v1)

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
