# Demo evidence (where each item is, all real)

> **خلاصه:** برای هر مورد دمو، آدرس دقیق آن در داشبورد آمده است. همه موارد واقعی‌اند. افسر ریسک در شادو هنوز DEFENSIVE یا OFF نگفته است، پس آن مورد از بازپخش کورکورانه تاریخی (پاسخ‌های واقعی مدل) نشان داده می‌شود. کلمپ RiskGuard هم به‌طور طبیعی رخ نداده است، پس شاهدش تست‌های واحد است.

Dashboard: `make api` + `make dashboard` → http://localhost:3000. Times are UTC; the dashboard shows Tehran time.

| item | where | what it shows |
|---|---|---|
| Trade Agent REJECT with rationale | Decision Timeline, `?mode=shadow`, filter «هوش مصنوعی», 2026-10-22 23:51 UTC (03:21 Tehran), ETHTMN | `rejected by LLM[ollama/qwen3:8b@500a1f067a9f trade_agent_v1@8196a879 #d0f8d290433a]`: the 1h trend is not up, contradicting the signal. The next candidate (USDTTMN) was then approved with a smaller size. |
| Officer DEFENSIVE | Decision Timeline, `?mode=replay`, filter «هوش مصنوعی», 2026-10-15 12:20 UTC | blind historical replay of the live config (real cached model answers, `scripts/export_replay_journal.py`): DEFENSIVE, avoid BTCTMN/ETHTMN, "entries are halved". DEFENSIVE in 24 of 42 replay blocks. **In shadow the Officer has said NORMAL every hour so far.** |
| RiskGuard clamp | `reports/demo/riskguard_tests.txt` (`pytest -v`), `risk/tests/test_guard.py::test_llm_can_only_reduce_risk` | no natural clamp in shadow yet. The tests show: the LLM cannot raise a size (0.5 stays 0.5), a wider stop and a farther or too-close target are ignored, DEFENSIVE halves the size, kill/daily/last-2h/OFF block entries. In shadow, RiskGuard's own sizing is visible on every ENTER (`sized by position cap x 0.13`) and the engine refused entries on USDTTMN after hours without trades (SKIP, 01:56–05:00 UTC). |
| a placed order and its lifecycle | Decision Timeline, `?mode=shadow`, filter «معاملات», ETHTMN 2026-10-23 02:55 → 04:58 UTC | ENTER (Trade Agent approved, size cut to 0.13 of the cap) → maker BUY at the bid 656,111,827 → FILL 02:56 → resting take-profit 661,360,721 → max hold 2 h: TP cancelled, limit at the ask 651,900,006 → 2 min timeout → market sell → CLOSE at 650,564,303, −1,095 TMN. Also BTCTMN 01:56 → 03:57 (+291 TMN) and an entry repriced once then cancelled (ETHTMN 05:01–05:07). |

Tables and charts: `llm_ablation_table.png`, `llm_ablation_windows.png`, `activity_ablation_table.png`,
`fix_check_table.png` (regenerate with `.venv/bin/python scripts/demo_figures.py`).
