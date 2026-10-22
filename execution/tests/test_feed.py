import pandas as pd

from execution.feed import MarketFeed


def _bars(end: str, minutes: int = 10) -> pd.DataFrame:
    index = pd.date_range(end=pd.Timestamp(end), periods=minutes, freq="1min", tz="UTC")
    return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=index)


def _feed(**bars) -> MarketFeed:
    feed = MarketFeed.__new__(MarketFeed)
    feed.bars, feed.max_quiet_minutes = bars, 120
    return feed


def test_a_quiet_symbol_does_not_freeze_the_clock_and_is_extended_flat():
    feed = _feed(BTCTMN=_bars("2026-10-22T23:05Z"), USDTTMN=_bars("2026-10-22T22:22Z"))
    assert feed.last_bar_close() == pd.Timestamp("2026-10-22T23:06Z")
    extended = feed._extended(feed.bars["USDTTMN"], feed._frontier())
    assert extended.index[-1] == pd.Timestamp("2026-10-22T23:05Z")
    assert (extended.loc["2026-10-22T22:23Z":, "volume"] == 0).all()


def test_a_symbol_stale_beyond_the_limit_holds_the_clock_back():
    feed = _feed(BTCTMN=_bars("2026-10-22T23:05Z"), USDTTMN=_bars("2026-10-22T20:00Z"))
    assert feed.last_bar_close() == pd.Timestamp("2026-10-22T20:01Z")
