from exchange.redact import REDACTED, is_private_path, redact, scrub_secret


def test_identifiers_are_redacted_everywhere():
    payload = {"result": {"orders": [{"clientOrderId": "abc-123", "price": "1", "client_id": "x"}]}}
    order = redact(payload, private=False)["result"]["orders"][0]
    assert order == {"clientOrderId": REDACTED, "price": "1", "client_id": REDACTED}


def test_balances_are_redacted_only_on_private_paths():
    package = {"result": [{"start_balance": "5000000", "liquidity_limit": "4000000", "status": "active", "id": 7}]}
    assert redact(package, private=True)["result"][0] == {
        "start_balance": REDACTED,
        "liquidity_limit": REDACTED,
        "status": "active",
        "id": REDACTED,
    }
    market = {"id": 7, "value": "1"}
    assert redact(market, private=False) == market  # public market data keeps its numbers


def test_empty_values_stay_visible_so_shapes_can_be_read():
    assert redact({"client_id": "", "balance": None}, private=True) == {"client_id": "", "balance": None}


def test_private_paths():
    assert is_private_path("/v1/account/balances")
    assert is_private_path("/v1/prop/user/packages/history")
    assert not is_private_path("/v1/depth")


def test_scrub_secret_removes_the_key_from_text():
    assert scrub_secret("x-api-key: s3cr3t!", "s3cr3t!") == f"x-api-key: {REDACTED}"
    assert scrub_secret("nothing", "") == "nothing"
