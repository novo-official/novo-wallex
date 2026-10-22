"""Read-only probe of the Wallex API. Prints raw responses so the real shapes can be confirmed
before any parsing code is written.

Usage (from the project root, with .env filled in):
    python scripts/probe_api.py
    python scripts/probe_api.py --symbol BTCTMN --resolution 60 --days 2
    python scripts/probe_api.py --client-id <id> --out scratchpad/probe.txt
    python scripts/probe_api.py --base-url https://api.wallex.ir --save-dir scratchpad/probe_json

Only GET requests are sent. Nothing here can place, cancel or modify an order.

Output is redacted by default (exchange/redact.py): identifiers everywhere, and
balances/ids on /v1/account and /v1/prop responses. The API key is never printed.
Pass --no-redact only for a local look, and never commit that output.

Endpoints marked UNVERIFIED are not in docs/wallex_spot_swagger.json. They are
probed to learn whether they exist; live code must not use them until confirmed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# The official Swagger uses api.wallex.com; the public docs use api.wallex.ir. Probe both.
DEFAULT_BASE_URLS = ["https://api.wallex.ir", "https://api.wallex.com"]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default="BTCTMN", help="API symbol for the UDF history call (default: %(default)s)")
    parser.add_argument("--resolution", default="60", help="UDF resolution, e.g. 1, 5, 15, 60, 1D")
    parser.add_argument("--days", type=float, default=2.0, help="history window in days, ending now")
    parser.add_argument("--client-id", default=None, help="clientOrderId for GET /v1/account/orders/{client_id}")
    parser.add_argument("--depth-symbol", default="BTCTMN", help="symbol for the single-market depth/trades calls")
    parser.add_argument("--out", type=Path, default=None, help="also write the full output to this file")
    parser.add_argument(
        "--save-dir", type=Path, default=None, help="write each JSON body to <dir>/<host>__<label>.json"
    )
    parser.add_argument("--no-redact", action="store_true", help="print identifiers and balances (never commit this)")
    parser.add_argument(
        "--base-url",
        action="append",
        default=None,
        help="API host to probe; repeat for several. Default: api.wallex.ir and api.wallex.com",
    )
    args = parser.parse_args(argv)
    if not args.base_url:
        args.base_url = list(DEFAULT_BASE_URLS)
    return args


def build_targets(args: argparse.Namespace) -> list[tuple[str, str, dict | None]]:
    """(label, path, params) for every read-only endpoint the probe covers."""
    from urllib.parse import quote

    now = int(time.time())
    targets: list[tuple[str, str, dict | None]] = [
        ("markets", "/hector/web/v1/markets", None),
        ("depth/all", "/depth/all", None),
        (
            "udf history",
            "/v1/udf/history",
            {"symbol": args.symbol, "resolution": args.resolution, "from": now - int(args.days * 86400), "to": now},
        ),
        ("open orders", "/v1/account/openOrders", None),
        ("orders", "/v1/account/orders", None),
        ("prop package history", "/v1/prop/user/packages/history", None),
        # Candidates from the public Wallex docs, not in the Swagger file.
        ("UNVERIFIED v1 markets", "/v1/markets", None),
        ("UNVERIFIED v1 depth", "/v1/depth", {"symbol": args.depth_symbol}),
        ("UNVERIFIED v1 trades", "/v1/trades", {"symbol": args.depth_symbol}),
        ("UNVERIFIED currencies stats", "/v1/currencies/stats", None),
        ("UNVERIFIED account balances", "/v1/account/balances", None),
        ("UNVERIFIED account fee", "/v1/account/fee", None),
    ]
    if args.client_id:
        targets.append(("order by client id", f"/v1/account/orders/{quote(args.client_id, safe='')}", None))
    return targets


def format_body(response_text: str, path: str, redact_output: bool) -> tuple[str, object | None]:
    """Pretty-printed body and the parsed (possibly redacted) JSON, or the raw text if it is not JSON."""
    from exchange.redact import is_private_path, redact

    try:
        payload = json.loads(response_text)
    except ValueError:
        return response_text, None
    if redact_output:
        payload = redact(payload, private=is_private_path(path))
    return json.dumps(payload, indent=2, ensure_ascii=False), payload


def slug(text: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in text).strip("_").lower()


def main(argv: list[str]) -> int:
    import django

    django.setup()

    from django.conf import settings

    from exchange.client import WallexClient, WallexError
    from exchange.ratelimit import RateLimiter
    from exchange.redact import scrub_secret

    args = parse_args(argv)
    lines: list[str] = []

    def emit(text: str = "") -> None:
        text = scrub_secret(text, settings.WALLEX_API_KEY)  # the key must never reach stdout or a file
        print(text)
        lines.append(text)

    emit(f"redaction: {'OFF (do not commit this output)' if args.no_redact else 'on'}")

    if not settings.WALLEX_API_KEY:
        emit("NOTE: WALLEX_API_KEY is empty; account endpoints will probably answer 401/403.")

    failures = 0
    for base_url in args.base_url:
        emit("#" * 80)
        emit(f"BASE URL: {base_url}")
        with WallexClient(
            base_url=base_url,
            api_key=settings.WALLEX_API_KEY,
            timeout_seconds=settings.WALLEX_TIMEOUT_SECONDS,
            max_retries=settings.WALLEX_MAX_RETRIES,
            rate_limiter=RateLimiter(settings.WALLEX_REQUESTS_PER_SECOND),
        ) as client:
            for label, path, params in build_targets(args):
                emit("=" * 80)
                emit(f"[{base_url}] {label} | GET {path} | params={params}")
                started = time.monotonic()
                try:
                    response = client.get(path, params)
                except WallexError as exc:
                    failures += 1
                    emit(f"ERROR: {exc}")
                    continue
                elapsed = time.monotonic() - started
                content_type = response.headers.get("content-type")
                emit(f"HTTP {response.status_code} in {elapsed:.2f}s | content-type={content_type}")
                body, payload = format_body(response.text, path, redact_output=not args.no_redact)
                emit(body)
                if args.save_dir and payload is not None:
                    args.save_dir.mkdir(parents=True, exist_ok=True)
                    host = slug(base_url.split("://", 1)[-1])
                    target = args.save_dir / f"{host}__{slug(label)}.json"
                    target.write_text(scrub_secret(body, settings.WALLEX_API_KEY) + "\n", encoding="utf-8")
                if not response.is_success:
                    failures += 1

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nFull output written to {args.out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
