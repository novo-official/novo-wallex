# Wallex Agentic Trading Challenge: rules and judging

> **خلاصه:** این فایل مرجع رسمی قوانین چالش Wallex در هکاتون «Agentic AI» استارکوچ است. یک موتور معاملاتی مبتنی بر هوش مصنوعی باید ۳ روز روی حساب اعتباری ۵ میلیون تومانی Wallex، فقط اسپات و بدون اهرم، کاملاً خودکار معامله کند. سود باید در پایان به تومان نقد شده باشد. داوری ۳۰۰ امتیاز دارد: ۱۵۰ امتیاز معاملاتی (بازده، افت سرمایه، فعالیت، پایداری) و ۱۵۰ امتیاز فنی (منطق استراتژی، نقش واقعی AI، مدیریت ریسک، قابلیت اجرای مجدد، بک‌تست و شدو تست). تیم بازنده در بخش بازده صفر می‌گیرد.

Source: the official challenge brief given to NOVO Team. This file is the
project's source of truth for the rules and the judging criteria. If the
organizers publish a change, update this file first and note the date.

## Goal

Build an AI-based algorithmic trading engine that:

- ingests and analyzes Wallex market data;
- finds opportunities based on an explicit strategy;
- decides buy/sell with no human intervention and executes automatically on Wallex;
- manages capital and risk;
- is fast, auditable and reproducible, so the judges can re-run it.

Profit from one lucky trade does not count. UI/UX is not scored.

Prize for the top team: 80M TMN (separate from the hackathon prize). The team
also keeps any net trading profit.

## Account

- Each team gets a dedicated "trade-credit" (prop) sub-account loaded with **5,000,000 TMN**. Withdrawals are disabled.
- If the team loses, it pays nothing. If it profits, it receives the net profit. Settlement is about 2 days after the end.
- **Spot only, no leverage.**
- **Trading window: 3 days.**

## Return

```
Return % = (final net account value − 5,000,000) / 5,000,000 × 100
```

- Includes realized P&L, fees, the value of remaining assets and unrealized P&L.
- **The profit must be realized in TMN at the end.** Do not finish holding coins whose value depends on price. Performance is judged on realized results.

## Prohibited (disqualification)

- Manual trades: every order must come from the algorithm.
- Self-trading.
- Coordinated trading between team members' accounts.
- Wash trading or artificial volume.
- Trading outside the allowed markets.

## Allowed markets

Every Wallex spot market except those in `blocked_markets.txt` (project root,
format `BASE/QUOTE`; the API symbol is the same text without "/", e.g.
`OP/TMN` → `OPTMN`). Major markets such as BTCTMN, ETHTMN, USDTTMN, SOLTMN,
XRPTMN, DOGETMN, TRXTMN, ADATMN, BNBTMN, LTCTMN, SHIBTMN and their USDT pairs
are not blocked. Every symbol is still checked against the file in code.

## Required deliverables

- A demo video of about 5 minutes (mandatory; quality counts). It shows outputs and behavior, not code.
- A GitHub repository with **exactly** the code that traded.
- A `.md` document covering: architecture and core logic; how AI and the Wallex tools are used; buy/sell conditions; capital and risk management; how to re-run and evaluate.
- **A decision log for every trade**: what signal it saw and why it bought or sold. Judges also use it to detect manual trades.
- Team members must be able to explain the algorithm fluently. Judges may ask for a re-run.

## Tools allowed

Wallex API, Wallex MCP (Model Context Protocol, for connecting AI agents to
Wallex), and any AI models or frameworks.

## Judging (300 points)

Stage 1 ranks teams by return. Stage 2 is a technical review of the top teams.
A positive return without technical approval does not win.

| Area | Criterion | Points |
|---|---|---|
| Trading (150) | Net % return, ranked among teams. **Losing teams get 0.** | 90 |
| | Drawdown control during the challenge (less drawdown, more points) | 30 |
| | Algorithm activity: regular, reasonable trading over the 3 days | 15 |
| | Execution stability: few errors, correct orders, no crashes | 15 |
| Technical (150) | Strategy logic: we can explain why and on what basis we buy/sell | 40 |
| | Real AI role in decisions (not if/else with an AI label) | 35 |
| | Risk and capital management: order size caps, stop loss, never all-in | 30 |
| | Re-runnable: clean code, run guide, judges can re-run it | 25 |
| | Backtest and shadow test done and results shown | 20 |

## What we optimize (team decision)

A **high probability of a small positive 3-day return with low drawdown**, plus
strong technical evidence. Not maximum backtest profit, and not high-variance
bets. A losing run scores 0 of 90 return points, so the sign of the return
matters more than its size. Fees matter: a taker round trip costs roughly
0.5–0.7%, which rules out naive scalping.

## Open questions to the organizers

Asked, not yet answered (defaults in use are in `CLAUDE.md`):

- Exact maker/taker fees for the prop account.
- Minimum order size for the prop account (the public `/v1/markets` shows `minNotional` 50,000 TMN / 1 USDT).
- A balance endpoint for the prop sub-account.
- The official Wallex MCP endpoint.
- Exact `CHALLENGE_START` / `CHALLENGE_END` (Asia/Tehran).
