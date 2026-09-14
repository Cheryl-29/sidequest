"""Live data providers used by the fixed itinerary planner.

The live surface is intentionally narrow: three curated CBD museums and the
TfNSW Trip Planner endpoint. User input never controls a URL.
"""

import hashlib
import math
import os
import time
from datetime import datetime, timedelta, timezone
from datetime import time as clock
from pathlib import Path
from typing import Any

import httpx

from .models import SYDNEY, Candidate, Evidence, Leg, Request, Trace

TFNSW_ENDPOINT = "https://api.transport.nsw.gov.au/v1/tp/trip"
CATALOG_OBSERVED_AT = datetime(2026, 9, 14, 5, 49, 10, tzinfo=timezone.utc)


class ProviderError(RuntimeError):
    """A live provider could not produce trustworthy data."""


class ProviderAuthError(ProviderError):
    pass


class ProviderRateLimitError(ProviderError):
    pass


class ProviderTimeoutError(ProviderError):
    pass


class ProviderSchemaError(ProviderError):
    pass


class NoRouteError(ProviderError):
    pass


class BudgetExhausted(ProviderError):
    pass


def load_local_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value.strip().strip("'\"")
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        if key.strip() == name:
            return raw.strip().strip("'\"")
    return None


def tfnsw_configured() -> bool:
    key = load_local_env("TFNSW_API_KEY") or ""
    return bool(key and not any(char.isspace() for char in key))


def _at(request: Request, hour: int) -> datetime:
    return datetime.combine(request.departure.date(), clock(hour), SYDNEY)


class CuratedVenueProvider:
    """M0-verified venue allowlist.

    Facts remain verified only inside the observation TTL. Once stale, the
    planner can still show a conditional result but cannot claim verification.
    """

    rows = (
        {
            "id": "museum_of_sydney",
            "obviousness": 0.45,
            "name": "Museum of Sydney",
            "category": "博物馆",
            "description": "从城市原点认识悉尼，适合一段安静的室内停留。",
            "lat": -33.8637531,
            "lon": 151.2114804,
            "tags": ["博物馆", "文化", "室内", "历史", "museum"],
            "stay": 45,
            "open": 10,
            "close": 17,
            "weekdays": None,
            "source": "https://mhnsw.au/visit-us/museum-of-sydney/plan-your-visit/",
        },
        {
            "id": "the_mint",
            "obviousness": 0.2,
            "name": "The Mint",
            "category": "历史建筑",
            "description": "在旧铸币厂建筑里看一段城市与公共生活的历史。",
            "lat": -33.8689428,
            "lon": 151.2124305,
            "tags": ["历史", "建筑", "文化", "室内"],
            "stay": 30,
            "open": 9,
            "close": 16,
            "weekdays": {0, 1, 2, 3, 4},
            "source": "https://mhnsw.au/visit-us/the-mint/plan-your-visit/",
        },
        {
            "id": "australian_museum",
            "obviousness": 0.9,
            "name": "Australian Museum",
            "category": "自然史",
            "description": "用自然史和科学展览填满一段好奇心时间。",
            "lat": -33.8743465,
            "lon": 151.2132545,
            "tags": ["博物馆", "自然史", "科学", "室内", "museum"],
            "stay": 60,
            "open": 10,
            "close": 17,
            "weekdays": None,
            "source": "https://australian.museum/visit/admission/",
        },
        {
            "id": "royal_botanic_garden",
            "obviousness": 0.95,
            "name": "Royal Botanic Garden Sydney",
            "category": "公园",
            "description": "从城市走进海港边的植物园，适合随时开始的一段散步。",
            "lat": -33.8642,
            "lon": 151.2166,
            "tags": ["公园", "自然", "散步", "户外", "garden", "park"],
            "stay": 35,
            "open": 7,
            "close": 18,
            "close_by_month": {1: 20, 2: 20, 3: 18.5, 4: 18, 5: 17.5, 6: 17,
                               7: 17, 8: 17.5, 9: 18, 10: 19.5, 11: 20, 12: 20},
            "weekdays": None,
            "source": "https://www.botanicgardens.org.au/royal-botanic-garden-sydney/plan-your-visit",
        },
        {
            "id": "barangaroo_reserve",
            "obviousness": 0.55,
            "name": "Barangaroo Reserve",
            "category": "海港公园",
            "description": "沿海港步道走一圈，在草地和砂岩岸线之间停下来。",
            "lat": -33.8538,
            "lon": 151.2039,
            "tags": ["公园", "自然", "散步", "海港", "户外", "park"],
            "stay": 35,
            "open": 0,
            "close": 24,
            "weekdays": None,
            "source": "https://www.barangaroo.com/precincts/barangaroo-reserve",
        },
    )

    def retrieve(self, request: Request) -> list[Candidate]:
        day_start = _at(request, 0)
        day_end = day_start + timedelta(days=1)
        observed_local = CATALOG_OBSERVED_AT.astimezone(SYDNEY)
        validity_end = max(observed_local, min(day_end, observed_local + timedelta(hours=24)))
        result = []
        for row in self.rows:
            scheduled = row["weekdays"] is None or request.departure.weekday() in row["weekdays"]
            close_value = row.get("close_by_month", {}).get(request.departure.month, row["close"])
            close_hour = int(close_value)
            close_minute = 30 if close_value % 1 else 0
            closes = day_start + timedelta(hours=close_hour, minutes=close_minute)
            evidence = [
                Evidence(
                    id=f"curated-m0:{row['id']}:{field}:20260914",
                    field=field,
                    value=value,
                    source_url=row["source"],
                    fetched_at=observed_local,
                    valid_from=observed_local,
                    valid_until=validity_end,
                    synthetic=False,
                )
                for field, value in (
                    ("hours", f"{row['open']:02}:00–{close_hour:02}:{close_minute:02}"),
                    ("cost", "Free general entry / 免费入场"),
                    ("status", "open" if scheduled else "closed by recurring schedule"),
                )
            ]
            result.append(
                Candidate(
                    id=row["id"],
                    name=row["name"],
                    category=row["category"],
                    description=row["description"],
                    lat=row["lat"],
                    lon=row["lon"],
                    tags=row["tags"],
                    stay=row["stay"],
                    cost=0,
                    obviousness=row["obviousness"],
                    open_at=_at(request, row["open"]) if scheduled else None,
                    close_at=closes if scheduled else None,
                    cancelled=not scheduled,
                    evidence=evidence,
                )
            )
        return result


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_walk(leg: dict[str, Any]) -> bool:
    product = (leg.get("transportation") or {}).get("product", {})
    return product.get("class") == 100 or product.get("name") == "footpath"


def _endpoint_time(journey: dict[str, Any], endpoint: str) -> datetime:
    leg = journey["legs"][0 if endpoint == "departure" else -1]
    point = leg["origin" if endpoint == "departure" else "destination"]
    field = "departureTimePlanned" if endpoint == "departure" else "arrivalTimePlanned"
    return _parse_dt(point[field])


def _general_public(journey: dict[str, Any]) -> bool:
    names = {
        ((leg.get("transportation") or {}).get("product") or {}).get("name")
        for leg in journey.get("legs", [])
    }
    return "School buses" not in names


def _ceil_minute(value: datetime) -> datetime:
    if value.second or value.microsecond:
        value += timedelta(minutes=1)
    return value.replace(second=0, microsecond=0)


def live_route_key(origin: str, dest: str, a: dict, b: dict, departure: datetime) -> str:
    requested = _ceil_minute(departure)
    return (
        f"tfnsw-v1:{origin}:{a['lat']:.6f},{a['lon']:.6f}:"
        f"{dest}:{b['lat']:.6f},{b['lon']:.6f}:{requested.isoformat()}"
    )


class TfNSWRouteProvider:
    def __init__(self, api_key: str, client: httpx.Client | None = None):
        if not api_key or any(char.isspace() for char in api_key):
            raise ProviderAuthError("TFNSW_API_KEY 未配置或格式无效")
        self.api_key = api_key
        self.client = client or httpx.Client(
            timeout=20,
            follow_redirects=False,
            headers={"User-Agent": "Sidequest/0.1"},
        )

    @classmethod
    def from_env(cls, client: httpx.Client | None = None):
        return cls(load_local_env("TFNSW_API_KEY") or "", client)

    def route(
        self,
        origin_id: str,
        destination_id: str,
        origin: dict,
        destination: dict,
        departure: datetime,
    ) -> Leg:
        requested = _ceil_minute(departure)
        params = {
            "outputFormat": "rapidJSON",
            "coordOutputFormat": "EPSG:4326",
            "depArrMacro": "dep",
            "itdDate": requested.strftime("%Y%m%d"),
            "itdTime": requested.strftime("%H%M"),
            "type_origin": "coord",
            "name_origin": f"{origin['lon']}:{origin['lat']}:EPSG:4326",
            "type_destination": "coord",
            "name_destination": f"{destination['lon']}:{destination['lat']}:EPSG:4326",
            "calcNumberOfTrips": 3,
            "TfNSWTR": "true",
        }
        fetched_at = datetime.now(timezone.utc).astimezone(SYDNEY)
        try:
            response = self.client.get(
                TFNSW_ENDPOINT,
                params=params,
                headers={"Authorization": f"apikey {self.api_key}", "Accept": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("TfNSW 路线请求超时") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("TfNSW 路线来源暂时不可用") from exc
        if response.status_code in (401, 403):
            raise ProviderAuthError("TfNSW 拒绝了当前 API key")
        if response.status_code == 429:
            raise ProviderRateLimitError("TfNSW 请求频率受限")
        if not response.is_success:
            raise ProviderError(f"TfNSW 返回 HTTP {response.status_code}")
        try:
            journeys = response.json().get("journeys", [])
            earliest = requested.astimezone(timezone.utc)
            eligible = [
                journey
                for journey in journeys
                if _general_public(journey) and _endpoint_time(journey, "departure") >= earliest
            ]
            if not eligible:
                raise NoRouteError("TfNSW 没有返回请求时刻之后的公共路线")
            selected = min(eligible, key=lambda item: _endpoint_time(item, "arrival"))
            planned_departure = _endpoint_time(selected, "departure").astimezone(SYDNEY)
            planned_arrival = _endpoint_time(selected, "arrival").astimezone(SYDNEY)
            raw_legs = selected["legs"]
        except NoRouteError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ProviderSchemaError("TfNSW 响应结构无法解析") from exc
        duration = math.ceil((planned_arrival - planned_departure).total_seconds() / 60)
        walking = math.ceil(
            sum((leg.get("duration") or 0) for leg in raw_legs if _is_walk(leg)) / 60
        )
        public_legs = [leg for leg in raw_legs if not _is_walk(leg)]
        transfers = selected.get("interchanges")
        if not isinstance(transfers, int):
            transfers = max(0, len(public_legs) - 1)
        mode = "walk" if not public_legs else "transit"
        fingerprint = (
            f"tfnsw-v1:{origin_id}:{destination_id}:{requested.isoformat()}:"
            f"{planned_departure.isoformat()}:{planned_arrival.isoformat()}"
        )
        evidence = Evidence(
            id="tfnsw:" + hashlib.sha256(fingerprint.encode()).hexdigest()[:16],
            field="route",
            value=(
                f"TfNSW 计划路线：{duration} 分钟，步行 {walking} 分钟；"
                "Opal 票价不可由该接口取得"
            ),
            source_url=TFNSW_ENDPOINT,
            fetched_at=fetched_at,
            valid_from=planned_departure,
            valid_until=planned_arrival,
            synthetic=False,
        )
        return Leg(
            origin=origin_id,
            destination=destination_id,
            departure=planned_departure,
            arrival=planned_arrival,
            minutes=duration,
            walking_minutes=walking,
            transfers=transfers,
            fare_aud=None,
            mode=mode,
            evidence=evidence,
        )


class LiveTools:
    def __init__(
        self,
        request: Request,
        limit: int = 20,
        previous: dict | None = None,
        routes: TfNSWRouteProvider | None = None,
        venues: CuratedVenueProvider | None = None,
    ):
        self.request = request
        self.limit = limit
        self.calls = 0
        self.hits = 0
        self.trace: list[Trace] = []
        self.cache: dict[str, dict] = dict(previous or {})
        self.started = time.perf_counter()
        self.routes = routes or TfNSWRouteProvider.from_env()
        self.venues = venues or CuratedVenueProvider()

    def log(self, node: str, action: str, summary: str, cached=False, evidence=None):
        self.trace.append(
            Trace(
                sequence=len(self.trace) + 1,
                node=node,
                action=action,
                summary=summary,
                elapsed_ms=round((time.perf_counter() - self.started) * 1000, 2),
                cache_hit=cached,
                evidence_ids=evidence or [],
            )
        )

    def consume(self):
        if self.calls >= self.limit:
            raise BudgetExhausted("Live Provider 调用预算已耗尽")
        self.calls += 1

    def retrieve(self):
        self.consume()
        if self.request.catalog == "osm":
            from .planner import catalog, osm_summary

            found = catalog(self.request)
            self.log("retrieve", "retrieve_candidates", osm_summary(self.request, found))
            return found
        found = self.venues.retrieve(self.request)
        self.log("retrieve", "retrieve_candidates", f"读取 {len(found)} 个 M0 核验的 CBD 候选")
        return found

    def route(self, origin: str, dest: str, a: dict, b: dict, departure: datetime):
        key = live_route_key(origin, dest, a, b, departure)
        cached = self.cache.get(key)
        if cached:
            leg = Leg.model_validate(cached)
            cache_fresh = datetime.now(SYDNEY) <= leg.evidence.fetched_at + timedelta(minutes=5)
            if (
                cache_fresh
                and leg.evidence.valid_from <= leg.departure <= leg.arrival <= leg.evidence.valid_until
            ):
                self.hits += 1
                self.log(
                    "route",
                    "plan_route",
                    f"复用 {origin} → {dest} 的同一出发时刻 TfNSW 证据",
                    True,
                    [leg.evidence.id],
                )
                return leg
        self.consume()
        leg = self.routes.route(origin, dest, a, b, departure)
        self.cache[key] = leg.model_dump(mode="json")
        self.log(
            "route",
            "plan_route",
            f"{origin} → {dest} · {leg.minutes} 分钟（TfNSW）",
            evidence=[leg.evidence.id],
        )
        return leg
