"""Minimal rate limiter: spaces calls at least 1/max_per_second apart."""

import time
from collections.abc import Callable


class RateLimiter:
    """Single-threaded spacing limiter. `clock` and `sleep` are injectable for tests."""

    def __init__(
        self,
        max_per_second: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_per_second <= 0:
            raise ValueError("max_per_second must be positive")
        self._interval = 1.0 / max_per_second
        self._clock = clock
        self._sleep = sleep
        self._next_allowed = 0.0

    def wait(self) -> None:
        """Block until the next call is allowed, then reserve that slot."""
        now = self._clock()
        start = max(now, self._next_allowed)
        if start > now:
            self._sleep(start - now)
        self._next_allowed = start + self._interval
