from pathlib import Path

import pytest
from django.conf import settings
from django.core import checks

from exchange.checks import check_market_policy
from exchange.symbols import (
    MarketBlockedError,
    MarketNotAllowedError,
    MarketPolicy,
    MarketPolicyError,
    load_blocked_symbols,
    load_market_policy,
    to_api_symbol,
)


def test_to_api_symbol_removes_slash_and_normalizes_case():
    assert to_api_symbol("OP/TMN") == "OPTMN"
    assert to_api_symbol(" op/tmn ") == "OPTMN"
    assert to_api_symbol("OPTMN") == "OPTMN"


@pytest.mark.parametrize("bad", ["", "/", "OP/T/MN", "OP-TMN", "OP TMN"])
def test_to_api_symbol_rejects_malformed_input(bad):
    with pytest.raises(MarketPolicyError):
        to_api_symbol(bad)


def test_repository_blocklist_loads_and_contains_known_entries():
    blocked = load_blocked_symbols(settings.BLOCKED_MARKETS_FILE)
    assert {"OPTMN", "IOTMN", "IOUSDT", "2ZTMN"} <= blocked


def test_blocklist_ignores_comments_and_blank_lines(tmp_path: Path):
    path = tmp_path / "blocked.txt"
    path.write_text("# header\n\nOP/TMN  # inline comment\n", encoding="utf-8")
    assert load_blocked_symbols(path) == frozenset({"OPTMN"})


@pytest.mark.parametrize("content", ["# only comments\n", "", "OPTMN\n", "OP/TMN/X\n"])
def test_blocklist_fails_closed_on_empty_or_malformed_file(tmp_path: Path, content):
    path = tmp_path / "blocked.txt"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(MarketPolicyError):
        load_blocked_symbols(path)


def test_policy_is_default_deny():
    policy = MarketPolicy(allowlist=frozenset({"BTCTMN"}), blocked=frozenset({"OPTMN"}))
    assert policy.is_tradable("BTC/TMN")
    assert not policy.is_tradable("ETHTMN")  # not allowlisted
    assert not policy.is_tradable("OP/TMN")  # blocked


def test_check_raises_blocked_before_not_allowed():
    policy = MarketPolicy(allowlist=frozenset({"BTCTMN"}), blocked=frozenset({"OPTMN"}))
    with pytest.raises(MarketBlockedError):
        policy.check("OP/TMN")
    with pytest.raises(MarketNotAllowedError):
        policy.check("ETHTMN")


def test_allowlisted_and_blocked_market_is_rejected_at_construction():
    with pytest.raises(MarketPolicyError):
        MarketPolicy(allowlist=frozenset({"OPTMN"}), blocked=frozenset({"OPTMN"}))


def test_check_rejects_malformed_symbol():
    policy = MarketPolicy(allowlist=frozenset({"BTCTMN"}), blocked=frozenset())
    with pytest.raises(MarketPolicyError):
        policy.check("BTC/TMN/X")


def test_load_policy_normalizes_allowlist(tmp_path: Path):
    config = tmp_path / "trading.toml"
    config.write_text('[markets]\nallowlist = ["btc/tmn", "ETHTMN"]\n', encoding="utf-8")
    blocked = tmp_path / "blocked.txt"
    blocked.write_text("OP/TMN\n", encoding="utf-8")

    policy = load_market_policy(config, blocked)

    assert policy.allowlist == frozenset({"BTCTMN", "ETHTMN"})
    assert policy.blocked == frozenset({"OPTMN"})


def test_load_policy_rejects_non_list_allowlist(tmp_path: Path):
    config = tmp_path / "trading.toml"
    config.write_text('[markets]\nallowlist = "BTCTMN"\n', encoding="utf-8")
    blocked = tmp_path / "blocked.txt"
    blocked.write_text("OP/TMN\n", encoding="utf-8")
    with pytest.raises(MarketPolicyError):
        load_market_policy(config, blocked)


def test_repository_config_allowlist_is_default_deny():
    policy = load_market_policy(settings.TRADING_CONFIG_FILE, settings.BLOCKED_MARKETS_FILE)
    assert policy.allowlist == frozenset(
        {
            "USDTTMN",
            "BTCTMN",
            "ETHTMN",
            "SOLTMN",
            "XRPTMN",
            "DOGETMN",
            "TRXTMN",
            "ADATMN",
            "BNBTMN",
            "LTCTMN",
            "SHIBTMN",
        }
    )
    assert policy.is_tradable("BTC/TMN")
    assert not policy.is_tradable("ZECTMN")  # blocked
    assert not policy.is_tradable("ETHUSDT")  # not allowlisted


def test_startup_check_reports_missing_blocklist(settings, tmp_path: Path):
    settings.BLOCKED_MARKETS_FILE = tmp_path / "missing.txt"
    errors = check_market_policy(app_configs=None)
    assert len(errors) == 1
    assert errors[0].id == "exchange.E001"
    assert errors[0].level == checks.ERROR


def test_startup_check_is_clean_for_repository_config():
    assert check_market_policy(app_configs=None) == []


def test_startup_check_warns_on_empty_allowlist(settings, tmp_path: Path):
    config = tmp_path / "trading.toml"
    config.write_text("[markets]\nallowlist = []\n", encoding="utf-8")
    settings.TRADING_CONFIG_FILE = config
    assert [message.id for message in check_market_policy(app_configs=None)] == ["exchange.W001"]
