from datetime import datetime
from zoneinfo import ZoneInfo

from execution.runner import challenge_end


def test_env_challenge_end_wins_over_config_and_defaults_to_tehran(settings):
    live = {"challenge": {"end": "2026-10-30T12:00:00+03:30"}}
    settings.CHALLENGE_END = "2026-10-26T12:00:00"
    assert challenge_end(live) == datetime(2026, 10, 26, 12, tzinfo=ZoneInfo("Asia/Tehran"))
    settings.CHALLENGE_END = ""
    assert challenge_end(live).isoformat() == "2026-10-30T12:00:00+03:30"
    assert challenge_end({"challenge": {"end": ""}}) is None
