import json
from datetime import UTC, datetime
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from backtest.dataset import load_rules
from exchange.client import WallexClient
from exchange.factory import build_policy
from exchange.schemas import SingleMarketDepthResponse
from execution import smoke
from execution.broker import LiveBroker
from execution.journal import Journal


class Command(BaseCommand):
    help = "G3: one tiny LIMIT BUY 5% under the best bid, read back, cancel, confirm (asks for a typed YES)."

    def add_arguments(self, parser):
        parser.add_argument("--symbol", default="USDTTMN")

    def handle(self, *args, **options):
        symbol = options["symbol"]
        if not settings.WALLEX_API_KEY:
            raise CommandError("WALLEX_API_KEY is empty")
        policy = build_policy()
        policy.check(symbol)
        client = WallexClient(settings.WALLEX_BASE_URL, settings.WALLEX_API_KEY, timeout_seconds=10)
        rules = load_rules(settings.BASE_DIR / "data")[symbol]
        book = SingleMarketDepthResponse.model_validate(client.get_depth(symbol)).result
        best_bid = max(level.price for level in book.bid)
        p = smoke.plan(symbol, Decimal(str(best_bid)), rules)
        self.stdout.write(
            f"Smoke test on {symbol}: LIMIT BUY {p.quantity} @ {p.price} (best bid {p.best_bid}, "
            f"notional {p.notional:.0f} TMN), then cancel. This sends one POST and one DELETE to Wallex."
        )
        if input("Type YES to send it: ").strip() != "YES":
            raise CommandError("not confirmed: nothing was sent")
        journal = Journal(settings.BASE_DIR / "journal" / "smoke", "smoke", "smoke")
        broker = LiveBroker(client, policy, journal)

        def tmn_balance():
            data = client.get_json("/v1/account/balances")
            entry = ((data or {}).get("result") or {}).get("balances", {}).get("TMN", {})
            return {k: entry.get(k) for k in ("value", "locked")}

        result = smoke.run(broker, p, tmn_balance)
        fee = (client.get_json("/v1/account/fee") or {}).get("result", {}).get(symbol)
        report = {
            "time": datetime.now(UTC).isoformat(),
            "symbol": symbol,
            "plan": {"price": str(p.price), "quantity": str(p.quantity), "best_bid": str(p.best_bid)},
            "ok": result.ok,
            "problems": result.problems,
            "steps": result.steps,
            "fee_endpoint": fee,
        }
        path = settings.BASE_DIR / "logs" / f"smoke-{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        for step in result.steps:
            self.stdout.write(json.dumps(step, ensure_ascii=False))
        self.stdout.write(f"fee endpoint: {fee}")
        self.stdout.write(f"report: {path} (local only, not committed)")
        if not result.ok:
            raise CommandError("smoke test FAILED: " + "; ".join(result.problems))
        self.stdout.write(self.style.SUCCESS("smoke test passed: placed, read back, cancelled, confirmed"))
