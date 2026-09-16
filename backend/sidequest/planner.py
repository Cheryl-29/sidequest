import hashlib
import itertools
import math
import time
from datetime import datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode

from .fixtures import ORIGINS, candidates
from .models import Candidate, Check, Evidence, Itinerary, Leg, Request, Result, Status, Stop, Trace
from .providers import BudgetExhausted, CuratedVenueProvider, LiveTools, NoRouteError

# A stop added around a locked one must be walkable from it (~13 minutes); farther away it is
# a second destination, not something done on the way.
SIDE_KM = 1.0


class PlannerTools(Protocol):
    calls: int
    hits: int
    trace: list[Trace]
    cache: dict[str, dict]
    started: float

    def log(self, node: str, action: str, summary: str, cached=False, evidence=None): ...
    def retrieve(self) -> list[Candidate]: ...
    def route(self, origin: str, dest: str, a: dict, b: dict, departure: datetime) -> Leg: ...


def distance(a: dict, b: dict) -> float:
    """Haversine distance, used for coarse ordering and SYNTHETIC fixture routes only."""
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat, dlon = lat2 - lat1, math.radians(b["lon"] - a["lon"])
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.asin(min(1, math.sqrt(value)))


def route_key(origin: str, dest: str, departure: datetime) -> str:
    # Exact departure time matters for public transport. Never key by endpoints alone.
    return f"replay-v1:{origin}:{dest}:{departure.isoformat()}"


def request_origin(request: Request) -> dict:
    if request.origin_id == "current":
        return {
            "name": "当前位置",
            "lat": request.origin_lat,
            "lon": request.origin_lon,
        }
    return ORIGINS[request.origin_id]


def google_maps_url(request: Request, order: tuple[Candidate, ...], legs: list[Leg]) -> str:
    origin = request_origin(request)
    coordinate = f"{origin['lat']},{origin['lon']}"
    params = {
        "api": "1",
        "origin": coordinate,
        "destination": coordinate,
        "waypoints": "|".join(f"{candidate.lat},{candidate.lon}" for candidate in order),
        "travelmode": "walking" if all(leg.mode == "walk" for leg in legs) else "transit",
    }
    return "https://www.google.com/maps/dir/?" + urlencode(params)


def catalog(request: Request) -> list[Candidate]:
    """The one place that decides where candidates come from, for planner and agent alike.
    The agent locks an id from this list and the planner must find the same id again."""
    if request.catalog == "osm":
        from .places import discover

        return discover(request)
    if request.mode == "replay":
        return candidates(request)
    return CuratedVenueProvider().retrieve(request)


def osm_summary(request: Request, found: list[Candidate]) -> str:
    from .places import radius_for

    return f"从 OSM 快照检索出发点 {radius_for(request):.1f} km 内的地点，保留 {len(found)} 个"


class ReplayTools:
    def __init__(self, request: Request, limit: int = 20, previous: dict | None = None):
        self.request = request
        self.limit = limit
        self.calls = 0
        self.hits = 0
        self.trace: list[Trace] = []
        self.cache: dict[str, dict] = dict(previous or {})
        self.started = time.perf_counter()

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
            raise BudgetExhausted("回放工具调用预算已耗尽")
        self.calls += 1

    def retrieve(self):
        self.consume()
        if self.request.catalog == "osm":
            found = catalog(self.request)
            self.log("retrieve", "retrieve_candidates", osm_summary(self.request, found))
            return found
        found = candidates(self.request)
        self.log(
            "retrieve", "retrieve_candidates", f"读取 {len(found)} 个合成候选；固定策略 fixed-v1"
        )
        return found

    def route(self, origin: str, dest: str, a: dict, b: dict, departure: datetime):
        key = route_key(origin, dest, departure)
        if key in self.cache:
            leg = Leg.model_validate(self.cache[key])
            if leg.evidence.valid_from <= departure <= leg.evidence.valid_until:
                self.hits += 1
                self.log(
                    "route",
                    "plan_route",
                    f"复用 {origin} → {dest} 的同一出发时刻证据",
                    True,
                    [leg.evidence.id],
                )
                return leg
        self.consume()
        km = distance(a, b)
        walking = max(4, math.ceil(km * 1.3 / 4.5 * 60))
        mode = "walk" if walking <= 24 else "transit"
        wait = (10 - departure.minute % 10) % 10 if mode == "transit" else 0
        minutes = walking if mode == "walk" else math.ceil(km / 18 * 60) + 10 + wait
        evidence = Evidence(
            id="route:" + hashlib.sha256(key.encode()).hexdigest()[:16],
            field="route",
            value=f"合成{mode}路段 {minutes} 分钟；不是实际路线",
            source_url="https://example.org/sidequest/synthetic-fixtures-v1",
            fetched_at=self.request.departure,
            valid_from=departure,
            valid_until=departure + timedelta(minutes=minutes),
        )
        leg = Leg(
            origin=origin,
            destination=dest,
            departure=departure,
            arrival=departure + timedelta(minutes=minutes),
            minutes=minutes,
            walking_minutes=minutes if mode == "walk" else 8,
            mode=mode,
            fare_aud=0 if mode == "walk" else 3,
            evidence=evidence,
        )
        self.cache[key] = leg.model_dump(mode="json")
        self.log(
            "route",
            "plan_route",
            f"{origin} → {dest} · {minutes} 分钟（合成）",
            evidence=[evidence.id],
        )
        return leg


def validate(request: Request, stops: list[Stop], legs: list[Leg]) -> list[Check]:
    """Full-chain validation. Unknown evidence never produces a pass."""
    checks = []

    def add(name, state, detail, evidence=()):
        checks.append(Check(name=name, status=state, detail=detail, evidence_ids=list(evidence)))

    def binary(name, ok, detail, evidence=()):
        add(name, "pass" if ok else "fail", detail, evidence)

    advisory = request.venue_facts == "advisory"

    def venue_unknown(name, detail, evidence=()):
        # A missing venue fact is unknown under either policy. Advisory only moves it out
        # of the verdict; a known conflict (closed at that hour) still fails.
        checks.append(Check(name=name, status="unknown", detail=detail,
                            evidence_ids=list(evidence), advisory=advisory))

    binary("route_chain", len(legs) == len(stops) + 1, "包含去程、站间与回程")
    previous_end, previous_id = request.departure, request.origin_id
    for i, stop in enumerate(stops):
        c = stop.candidate
        evidence = [e.id for e in c.evidence]
        if i >= len(legs):
            add("connection", "fail", f"{c.name} 缺少到达路段")
            continue
        leg = legs[i]
        binary(
            "connection",
            leg.origin == previous_id
            and leg.destination == c.id
            and leg.departure >= previous_end
            and leg.arrival == stop.arrival
            and stop.start >= stop.arrival
            and stop.end >= stop.start,
            f"{c.name}：前后路段与停留连续",
            [leg.evidence.id],
        )
        binary(
            "status",
            not c.cancelled,
            f"{c.name}：{'已关闭 / 取消' if c.cancelled else '可访问'}",
            evidence,
        )
        if c.open_at is None or c.close_at is None:
            venue_unknown(
                "opening_hours",
                f"{c.name}：营业时间未核实，出发前确认一下" if advisory
                else f"{c.name}：缺少开放时间证据",
            )
        else:
            binary(
                "opening_hours",
                c.open_at <= stop.start and stop.end <= c.close_at,
                f"{c.name}：停留须位于开放窗口",
                evidence,
            )
        required_stay = request.stay_minutes.get(c.id, c.stay)
        binary(
            "stay",
            (stop.end - stop.start).total_seconds() >= required_stay * 60,
            f"{c.name}：至少停留 {required_stay} 分钟",
        )
        if c.event:
            if not c.event_start or not c.event_end:
                add("event", "unknown", f"{c.name}：缺少可靠活动起止时间")
            else:
                binary(
                    "event",
                    stop.arrival + timedelta(minutes=10) <= c.event_start
                    and stop.start <= c.event_start
                    and stop.end >= c.event_end,
                    f"{c.name}：提前 10 分钟到场并完整参加",
                    evidence,
                )
        expected_fields = {"status", "cost"} | ({"hours"} if c.open_at is not None else set())
        fresh = all(
            any(
                e.field == field
                and e.valid_from <= stop.start
                and stop.end <= e.valid_until
                and e.fetched_at <= stop.start
                for e in c.evidence
            )
            for field in expected_fields
        )
        if fresh:
            add("evidence", "pass", f"{c.name}：证据覆盖停留时段", evidence)
        else:
            venue_unknown(
                "evidence",
                f"{c.name}：开放状态与费用未核实" if advisory else f"{c.name}：场馆证据未覆盖停留时段",
                evidence,
            )
        previous_end, previous_id = stop.end, c.id
    if legs:
        back = legs[-1]
        binary(
            "return_connection",
            back.origin == previous_id
            and back.destination == request.origin_id
            and back.departure >= previous_end,
            "回程与最后一站衔接",
            [back.evidence.id],
        )
        binary(
            "deadline",
            back.arrival <= request.deadline and back.arrival.date() == request.departure.date(),
            f"预计 {back.arrival:%H:%M} 返回，最晚 {request.deadline:%H:%M}",
            [back.evidence.id],
        )
    for leg in legs:
        fresh = leg.evidence.valid_from <= leg.departure <= leg.arrival <= leg.evidence.valid_until
        add(
            "route_evidence",
            "pass" if fresh else "unknown",
            f"{leg.origin} → {leg.destination}：路线有效时段",
            [leg.evidence.id],
        )
        binary(
            "route_duration",
            leg.arrival >= leg.departure
            and math.ceil((leg.arrival - leg.departure).total_seconds() / 60) == leg.minutes,
            "路段时长一致",
        )
    ids = {s.candidate.id for s in stops}
    binary("locks", set(request.locked_ids) <= ids, "保留全部显式锁定站点")
    binary("excluded", not (set(request.excluded_ids) & ids), "不含已删除站点")
    binary(
        "stop_count",
        1 <= len(stops) <= request.max_stops and len(ids) == len(stops),
        "站点数量与唯一性",
    )
    binary("timed_events", sum(s.candidate.event for s in stops) <= 1, "每趟最多一个定时活动")
    walking = sum(leg.walking_minutes for leg in legs)
    binary(
        "walking",
        walking <= request.max_walk_minutes,
        f"全程步行 {walking} / {request.max_walk_minutes} 分钟",
    )
    if request.budget_aud is not None:
        costs = [s.candidate.cost for s in stops]
        if request.include_transport_cost:
            costs += [leg.fare_aud for leg in legs]
        known = sum(c for c in costs if c is not None)
        if known > request.budget_aud:
            add("budget", "fail", f"已知费用 AUD {known:g} 超出预算")
        elif None in costs:
            add("budget", "unknown", "存在未知费用，无法验证所选预算范围")
        else:
            scope = "门票与交通" if request.include_transport_cost else "门票与活动"
            add("budget", "pass", f"{scope}费用 AUD {known:g} / {request.budget_aud:g}")
    return checks


def assemble(request: Request, order: tuple[Candidate, ...], tools: PlannerTools) -> Itinerary:
    origin = request_origin(request)
    previous, pos, cursor = request.origin_id, origin, request.departure
    stops, legs = [], []
    for c in order:
        leg = tools.route(previous, c.id, pos, c.model_dump(), cursor)
        legs.append(leg)
        start = max(leg.arrival, c.open_at or leg.arrival)
        if c.event_start:
            start = max(start, c.event_start)
        stay = request.stay_minutes.get(c.id, c.stay)
        end = start + timedelta(minutes=stay)
        if c.event_end:
            end = max(end, c.event_end)
        stops.append(
            Stop(
                candidate=c,
                arrival=leg.arrival,
                start=start,
                end=end,
                stay_minutes=int((end - start).total_seconds() / 60),
                wait_minutes=int((start - leg.arrival).total_seconds() / 60),
                locked=c.id in request.locked_ids,
                stay_basis="用户指定"
                if c.id in request.stay_minutes
                else "合成活动时段"
                if c.event
                else "系统建议（回放）"
                if request.mode == "replay"
                else "系统建议（Live）",
            )
        )
        previous, pos, cursor = c.id, c.model_dump(), end
    legs.append(tools.route(previous, request.origin_id, pos, origin, cursor))
    checks = validate(request, stops, legs)
    verdict = [c for c in checks if not c.advisory]
    state = (
        Status.INFEASIBLE
        if any(c.status == "fail" for c in verdict)
        else Status.CONDITIONAL
        if any(c.status == "unknown" for c in verdict)
        else Status.VERIFIED
    )
    unknowns = [c.detail for c in verdict if c.status == "unknown"]
    if any(s.candidate.cost is None for s in stops):
        unknowns.append("部分消费费用未知；餐饮与自选购物未计入")
    if request.include_transport_cost and any(leg.fare_aud is None for leg in legs):
        unknowns.append("TfNSW 当前不提供 Opal 票价；交通费用未计入已知费用")
    cost = sum(s.candidate.cost or 0 for s in stops)
    if request.include_transport_cost:
        cost += sum(leg.fare_aud or 0 for leg in legs)
    total = int((legs[-1].arrival - request.departure).total_seconds() / 60)
    matched = [tag for c in order for tag in c.tags if tag.lower() in request.preference.lower()]
    return Itinerary(
        id=hashlib.sha256(
            ("/".join(c.id for c in order) + request.model_dump_json()).encode()
        ).hexdigest()[:12],
        title=f"{order[0].category}开场，{order[-1].category}收尾"
        if len(order) > 1
        else f"给自己一段{order[0].category}时间",
        reason=f"包含与你这次偏好相关的「{matched[0]}」，把附近的停留串成一趟。"
        if matched
        else "按距离与时间窗口组合，让一次出门有几种不同的体验。",
        stops=stops,
        legs=legs,
        checks=checks,
        status=state,
        return_at=legs[-1].arrival,
        total_minutes=total,
        walking_minutes=sum(leg.walking_minutes for leg in legs),
        known_cost=cost,
        unknowns=unknowns,
        advisories=[c.detail for c in checks if c.advisory],
        score=round(len(order) * 30 + len(matched) * 18 - total * 0.05, 2),
        map_url=google_maps_url(request, order, legs),
    )


def plan(
    request: Request,
    limit: int = 20,
    previous: dict | None = None,
    tools: PlannerTools | None = None,
) -> tuple[Result, dict]:
    tools = tools or (
        ReplayTools(request, limit, previous)
        if request.mode == "replay"
        else LiveTools(request, limit, previous)
    )
    tools.log(
        "parse",
        "validate_request",
        f"悉尼时间 {request.departure:%H:%M}–{request.deadline:%H:%M}；硬约束由程序验证",
    )
    data_notice = (
        "合成回放数据，仅用于验证软件行为，不可据此实际出行。"
        if request.mode == "replay"
        else "路线为 TfNSW 实时查询；场馆事实来自 M0 核验快照，过期时仅作条件性提示。出行前请复核。"
    )
    if request.catalog == "osm":
        data_notice = (
            "地点来自 OpenStreetMap 快照（© OpenStreetMap contributors, ODbL），营业时间与费用未核实；"
            + ("路线为合成回放，不可据此实际出行。" if request.mode == "replay"
               else "路线为 TfNSW 实时查询。出行前请复核。")
        )
    found = tools.retrieve()
    all_ids = {c.id for c in found}
    if (set(request.locked_ids) | set(request.stay_minutes)) - all_ids:
        return Result(
            request=request,
            itineraries=[],
            trace=tools.trace,
            rejected=[],
            status="needs_input",
            message="锁定或修改的站点不在当前候选目录中。",
            tool_calls=tools.calls,
            cache_hits=0,
            elapsed_ms=0,
            data_notice=data_notice,
        ), tools.cache
    origin = request_origin(request)

    def relevance(c):
        return sum(tag.lower() in request.preference.lower() for tag in c.tags) * 5 - distance(
            origin, c.model_dump()
        )

    found.sort(key=relevance, reverse=True)
    locked = [c for c in found if c.id in request.locked_ids]

    def from_locks(c):
        return min(distance(lock.model_dump(), c.model_dump()) for lock in locked)

    if locked:
        # Stops added around a lock are on the way to it. Picking them by distance from the
        # origin put a corner shop and a cafe at home around a beach 4 km away.
        found.sort(key=lambda c: from_locks(c) - relevance(c) * 0.08)

    def individually_possible(c):
        if c.id in request.excluded_ids or c.cancelled:
            return False
        if locked and c not in locked and (
            from_locks(c) > SIDE_KM or c.category in {lock.category for lock in locked}
        ):
            return False  # too far to be on the way, or a second beach after the beach
        # An event that cannot finish and return by the deadline is an obvious
        # deterministic conflict. Unknown hours remain eligible for inspection.
        if c.event_end:
            return c.event_end + timedelta(minutes=10) <= request.deadline
        return True

    pool = [c for c in found if individually_possible(c)]
    if any(c.cancelled for c in locked):
        tools.log("validate", "lock_conflict", "锁定站点已关闭，不能自动替换")
        return Result(
            request=request,
            itineraries=[],
            trace=tools.trace,
            rejected=[],
            status="needs_input",
            message="锁定的站点已关闭。请解除锁定，或取消这次行程。",
            tool_calls=tools.calls,
            cache_hits=0,
            elapsed_ms=round((time.perf_counter() - tools.started) * 1000, 2),
            data_notice=data_notice,
        ), tools.cache
    itineraries, rejected, seen = [], [], set()
    # At most six deep combinations. Coarse distances are never final evidence.
    trials = 0
    exhausted = False
    window = (request.deadline - request.departure).total_seconds() / 60
    sizes = list(range(request.max_stops, max(1, len(locked)) - 1, -1))
    if window <= 75:
        sizes = [size for size in sizes if size <= max(1, len(locked))]
    try:
        for size in sizes:
            for anchor in pool:
                selected = list(locked)
                if anchor not in selected:
                    selected.append(anchor)
                if len(selected) > size:
                    continue
                near = sorted(
                    (c for c in pool if c not in selected),
                    key=lambda c: (
                        min(distance(s.model_dump(), c.model_dump()) for s in selected)
                        - relevance(c) * 0.08
                    ),
                )
                selected += near[: size - len(selected)]
                if len(selected) != size:
                    continue
                signature = frozenset(c.id for c in selected)
                if signature in seen:
                    continue
                seen.add(signature)

                # Finite permutation ordering by approximate distance; exact routes are validated below.
                def rough(order):
                    points = [origin] + [c.model_dump() for c in order] + [origin]
                    return sum(distance(a, b) for a, b in zip(points, points[1:]))

                order = min(itertools.permutations(selected), key=rough)
                tools.log("plan", "propose_itinerary", " → ".join(c.name for c in order))
                try:
                    itinerary = assemble(request, order, tools)
                except NoRouteError as exc:
                    rejected.append(
                        {"candidates": [c.id for c in order], "reasons": [str(exc)]}
                    )
                    tools.log("validate", "reject", str(exc))
                    trials += 1
                    continue
                trials += 1
                if itinerary.status == Status.INFEASIBLE:
                    reasons = [c.detail for c in itinerary.checks if c.status == "fail"]
                    rejected.append({"candidates": [c.id for c in order], "reasons": reasons})
                    tools.log("validate", "reject", "；".join(reasons))
                else:
                    itineraries.append(itinerary)
                    tools.log(
                        "validate",
                        "accept",
                        f"{size} 站 · {itinerary.status} · {itinerary.return_at:%H:%M} 返回",
                    )
                if len(itineraries) >= 3:
                    break
                if trials >= 6:
                    exhausted = True
                    break
                # Leave budget for a smaller combination after two unsuccessful large plans.
                if not itineraries and trials >= 2 and size > 1:
                    break
            if len(itineraries) >= 3 or exhausted:
                break
    except BudgetExhausted:
        exhausted = True
        tools.log("finish", "budget_exhausted", f"已使用 {tools.calls} / {limit} 次回放工具请求")
    itineraries.sort(key=lambda x: (x.status == Status.VERIFIED, x.score), reverse=True)
    # The fixed policy may stop once it has useful verified output. Exhaustion
    # only becomes the run outcome when no plan survived; Trace still records it.
    status = "completed" if itineraries else "search_exhausted"
    message = (
        (
            "找到可供比较的小行程。全部时间、费用与路线均来自合成回放场景。"
            if request.mode == "replay"
            else "找到可供比较的小行程。路线来自 TfNSW；交通票价未知。"
        )
        if itineraries
        else "在本次候选与调用预算内未找到可行方案；这不代表悉尼没有可去的地方。"
    )
    if itineraries and len(itineraries[0].stops) < min(2, request.max_stops):
        message += "当前时间或证据限制下仅返回一站。"
    tools.log("finish", "finish", f"返回 {len(itineraries)} 个方案；{len(rejected)} 个组合未通过")
    return Result(
        request=request,
        itineraries=itineraries,
        trace=tools.trace,
        rejected=rejected,
        status=status,
        message=message,
        tool_calls=tools.calls,
        cache_hits=tools.hits,
        elapsed_ms=round((time.perf_counter() - tools.started) * 1000, 2),
        data_notice=data_notice,
    ), tools.cache
