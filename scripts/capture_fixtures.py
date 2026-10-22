"""Capture real Wallex responses into tests/fixtures/wallex/ (public endpoints only, GET only).

Usage (from the project root):
    python scripts/capture_fixtures.py
    python scripts/capture_fixtures.py --base-url https://api.wallex.ir --out-dir tests/fixtures/wallex

Every per-symbol request passes the market policy first (default-deny), so a
blocked or non-allowlisted market is never requested. Account endpoints are not
touched, so the files contain no identifiers or balances.

Files written:
    depth_v1_allowlist.json   {symbol: GET /v1/depth?symbol=... response} for every allowlisted market
    markets_v1_subset.json    GET /v1/markets with only the symbols listed in SUBSET_EXTRA + allowlist + USDT pairs
    trades_v1_btctmn.json     GET /v1/trades?symbol=BTCTMN
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Kept in the markets subset to test the policy flags: blocked TMN markets and one USDT market.
SUBSET_EXTRA = ("ZECTMN", "DASHTMN", "GRAMTMN", "BTCUSDT")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="https://api.wallex.ir")
    parser.add_argument("--out-dir", type=Path, default=PROJECT_ROOT / "tests" / "fixtures" / "wallex")
    parser.add_argument("--trades-symbol", default="BTCTMN")
    return parser.parse_args(argv)


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {path} ({path.stat().st_size} bytes)")


def main(argv: list[str]) -> int:
    import django

    django.setup()

    from django.conf import settings

    from exchange.client import WallexClient
    from exchange.factory import build_policy
    from exchange.ratelimit import RateLimiter

    args = parse_args(argv)
    policy = build_policy()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    print(f"capture started {datetime.now(UTC).isoformat()} from {args.base_url}")

    # Public endpoints only: no API key is sent, so nothing account-specific can come back.
    with WallexClient(
        base_url=args.base_url,
        api_key="",
        timeout_seconds=settings.WALLEX_TIMEOUT_SECONDS,
        max_retries=settings.WALLEX_MAX_RETRIES,
        rate_limiter=RateLimiter(settings.WALLEX_REQUESTS_PER_SECOND),
    ) as client:
        depth: dict[str, object] = {}
        for symbol in sorted(policy.allowlist):
            policy.check(symbol)
            depth[symbol] = json.loads(client.get("/v1/depth", {"symbol": symbol}).text)
        write_json(args.out_dir / "depth_v1_allowlist.json", depth)

        markets = json.loads(client.get("/v1/markets").text)
        usdt_pairs = {symbol.removesuffix("TMN") + "USDT" for symbol in policy.allowlist if symbol != "USDTTMN"}
        keep = set(policy.allowlist) | usdt_pairs | set(SUBSET_EXTRA)
        markets["result"]["symbols"] = {
            symbol: info for symbol, info in markets["result"]["symbols"].items() if symbol in keep
        }
        write_json(args.out_dir / "markets_v1_subset.json", markets)

        policy.check(args.trades_symbol)
        trades = json.loads(client.get("/v1/trades", {"symbol": args.trades_symbol}).text)
        write_json(args.out_dir / f"trades_v1_{args.trades_symbol.lower()}.json", trades)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
