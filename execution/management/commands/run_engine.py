import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from execution.runner import build_engine


class Command(BaseCommand):
    help = "Run the trading engine: --mode shadow (simulated fills on live data) or --mode live (real orders)."

    def add_arguments(self, parser):
        parser.add_argument("--mode", required=True, choices=["shadow", "live"])
        parser.add_argument("--minutes", type=float, default=None, help="stop after N minutes (tests, smoke runs)")

    def handle(self, *args, **options):
        mode = options["mode"]
        if mode == "live":
            if not settings.LIVE_TRADING_CONFIRMED:
                raise CommandError("live needs LIVE_TRADING_CONFIRMED=true in .env (and the user's go-ahead)")
            from execution.preflight import run_preflight

            restart = (settings.BASE_DIR / "logs" / "engine_state_live.json").exists()
            results = run_preflight("live", restart=restart)
            if not all(ok for _, ok, _ in results):
                raise CommandError("preflight failed; see `manage.py preflight --mode live`")
        engine = build_engine(mode)
        if mode == "live" and engine.s.challenge_end is None:
            raise CommandError("CHALLENGE_END is not set in config/live.toml")
        engine.feed.load(engine.now())
        self.stdout.write(f"engine {mode} started, config {engine.config_hash}, end {engine.s.challenge_end}")
        stop_at = engine.now() + timedelta(minutes=options["minutes"]) if options["minutes"] else None
        while True:
            started = time.monotonic()
            engine.step()
            now = engine.now()
            if stop_at and now >= stop_at:
                break
            if engine.s.challenge_end and now > engine.s.challenge_end + timedelta(minutes=10) and engine.final_checked:
                break
            time.sleep(max(1.0, engine.s.loop_seconds - (time.monotonic() - started)))
        self.stdout.write("engine stopped")
