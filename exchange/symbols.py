"""Market policy: blocklist loader, symbol normalization and default-deny allowlist.

blocked_markets.txt holds one BASE/QUOTE pair per line; '#' starts a comment.
The Wallex API symbol is the same text without the slash (OP/TMN -> OPTMN).

Every order must pass `MarketPolicy.check()` before it is sent. Read-only market
data requests for a symbol must pass `MarketPolicy.check_data()`: the data universe
(`[data] symbols`) may hold markets we never trade, such as the USDT pairs used for
fair value, but never a blocked one. The policy fails closed: unreadable or
malformed files raise instead of loosening the rules.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

_TOKEN_RE = re.compile(r"^[A-Z0-9]+$")


class MarketPolicyError(Exception):
    """A symbol or policy file violates the market policy."""


class MarketBlockedError(MarketPolicyError):
    """The symbol is on the blocklist."""


class MarketNotAllowedError(MarketPolicyError):
    """The symbol is not on the allowlist (default-deny)."""


class MarketNotInDataUniverseError(MarketPolicyError):
    """The symbol is neither in the data universe nor on the allowlist."""


def to_api_symbol(value: str) -> str:
    """Normalize 'OP/TMN', 'op/tmn' or 'OPTMN' to the API symbol 'OPTMN'."""
    if value.count("/") > 1:
        raise MarketPolicyError(f"malformed market: {value!r}")
    symbol = value.strip().upper().replace("/", "")
    if not _TOKEN_RE.match(symbol):
        raise MarketPolicyError(f"malformed market: {value!r}")
    return symbol


def load_blocked_symbols(path: Path) -> frozenset[str]:
    """Read blocked_markets.txt. Empty or malformed files raise (fail closed)."""
    symbols: set[str] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if "/" not in line:
            raise MarketPolicyError(f"{path}:{line_number}: expected BASE/QUOTE, got {raw_line!r}")
        symbols.add(to_api_symbol(line))
    if not symbols:
        raise MarketPolicyError(f"{path}: contains no blocked markets; refusing to start")
    return frozenset(symbols)


@dataclass(frozen=True)
class MarketPolicy:
    """Default-deny policy: a symbol is tradable only if it is allowlisted and not blocked."""

    allowlist: frozenset[str]
    blocked: frozenset[str]
    data_symbols: frozenset[str] = frozenset()  # read-only data universe; never makes a market tradable

    def __post_init__(self) -> None:
        overlap = self.allowlist & self.blocked
        if overlap:
            raise MarketPolicyError(f"allowlisted markets are also blocked: {sorted(overlap)}")
        data_overlap = self.data_symbols & self.blocked
        if data_overlap:
            raise MarketPolicyError(f"data universe contains blocked markets: {sorted(data_overlap)}")

    @property
    def data_universe(self) -> frozenset[str]:
        """Symbols whose market data may be requested: the data universe plus the allowlist."""
        return self.data_symbols | self.allowlist

    def check(self, symbol: str) -> None:
        """Raise unless `symbol` may be traded. Call before every order."""
        api_symbol = to_api_symbol(symbol)
        if api_symbol in self.blocked:
            raise MarketBlockedError(f"{api_symbol} is blocked")
        if api_symbol not in self.allowlist:
            raise MarketNotAllowedError(f"{api_symbol} is not in the allowlist")

    def check_data(self, symbol: str) -> None:
        """Raise unless market data for `symbol` may be requested. Call before every per-symbol data request."""
        api_symbol = to_api_symbol(symbol)
        if api_symbol in self.blocked:
            raise MarketBlockedError(f"{api_symbol} is blocked")
        if api_symbol not in self.data_universe:
            raise MarketNotInDataUniverseError(f"{api_symbol} is not in the data universe")

    def is_tradable(self, symbol: str) -> bool:
        try:
            self.check(symbol)
        except MarketPolicyError:
            return False
        return True


def load_market_policy(trading_config_path: Path, blocked_markets_path: Path) -> MarketPolicy:
    """Build the policy from config/trading.toml and blocked_markets.txt."""
    with trading_config_path.open("rb") as config_file:
        config = tomllib.load(config_file)
    raw_allowlist = config.get("markets", {}).get("allowlist", [])
    if not isinstance(raw_allowlist, list) or not all(isinstance(item, str) for item in raw_allowlist):
        raise MarketPolicyError(f"{trading_config_path}: markets.allowlist must be a list of strings")
    raw_data = config.get("data", {}).get("symbols", [])
    if not isinstance(raw_data, list) or not all(isinstance(item, str) for item in raw_data):
        raise MarketPolicyError(f"{trading_config_path}: data.symbols must be a list of strings")
    return MarketPolicy(
        allowlist=frozenset(to_api_symbol(item) for item in raw_allowlist),
        blocked=load_blocked_symbols(blocked_markets_path),
        data_symbols=frozenset(to_api_symbol(item) for item in raw_data),
    )
