"""Preflight: every condition live trading needs, each PASS/FAIL with a reason. Live refuses to start on any FAIL."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime

from django.conf import settings


def run_preflight(mode: str, restart: bool = False) -> list[tuple[str, bool, str]]:
    """`restart`: the supervisor restarting a live engine that has a state file. Then our own resting orders are
    expected (the engine restores and reconciles them) and the last hour is still allowed (the flatten must run)."""
    from exchange.client import WallexAPIError, WallexClient
    from exchange.factory import build_policy

    from .broker import LiveBroker, parse_balances
    from .journal import Journal
    from .runner import TRADED, build_engine, challenge_end, load_live_config

    results: list[tuple[str, bool, str]] = []

    def check(name: str, function) -> None:
        try:
            ok, detail = function()
        except Exception as exc:  # noqa: BLE001 - every failure is reported, never raised
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append((name, bool(ok), str(detail)))

    live = load_live_config()
    key = settings.WALLEX_API_KEY
    client = WallexClient(settings.WALLEX_BASE_URL, key, timeout_seconds=settings.WALLEX_TIMEOUT_SECONDS)
    check("api key", lambda: (bool(key) or mode == "shadow", "set" if key else "missing (shadow only)"))

    def policy():
        policy = build_policy()
        bad = [s for s in TRADED if not policy.is_tradable(s)]
        return not bad, f"allowlist {sorted(policy.allowlist)}; not tradable: {bad}"

    check("market policy", policy)

    def end():
        moment = challenge_end(live)
        if moment is None:
            return False, "challenge end missing: set CHALLENGE_END in .env or [challenge] end in config/live.toml"
        left = moment - datetime.now(moment.tzinfo)
        needed = timedelta(0) if restart else timedelta(hours=1)
        return left > needed, f"{moment.isoformat()} ({left.total_seconds() / 3600:.1f} h left)"

    check("challenge end", end)

    def clock():
        response = client.get("/v1/markets")
        server = parsedate_to_datetime(response.headers["date"])
        drift = abs((datetime.now(server.tzinfo) - server).total_seconds())
        return drift < 3, f"drift {drift:.1f} s vs server Date header"

    check("clock drift", clock)

    if key:

        def balances():
            values = parse_balances(client.get_json("/v1/account/balances"))
            tmn = values.get("TMN", Decimal(0))
            coins = {a: str(v) for a, v in values.items() if a != "TMN" and v > 0}
            return tmn > 0, f"TMN {tmn}; other non-zero assets: {coins or 'none'}"

        check("balances", balances)

        def open_orders():
            orders = LiveBroker(
                client, build_policy(), Journal(settings.BASE_DIR / "logs", "preflight", "-")
            ).open_orders()
            if restart:
                return True, f"{len(orders)} open orders (restart: restored from the state file and reconciled)"
            return not orders, f"{len(orders)} open orders on the account"

        check("no open orders", open_orders)

        def prop():
            try:
                data = client.get_json("/v1/prop/user/packages/history")
            except WallexAPIError as exc:
                if exc.status_code != 401:
                    raise
                # Verified 2026-10-23: this endpoint wants a web login, not the API key. Then the challenge end
                # must be set by hand (RUNBOOK.md §3) and liquidity_limit stays unknown (the -2.5% kill switch
                # is far above any plausible prop floor).
                manual = bool(challenge_end(live))
                return manual, "401 (needs a web login): " + (
                    "using the manual challenge end; liquidity_limit unknown" if manual else "set CHALLENGE_END"
                )
            rows = data.get("result") or []
            rows = rows if isinstance(rows, list) else rows.get("data", [rows])
            active = [r for r in rows if str(r.get("status", "")).lower() in ("active", "running", "open", "1")] or rows
            row = active[0] if active else {}
            limit = row.get("liquidity_limit")
            return bool(row), (
                f"status {row.get('status')}, liquidity_limit {limit}, warning_limit "
                f"{row.get('warning_limit')}, expire_at {row.get('expire_at')}"
            )

        check("prop package", prop)

    def ollama():
        if not (live["llm"]["officer"] or live["llm"]["trade_agent"]):
            return True, "LLM disabled"
        from agent.replay import providers_for

        provider = providers_for("live-local")[0]
        return True, f"{provider.identity.model} @ {provider.identity.digest[:12]}"

    check("local LLM (pinned digest)", ollama)

    def database():
        from django.db import connection

        connection.ensure_connection()
        return True, "connected"

    check("database", database)

    def engine():
        built = build_engine("shadow")
        return True, f"config hash {built.config_hash}"

    check("config hash / engine build", engine)
    if mode == "live":
        check(
            "code committed",
            lambda: (
                "-dirty" not in build_engine("shadow").config_hash,
                "engine/strategy/risk/agent/config have no uncommitted changes",
            ),
        )
    return results
