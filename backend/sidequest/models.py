from datetime import datetime
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

SYDNEY = ZoneInfo("Australia/Sydney")


class Status(StrEnum):
    VERIFIED = "verified"
    CONDITIONAL = "conditional"
    INFEASIBLE = "infeasible"


class Request(BaseModel):
    origin_id: Literal["central", "townhall", "circular", "current"] = "townhall"
    origin_lat: float | None = Field(default=None, ge=-34.2, le=-33.5)
    origin_lon: float | None = Field(default=None, ge=150.8, le=151.5)
    departure: datetime
    deadline: datetime
    preference: str = Field(default="", max_length=300)
    max_walk_minutes: int = Field(default=90, ge=0, le=480)
    budget_aud: float | None = Field(default=None, ge=0, le=10000)
    include_transport_cost: bool = False
    max_stops: int = Field(default=3, ge=1, le=3)
    locked_ids: list[str] = Field(default_factory=list, max_length=3)
    # Users revise at most 12 at a time (api.Revision); an agent session also excludes
    # every place rerolled away, which accumulates past that.
    excluded_ids: list[str] = Field(default_factory=list, max_length=64)
    stay_minutes: dict[str, int] = Field(default_factory=dict)
    mode: Literal["replay", "live"] = "replay"
    # "verdict": missing venue facts (hours, venue evidence) make the plan conditional.
    # "advisory": they are still reported as unknown -- never pass -- but only the time
    # budget and known conflicts decide the verdict. Plan v2.1 §6.1: the open-world MVP.
    venue_facts: Literal["verdict", "advisory"] = "verdict"
    # Where candidates come from, independent of `mode` (which picks the route source).
    # "fixed": replay fixtures or the curated allowlist. "osm": places discovered around
    # the origin from the local OpenStreetMap index (plan v2.1 §5).
    catalog: Literal["fixed", "osm"] = "fixed"

    @model_validator(mode="after")
    def validate_window(self):
        for field in ("departure", "deadline"):
            dt = getattr(self, field)
            if dt.tzinfo is None:
                raise ValueError("时间必须携带时区")
            local = dt.astimezone(SYDNEY)
            # Reject imaginary wall times during the spring DST jump.
            if local.astimezone(ZoneInfo("UTC")).astimezone(SYDNEY) != local:
                raise ValueError("无效的当地时间")
            setattr(self, field, local)
        minutes = (self.deadline.timestamp() - self.departure.timestamp()) / 60
        if not 30 <= minutes <= 960:
            raise ValueError("可用时间应为 30 分钟至 16 小时")
        if self.departure.date() != self.deadline.date():
            raise ValueError("出发和返回必须在悉尼当地同一天")
        if set(self.locked_ids) & set(self.excluded_ids):
            raise ValueError("同一站不能同时锁定和删除")
        if len(set(self.locked_ids)) > self.max_stops:
            raise ValueError("锁定站数超过行程站数上限")
        if any(not 10 <= v <= 300 for v in self.stay_minutes.values()):
            raise ValueError("停留时长应为 10 至 300 分钟")
        if self.origin_id == "current" and (self.origin_lat is None or self.origin_lon is None):
            raise ValueError("使用当前位置时必须提供悉尼范围内的坐标")
        return self


class Evidence(BaseModel):
    id: str
    field: str
    value: str
    source_url: str
    fetched_at: datetime
    valid_from: datetime
    valid_until: datetime
    synthetic: bool = True


class Candidate(BaseModel):
    id: str
    name: str
    category: str
    description: str
    lat: float
    lon: float
    tags: list[str]
    stay: int
    cost: float | None
    # D3 常规度: 1 = the obvious tourist answer, 0 = you only find it if you already knew.
    # A property of the place itself, never a distance from this user's history.
    obviousness: float | None = Field(default=None, ge=0, le=1)
    open_at: datetime | None
    close_at: datetime | None
    event: bool = False
    event_start: datetime | None = None
    event_end: datetime | None = None
    cancelled: bool = False
    evidence: list[Evidence]


class Leg(BaseModel):
    origin: str
    destination: str
    departure: datetime
    arrival: datetime
    minutes: int
    walking_minutes: int
    transfers: int = 0
    fare_aud: float | None = None
    mode: Literal["walk", "transit"]
    evidence: Evidence


class Stop(BaseModel):
    candidate: Candidate
    arrival: datetime
    start: datetime
    end: datetime
    stay_minutes: int
    wait_minutes: int
    locked: bool
    stay_basis: str = "系统建议（回放）"


class Check(BaseModel):
    name: str
    status: Literal["pass", "fail", "unknown"]
    detail: str
    evidence_ids: list[str] = Field(default_factory=list)
    # Shown to the user, excluded from the verdict. Only ever set on an unknown.
    advisory: bool = False


class Itinerary(BaseModel):
    id: str
    title: str
    reason: str
    stops: list[Stop]
    legs: list[Leg]
    checks: list[Check]
    status: Status
    return_at: datetime
    total_minutes: int
    walking_minutes: int
    known_cost: float
    unknowns: list[str]
    score: float
    advisories: list[str] = Field(default_factory=list)
    map_url: str


class Trace(BaseModel):
    sequence: int
    node: str
    action: str
    summary: str
    elapsed_ms: float
    cache_hit: bool = False
    evidence_ids: list[str] = Field(default_factory=list)


class Result(BaseModel):
    request: Request
    itineraries: list[Itinerary]
    trace: list[Trace]
    rejected: list[dict]
    status: Literal["completed", "search_exhausted", "needs_input"]
    message: str
    tool_calls: int
    cache_hits: int
    elapsed_ms: float
    data_notice: str = "合成回放数据，仅用于验证软件行为，不可据此实际出行。"
    strategy: str = "fixed-v1"
