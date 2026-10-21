import httpx
import pytest

from exchange.client import WallexAPIError, WallexClient, WallexResponseError, WallexTransportError
from exchange.ratelimit import RateLimiter

BASE_URL = "https://api.test"


class FakeClock:
    """Time only moves when `sleep` is called, so tests are deterministic."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(handler, *, max_retries=3, rate_limiter=None) -> tuple[WallexClient, list[float]]:
    sleeps: list[float] = []
    client = WallexClient(
        BASE_URL,
        "secret-key",
        max_retries=max_retries,
        backoff_seconds=0.5,
        rate_limiter=rate_limiter,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    return client, sleeps


def test_sends_api_key_header_and_parses_json():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "secret-key"
        assert request.url.path == "/hector/web/v1/markets"
        return httpx.Response(200, json={"result": []})

    client, _ = make_client(handler)
    assert client.get_markets() == {"result": []}


def test_udf_history_sends_expected_query_parameters():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/udf/history"
        assert dict(request.url.params) == {"symbol": "BTCTMN", "resolution": "60", "from": "100", "to": "200"}
        return httpx.Response(200, json={})

    client, _ = make_client(handler)
    client.get_udf_history("BTCTMN", "60", 100, 200)


def test_retries_transient_status_then_succeeds():
    responses = iter([503, 503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(responses)
        return httpx.Response(status, json={"ok": status == 200})

    client, sleeps = make_client(handler)
    assert client.get_json("/x") == {"ok": True}
    assert sleeps == [0.5, 1.0]  # exponential backoff


def test_gives_up_after_max_retries_and_reports_last_status():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, text="busy")

    client, sleeps = make_client(handler, max_retries=2)
    with pytest.raises(WallexAPIError) as excinfo:
        client.get_json("/x")
    assert excinfo.value.status_code == 503
    assert len(calls) == 3  # 1 attempt + 2 retries
    assert sleeps == [0.5, 1.0]


def test_client_errors_are_not_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, text="unauthorized")

    client, sleeps = make_client(handler)
    with pytest.raises(WallexAPIError) as excinfo:
        client.get_json("/x")
    assert excinfo.value.status_code == 401
    assert len(calls) == 1
    assert sleeps == []


def test_transport_errors_are_retried_then_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset")

    client, sleeps = make_client(handler, max_retries=1)
    with pytest.raises(WallexTransportError):
        client.get("/x")
    assert sleeps == [0.5]


def test_non_json_success_body_raises_response_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    client, _ = make_client(handler)
    with pytest.raises(WallexResponseError):
        client.get_json("/x")


def test_rate_limiter_spaces_calls_evenly():
    fake = FakeClock()
    limiter = RateLimiter(max_per_second=2, clock=fake.clock, sleep=fake.sleep)
    for _ in range(3):
        limiter.wait()
    assert fake.sleeps == [0.5, 0.5]


def test_rate_limiter_rejects_non_positive_rate():
    with pytest.raises(ValueError):
        RateLimiter(max_per_second=0)
