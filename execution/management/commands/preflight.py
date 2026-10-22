from django.core.management.base import BaseCommand, CommandError

from execution.preflight import run_preflight


class Command(BaseCommand):
    help = "Check every live-trading precondition; exits non-zero if any check fails."

    def add_arguments(self, parser):
        parser.add_argument("--mode", default="live", choices=["shadow", "live"])

    def handle(self, *args, **options):
        results = run_preflight(options["mode"])
        for name, ok, detail in results:
            self.stdout.write(f"{'PASS' if ok else 'FAIL'}  {name:<28} {detail}")
        if not all(ok for _, ok, _ in results):
            raise CommandError("preflight failed")
