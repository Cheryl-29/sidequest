from datetime import datetime

import httpx
import pytest
from sidequest.models import SYDNEY, Request
from sidequest.planner import validate
from sidequest.providers import (
    CuratedVenueProvider,
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


def test_live_catalog_includes_free_walkable_parks():
    places = CuratedVenueProvider().retrieve(request())
    parks = {place.id: place for place in places if "公园" in place.category}
    assert {"royal_botanic_garden", "barangaroo_reserve"} <= parks.keys()
    assert all(place.cost == 0 for place in parks.values())
