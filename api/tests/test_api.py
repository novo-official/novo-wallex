import json

import pytest

from api import sources


@pytest.fixture
def base(tmp_path):
    (tmp_path / "journal" / "shadow").mkdir(parents=True)
    (tmp_path / "logs").mkdir()
    rows = [
        {"time": "2026-10-23T00:00:00+00:00", "kind": "START", "equity": "5000000"},
        {"time": "2026-10-23T00:05:00+00:00", "kind": "LLM", "role": "regime_officer", "risk_mode": "NORMAL"},
        {"time": "2026-10-23T00:06:00+00:00", "kind": "FILL", "symbol": "USDTTMN", "price": "100"},
        {
            "time": "2026-10-23T00:30:00+00:00",
            "kind": "CLOSE",
            "symbol": "USDTTMN",
            "price": "101",
            "pnl_tmn_approx": "500",
            "role": "take_profit",
        },
    ]
    (tmp_path / "journal" / "shadow" / "decisions-20261023.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )
    (tmp_path / "logs" / "engine_state_shadow.json").write_text(json.dumps({"equity": "5000500", "positions": {}}))
    return tmp_path


def test_overview_reads_state_trades_and_the_last_officer_decision(base):
    view = sources.overview(base, "shadow")
    assert view["state"]["equity"] == "5000500"
    assert view["officer"]["risk_mode"] == "NORMAL"
    assert view["trades"][0]["pnl_tmn"] == "500" and view["trades"][0]["entry_price"] == "100"
    assert view["counts"]["START"] == 1 and view["running"] is False


def test_decisions_are_newest_first_and_filterable(base):
    rows = sources.decisions(base, "shadow", kinds={"FILL", "CLOSE"})
    assert [r["kind"] for r in rows] == ["CLOSE", "FILL"]


@pytest.mark.django_db
def test_api_endpoints(client):
    assert client.get("/api/overview?mode=shadow").status_code == 200
    assert client.get("/api/decisions?mode=nope").status_code == 400
