"""Live data providers used by the fixed itinerary planner.

The live surface is intentionally narrow: three curated CBD museums and the
TfNSW Trip Planner endpoint. User input never controls a URL.
"""

import hashlib
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .models import SYDNEY, Evidence, Leg, Request, Trace
from .rate_limit import gate_for, retry_after

TFNSW_ENDPOINT = "https://api.transport.nsw.gov.au/v1/tp/trip"


class ProviderError(RuntimeError):
    """A live provider could not produce trustworthy data."""


class ProviderAuthError(ProviderError):
    pass


class ProviderRateLimitError(ProviderError):
    pass


class ProviderQuotaError(ProviderRateLimitError):
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
        self.gate = gate_for(api_key)
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
        # Bound each leg's retries; long server cooldowns fail fast and stay shared.
        deadline = time.monotonic() + 25
        for attempt in range(3):
            if not self.gate.acquire(min(deadline, time.monotonic() + 8)):
                raise ProviderRateLimitError("路线服务正在限流，请稍后再试；若持续出现，请检查 TfNSW 配额。")
            fetched_at = datetime.now(timezone.utc).astimezone(SYDNEY)
            try:
                response = self.client.get(
                    TFNSW_ENDPOINT,
                    params=params,
                    headers={"Authorization": f"apikey {self.api_key}", "Accept": "application/json"},
                    timeout=max(0.1, min(20, deadline - time.monotonic())),
                )
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError("TfNSW 路线请求超时") from exc
            except httpx.HTTPError as exc:
                raise ProviderError("TfNSW 路线来源暂时不可用") from exc
            detail = response.headers.get("X-Error-Detail", "").lower()
            if response.status_code in (401, 403, 429) and "quota" in detail:
                self.gate.defer(retry_after(response.headers.get("Retry-After")) or 300)
                raise ProviderQuotaError("TfNSW 路线查询额度已用尽，请检查账户配额并等待恢复。")
            limited = response.status_code == 429 or (
                response.status_code in (401, 403) and "rate limit" in detail
            )
            if limited:
                delay = retry_after(response.headers.get("Retry-After"))
                self.gate.defer(max(0.35, delay if delay is not None else 2 ** attempt))
                if attempt == 2:
                    raise ProviderRateLimitError("路线服务繁忙，重试后仍受限，请稍后再试。")
                continue
            if response.status_code in (401, 403):
                raise ProviderAuthError("TFNSW_API_KEY 无效或没有路线接口访问权限")
            break
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
    ):
        self.request = request
        self.limit = limit
        self.calls = 0
        self.hits = 0
        self.trace: list[Trace] = []
        self.cache: dict[str, dict] = dict(previous or {})
        self.started = time.perf_counter()
        self.routes = routes or TfNSWRouteProvider.from_env()

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
        from .planner import catalog, osm_summary

        found = catalog(self.request)
        self.log("retrieve", "retrieve_candidates", osm_summary(self.request, found))
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
