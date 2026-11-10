from datetime import datetime

import httpx
import pytest
from sidequest.models import SYDNEY, Request
from sidequest.planner import validate
from sidequest.providers import (
    NoRouteError,
    ProviderAuthError,
    TfNSWRouteProvider,
    live_route_key,
)


def request():
    return Request.model_validate(
        {
            "departure": "2026-09-14T15:00:00+10:00",
            "deadline": "2026-09-14T18:00:00+10:00",
            "mode": "live",
            "catalog": "osm",
        }
    )


def journey(departure, arrival, product="Sydney Buses", duration=300, interchanges=0):
    return {
        "interchanges": interchanges,
        "legs": [
            {
                "duration": duration,
                "origin": {"departureTimePlanned": departure},
                "destination": {"arrivalTimePlanned": arrival},
                "transportation": {"product": {"class": 5, "name": product}},
            }
        ],
    }


def test_tfnsw_selects_fastest_eligible_public_journey_and_keeps_fare_unknown():
    body = {
        "journeys": [
            journey("2026-09-14T05:01:00Z", "2026-09-14T05:08:00Z", "School buses"),
            journey("2026-09-14T05:03:00Z", "2026-09-14T05:20:00Z"),
            journey("2026-09-14T05:04:00Z", "2026-09-14T05:15:00Z", interchanges=1),
        ]
    }

    def handle(provider_request):
        assert provider_request.headers["Authorization"] == "apikey test-key"
        assert provider_request.url.params["itdTime"] == "1500"
        return httpx.Response(200, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    provider = TfNSWRouteProvider("test-key", client)
    leg = provider.route(
        "townhall",
        "museum",
        {"lat": -33.8732, "lon": 151.2067},
        {"lat": -33.8743, "lon": 151.2132},
        request().departure,
    )

    assert leg.departure == datetime(2026, 9, 14, 15, 4, tzinfo=SYDNEY)
    assert leg.arrival == datetime(2026, 9, 14, 15, 15, tzinfo=SYDNEY)
    assert leg.minutes == 11
    assert leg.transfers == 1
    assert leg.fare_aud is None
    assert leg.evidence.synthetic is False
    assert "Opal" in leg.evidence.value

    route_checks = validate(request(), [], [leg])
    assert next(check for check in route_checks if check.name == "route_duration").status == "pass"


def test_tfnsw_school_only_response_is_no_route():
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "journeys": [
                        journey(
                            "2026-09-14T05:01:00Z",
                            "2026-09-14T05:08:00Z",
                            "School buses",
                        )
                    ]
                },
            )
        )
    )
    with pytest.raises(NoRouteError):
        TfNSWRouteProvider("test-key", client).route(
            "a", "b", {"lat": 1, "lon": 2}, {"lat": 3, "lon": 4}, request().departure
        )


def test_tfnsw_auth_failure_has_typed_error():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(401, json={"error": "denied"}))
    )
    with pytest.raises(ProviderAuthError):
        TfNSWRouteProvider("test-key", client).route(
            "a", "b", {"lat": 1, "lon": 2}, {"lat": 3, "lon": 4}, request().departure
        )


def test_live_cache_key_tracks_departure_minute_and_coordinates():
    departure = request().departure
    a = {"lat": 1.0, "lon": 2.0}
    b = {"lat": 3.0, "lon": 4.0}
    key = live_route_key("a", "b", a, b, departure)
    assert key != live_route_key("a", "b", a, b, departure.replace(minute=1))
    assert key != live_route_key("a", "b", a, {"lat": 3.1, "lon": 4.0}, departure)


def test_real_routes_never_run_over_the_synthetic_fixture_catalog():
    with pytest.raises(ValueError, match="合成"):
        Request.model_validate({"departure": "2026-09-14T15:00:00+10:00",
                                "deadline": "2026-09-14T18:00:00+10:00", "mode": "live"})

@pytest.fixture
def provider_clock(monkeypatch):
    from sidequest import rate_limit
    clock = [100.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rate_limit.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(rate_limit, "_gates", {})
    return clock


def call_provider(provider):
    return provider.route("a", "b", {"lat": 1, "lon": 2}, {"lat": 3, "lon": 4}, request().departure)


def test_rate_limit_recovers_and_honors_retry_after(provider_clock):
    calls = []

    def handle(_):
        calls.append(provider_clock[0])
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"journeys": [journey("2026-09-14T05:01:00Z", "2026-09-14T05:08:00Z")]})

    provider = TfNSWRouteProvider("retry-key", httpx.Client(transport=httpx.MockTransport(handle)))
    assert call_provider(provider).minutes == 7
    assert calls == [100, 102]


def test_rate_limit_stops_after_three_attempts(provider_clock):
    from sidequest.providers import ProviderRateLimitError
    calls = []

    def handle(_):
        calls.append(provider_clock[0])
        return httpx.Response(429)

    with pytest.raises(ProviderRateLimitError):
        call_provider(TfNSWRouteProvider("retry-key", httpx.Client(transport=httpx.MockTransport(handle))))
    assert calls == [100, 101, 103]


def test_long_cooldown_shared_across_provider_instances(provider_clock):
    from sidequest.providers import ProviderRateLimitError
    calls = []

    def handle(_):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "120"})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    for _ in range(2):
        with pytest.raises(ProviderRateLimitError):
            call_provider(TfNSWRouteProvider("shared-key", client))
    assert len(calls) == 1


def test_quota_does_not_retry(provider_clock):
    from sidequest.providers import ProviderQuotaError
    calls = []

    def handle(_):
        calls.append(1)
        return httpx.Response(403, headers={"X-Error-Detail": "Account Over Quota Limit"})

    with pytest.raises(ProviderQuotaError, match="额度"):
        call_provider(TfNSWRouteProvider("quota-key", httpx.Client(transport=httpx.MockTransport(handle))))
    assert len(calls) == 1


def test_gate_spacing_and_deadline(provider_clock):
    from sidequest.rate_limit import gate_for
    gate = gate_for("key")
    assert gate is gate_for("key")
    assert gate.acquire(101)
    assert gate.acquire(101)
    assert provider_clock[0] == pytest.approx(100.35)
    assert not gate.acquire(100.5)


def test_retry_after_formats():
    from datetime import timedelta, timezone
    from email.utils import format_datetime

    from sidequest.rate_limit import retry_after
    assert retry_after("invalid") is None
    assert retry_after("NaN") is None
    assert retry_after("-1") == 0
    assert retry_after("3") == 3
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60))
    assert 58 < retry_after(future) <= 60
