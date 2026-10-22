# Changelog

Every change to the trading code after the `live-v1` tag gets an entry here and a new tag (code freeze,
RUNBOOK.md §2). Before go-live, `git log` is the history.

## Unreleased (before live-v1)

- 2026-10-23: AI activity module is the only live module (trend OFF); entry at the best bid after the LLM
  review, 3 min timeout from the order, one reprice; error halts lift after 40 clean loops; the feed clock no
  longer freezes on quiet markets; manual `CHALLENGE_END`; `make go-live` with the G3 smoke test.
