"""Hook material: what is true about this moment, computed rather than fetched.

A hook has to say why now and why this person. Encyclopedia text answers neither, so the
narrator gets facts derived from the request and the validated itinerary instead: when
the sun sets relative to the stops, how much of the free window the quest uses, what the
legs actually are, and which remembered preferences shaped the pick. Every fact carries
an id the narration may cite; the citation check in `agent.narrate` accepts only these
and the stops' own evidence.

None of this feeds the verdict. Sunset is astronomy, not weather: it says when, never
whether there will be anything to see.
"""

import math
from datetime import date, datetime, timedelta, timezone

from pydantic import BaseModel

from .memory import MemoryItem, context_of
from .models import SYDNEY, Candidate, Itinerary, Request
from .planner import request_origin
from .taste import Dimension, aim_value, describe, dimensions_of


class Fact(BaseModel, frozen=True):
    id: str
    text: str


def sunset(day: date, lat: float, lon: float) -> datetime | None:
    """NOAA's low-precision solar equations, good to a couple of minutes at Sydney's
    latitude. None where the sun does not set that day (never inside the bounding box)."""
    gamma = 2 * math.pi / 365 * (day.timetuple().tm_yday - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    phi = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(phi) * math.cos(decl))
              - math.tan(phi) * math.tan(decl))
    if not -1 <= cos_ha <= 1:
        return None
    minutes = (720 - 4 * (lon - math.degrees(math.acos(cos_ha))) - eqtime) % 1440
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return (midnight + timedelta(minutes=round(minutes))).astimezone(SYDNEY)


def sun_fact(request: Request, itinerary: Itinerary) -> Fact:
    origin = request_origin(request)
    day = request.departure.date()
    at = sunset(day, origin["lat"], origin["lon"])
    fact_id = f"clock:sunset:{day.isoformat()}"
    if at is None:
        return Fact(id=fact_id, text="当天没有日落")
    text = f"约 {at:%H:%M} 日落（按日期与坐标计算）"
    if at <= request.departure:
        return Fact(id=fact_id, text=f"{text}，出发时天已经黑了")
    if at > itinerary.return_at:
        return Fact(id=fact_id, text=f"{text}，回来之前天不会黑")
    for stop in itinerary.stops:
        if stop.arrival <= at <= stop.end:
            return Fact(id=fact_id, text=f"{text}，那时你正在{stop.candidate.name}")
    return Fact(id=fact_id, text=f"{text}，那时你在路上")


def window_fact(request: Request, itinerary: Itinerary) -> Fact:
    free = round((request.deadline - request.departure).total_seconds() / 60)
    spare = round((request.deadline - itinerary.return_at).total_seconds() / 60)
    return Fact(
        id="clock:window",
        text=f"{context_of(request.departure, request.deadline).label}：空闲 {free} 分钟，"
             f"整趟用 {itinerary.total_minutes} 分钟，{itinerary.return_at:%H:%M} 回到，"
             f"离必须回来还剩 {spare} 分钟",
    )


def leg_facts(request: Request, itinerary: Itinerary) -> list[Fact]:
    names = {request.origin_id: request_origin(request)["name"],
             **{s.candidate.id: s.candidate.name for s in itinerary.stops}}
    out = []
    for leg in itinerary.legs:
        how = "步行" if leg.mode == "walk" else f"公共交通（其中步行 {leg.walking_minutes} 分钟"
        if leg.mode != "walk":
            how += f"，换乘 {leg.transfers} 次）" if leg.transfers else "）"
        out.append(Fact(
            id=leg.evidence.id,
            text=f"{names.get(leg.origin, leg.origin)} → {names.get(leg.destination, leg.destination)}："
                 f"{leg.departure:%H:%M} 出发，{how} {leg.minutes} 分钟",
        ))
    return out


def memory_facts(items: list[MemoryItem], anchor: Candidate, kind: str | None,
                 names: dict[str, str]) -> list[Fact]:
    """Only remembered preferences this pick actually agrees with. An item that pushed the
    anchor down, or a dimension the anchor sits on the other side of, is not a reason to go."""
    axes = dimensions_of(anchor.tags, 0.0, anchor.obviousness)
    out = []
    for item in items:
        key = item.key
        if key.kind == "dimension":
            dimension = Dimension(key.key)
            fits = (dimension is not Dimension.TRAVEL
                    and axes[dimension] == aim_value(dimension, int(key.value)))
        elif key.kind == "category":
            fits = key.key == kind and key.value == "like"
        elif key.kind == "place":
            fits = key.key == anchor.id and key.value == "favorite"
        else:
            fits = False  # notes never score, so they never become a reason either
        if fits:
            text = describe(key, item.context, item.label or names.get(key.key, ""))
            out.append(Fact(id=item.id, text=f"你确认过的偏好：{text}"))
    return out
