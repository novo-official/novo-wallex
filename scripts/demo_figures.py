"""Demo figures (reports/demo/*.png) from the committed reports: the LLM ablation window chart and tables, and the
live-config activity ablation table. Usage: .venv/bin/python scripts/demo_figures.py"""

import json
import shutil
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

OUT = Path("reports/demo")


def pct(value: float, digits: int = 2) -> str:
    return f"{value * 100:+.{digits}f}%"


def table_png(
    path: Path, title: str, columns: list[str], rows: list[list[str]], note: str, highlight: int | None, wide: int = 1
) -> None:
    """`wide`: how many leading columns hold labels (they get more width)."""
    fig, ax = plt.subplots(figsize=(14, 0.8 + 0.42 * (len(rows) + 2)))
    ax.axis("off")
    ax.set_title(title, fontsize=13, fontweight="bold", loc="left")
    rest = (0.66 if wide == 2 else 0.72) / (len(columns) - wide)
    widths = ([0.09, 0.25] if wide == 2 else [0.28]) + [rest] * (len(columns) - wide)
    table = ax.table(cellText=rows, colLabels=columns, loc="center", cellLoc="center", colWidths=widths)
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 1.5)
    for (r, _c), cell in table.get_celld().items():
        cell.set_edgecolor("#d0d7de")
        if r == 0:
            cell.set_facecolor("#24292f")
            cell.get_text().set_color("white")
            cell.get_text().set_fontweight("bold")
        elif highlight is not None and r == highlight + 1:
            cell.set_facecolor("#ddf4ff")
    fig.text(0.01, 0.02, note, fontsize=8.5, color="#57606a", wrap=True)
    fig.savefig(path, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def ablation() -> None:
    fees = pd.read_csv("reports/p4-llm_ablation/fee_table.csv")
    windows = pd.read_csv("reports/p4-llm_ablation/windows.csv")
    control = pd.read_csv("reports/p4-ablation_size_control/fee_table.csv")
    control_w = pd.read_csv("reports/p4-ablation_size_control/windows.csv")
    rows = []
    for split in ("selection", "holdout"):
        for variant in ("rules", "rules + officer", "rules + officer + trade agent"):
            r = fees[(fees.variant == variant) & (fees.split == split) & (fees.fees == "base")].iloc[0]
            w = windows[(windows.variant == variant) & (windows.split == split) & (windows.cost_case == "base")]
            rows.append(
                [
                    split,
                    variant,
                    f"{r.p_above_target:.0%}",
                    pct(r.mean_return),
                    pct(r.median_return),
                    pct(r.p10_drawdown),
                    pct(r.worst_drawdown),
                    pct(w["return"].min()),
                    f"{w.avg_exposure.mean():.3f}",
                ]
            )
        if split == "selection":
            r = control[(control.variant != "rules") & (control.fees == "base")].iloc[0]
            w = control_w[(control_w.variant != "rules") & (control_w.cost_case == "base")]
            rows.append(
                [
                    "selection",
                    "control: rules x0.76 size, no LLM",
                    f"{r.p_above_target:.0%}",
                    pct(r.mean_return),
                    pct(r.median_return),
                    pct(r.p10_drawdown),
                    pct(r.worst_drawdown),
                    pct(w["return"].min()),
                    f"{w.avg_exposure.mean():.3f}",
                ]
            )
    table_png(
        OUT / "llm_ablation_table.png",
        "LLM ablation on the trend rules: 3-day windows (13 selection + 6 holdout), base fees",
        ["split", "variant", "P(>+0.2%)", "mean", "median", "p10 DD", "worst DD", "worst win.", "exposure"],
        rows,
        "The LLM roles did not improve returns; they cut drawdown mostly by trading smaller (see the equal-exposure "
        "control). Local qwen3:8b, blind replay, every answer cached (make reproduce). docs/RESEARCH_P4.md section 5.",
        highlight=3,
        wide=2,
    )
    shutil.copy("reports/p4-llm_ablation/windows.png", OUT / "llm_ablation_windows.png")


def activity() -> None:
    s = json.loads(Path("reports/ai_activity_llm/summary.json").read_text())
    rows = []
    for name, r in s["results"].items():
        t = r["per_trade"]
        rows.append(
            [
                name,
                pct(r["return"]),
                pct(r["max_drawdown"]),
                f"{r['trades']} ({r['trades_per_day']:.2f}/d)",
                f"{t['mean_pnl_tmn']:+,.0f}",
                pct(t["mean_gross_pct_notional"]),
                pct(t["mean_net_pct_notional"]),
                f"{r['fees_paid']:,.0f}",
            ]
        )
    order = ["ML only (proxy)", "ML + Officer", "ML + Officer + Trade Agent (live config)"]
    rows.sort(key=lambda row: order.index(row[0]))
    table_png(
        OUT / "activity_ablation_table.png",
        "Live config (AI activity) with and without the LLM roles: last 7 days, measured fees (TMN 0.25% / 0.30%)",
        [
            "variant",
            "return",
            "max DD",
            "trades",
            "P&L/trade TMN",
            "gross/trade",
            "net/trade",
            "fees TMN",
        ],
        rows,
        f"Trade Agent approved {s['trade_agent_approved']} of {s['trade_agent_reviews']} candidates. The gain comes "
        "from fewer and smaller trades, not better selection (net per trade is not better). 11-20 trades: small "
        "sample. docs/RESEARCH_P4.md section 6.",
        highlight=2,
    )


def fix_check() -> None:
    s = json.loads(Path("reports/ai_fix_check/summary.json").read_text())
    names = {
        "a_current": "(a) current live config",
        "b_edge_positive": "(b) only E[net] > 0",
        "c_edge_positive_15m_up": "(c) (b) + 15m trend up",
    }
    rows = []
    for key, label in names.items():
        m, x, low = (s["results"][f"{key}/{f}"] for f in ("measured", "measured_x1.5", "low"))
        rows.append(
            [
                label,
                pct(m["return"]),
                pct(x["return"]),
                pct(low["return"]),
                pct(m["max_drawdown"]),
                f"{m['trades_per_day']:.2f}",
                pct(m["worst_3d"]),
            ]
        )
    table_png(
        OUT / "fix_check_table.png",
        "AI activity variants, last 14 days, ML-only proxy (no LLM)",
        ["variant", "measured fees", "x1.5", "low 0.15/0.20", "max DD", "trades/day", "worst 3 days"],
        rows,
        "Chosen after seeing (a) on the same data: optimistic. Decision: (a) with both LLM roles (>= 1 trade/day).",
        highlight=0,
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ablation()
    activity()
    fix_check()
    print(sorted(p.name for p in OUT.glob("*.png")))


if __name__ == "__main__":
    main()
