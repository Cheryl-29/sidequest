"""Open-world place discovery from a local OpenStreetMap snapshot (plan v2.1 §5, M3′-2).

The index is a SQLite file built offline by `scripts/build_places.py` from an Overpass
JSON export. Retrieval is per request: the executor centres the search on the request's
origin and derives the radius from the free window, so neither the user nor the model
ever chooses a coordinate, a radius or a URL.

What OSM gives us is a name, a position and a kind. Everything else stays missing:
opening hours and fees are crowd-sourced, so they never become `open_at`/`close_at` or
`cost`, and under `venue_facts="advisory"` they surface as advisories rather than as a
verdict. The one use of the `opening_hours` tag is a conservative soft filter -- a place
is dropped only when a tag we fully understand says it is shut for the entire window.
"""

import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .models import SYDNEY, Candidate, Evidence, Request

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INDEX = ROOT / "data/places/sydney.sqlite"
OSM_ATTRIBUTION = "© OpenStreetMap contributors, ODbL"

# Radius from the free window: roughly a third of it can go on the way out, at an
# effective door-to-door public transport speed. Coarse on purpose -- it only decides
# what is worth routing; TfNSW legs decide what is reachable.
KM_PER_MINUTE = 0.15
MIN_RADIUS_KM = 1.0
MAX_RADIUS_KM = 12.0
PER_KIND_PER_RING = 4  # near half and far half of the radius, so D2 has both poles
PLACE_TTL = timedelta(days=30)


@dataclass(frozen=True)
class Kind:
    code: str
    osm: tuple[tuple[str, str], ...]  # every pair must match; `;`-lists count as matching
    category: str
    tags: tuple[str, ...]
    stay: int  # a planning assumption, labelled as such, never a venue fact
    action: str = ""  # what to do there; narration material, never a fact about the place
    everyday: bool = False  # an errand-sized quest; D3 says nothing about a supermarket
    # A "just passing through" visit for places that are still worth ten minutes: thirty
    # free minutes at Bondi should reach the sand, not only the cafes behind it.
    short_stay: int | None = None
    short_action: str = ""


# Precedence order: the first matching kind wins for an object carrying several, so a
# bubble tea cafe must come before the plain cafe.
KINDS = (
    Kind("museum", (("tourism", "museum"),), "博物馆", ("博物馆", "文化", "室内"), 60),
    Kind("gallery", (("tourism", "gallery"),), "美术馆", ("美术馆", "艺术", "室内"), 45),
    Kind("arts_centre", (("amenity", "arts_centre"),), "艺术中心", ("艺术", "文化", "室内"), 45),
    Kind("library", (("amenity", "library"),), "图书馆", ("图书馆", "安静", "室内"), 45),
    Kind("viewpoint", (("tourism", "viewpoint"),), "观景点", ("观景", "户外"), 20),
    Kind("beach", (("natural", "beach"),), "海滩", ("海滩", "自然", "户外"), 60,
         short_stay=15, short_action="走到水边站一会儿再回来"),
    Kind("nature_reserve", (("leisure", "nature_reserve"),), "自然保护区",
         ("自然", "散步", "户外"), 60, short_stay=20, short_action="沿步道走一小段就折返"),
    Kind("garden", (("leisure", "garden"),), "花园", ("花园", "散步", "户外"), 40,
         short_stay=15, short_action="找张长椅坐一会儿"),
    Kind("park", (("leisure", "park"),), "公园", ("公园", "散步", "户外"), 45,
         short_stay=15, short_action="找张长椅坐一会儿"),
    Kind("supermarket", (("shop", "supermarket"),), "超市", ("超市", "零食", "室内"), 15,
         "买一样没吃过的零食", True),
    Kind("convenience", (("shop", "convenience"),), "便利店", ("便利店", "零食", "室内"), 10,
         "买一样没吃过的零食", True),
    Kind("bakery", (("shop", "bakery"),), "面包店", ("面包", "室内"), 10,
         "挑一个今天看着最顺眼的", True),
    Kind("bubble_tea", (("amenity", "cafe"), ("cuisine", "bubble_tea")), "奶茶店",
         ("奶茶", "室内"), 15, "点一个平时不会点的口味", True),
    Kind("ice_cream", (("amenity", "ice_cream"),), "冰淇淋店", ("冰淇淋", "室内"), 15,
         "点一个平时不会点的口味", True),
    Kind("cafe", (("amenity", "cafe"),), "咖啡馆", ("咖啡", "室内"), 20,
         "外带一杯，换条路走回来", True),
    Kind("books", (("shop", "books"),), "书店", ("书店", "安静", "室内"), 20,
         "翻三本书的第一页", True),
    Kind("second_hand", (("shop", "second_hand"),), "二手店", ("二手", "室内"), 20,
         "找一件看不出年代的东西", True),
    Kind("florist", (("shop", "florist"),), "花店", ("花店", "室内"), 10,
         "买一枝花放在桌上", True),
)
KIND_BY_CODE = {k.code: k for k in KINDS}
KEPT_TAGS = ("name", "opening_hours", "website", "wikipedia", "wikidata", "fee", "tourism")


class PlaceIndexMissing(RuntimeError):
    pass


def _has(tags: dict, key: str, value: str) -> bool:
    return value in (part.strip() for part in tags.get(key, "").split(";"))


def kind_of(tags: dict) -> Kind | None:
    return next((k for k in KINDS if all(_has(tags, key, v) for key, v in k.osm)), None)


def obviousness(tags: dict) -> float:
    """D3 proxy from the place's own tags: is it the answer a guidebook would give?

    Deliberately about the place, never about any user (plan v2 §4.3). A Wikipedia tag
    means an article exists (middle tier) and an attraction tag on top of it reaches the
    top tier.
    """
    score = 0.2
    if tags.get("wikipedia"):
        score += 0.3
    if tags.get("wikidata"):
        score += 0.1
    if tags.get("tourism") == "attraction":
        score += 0.3
    return round(min(1.0, score), 2)


def km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    lat1, lat2 = math.radians(a_lat), math.radians(b_lat)
    dlat, dlon = lat2 - lat1, math.radians(b_lon - a_lon)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.asin(min(1, math.sqrt(value)))


# ---- opening_hours soft filter ------------------------------------------------------

DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
_TIMES = r"\d{2}:\d{2}-\d{2}:\d{2}(?:,\d{2}:\d{2}-\d{2}:\d{2})*"
_RULE = re.compile(rf"^(?:(?P<days>[A-Za-z]{{2}}(?:[-,][A-Za-z]{{2}})*)\s+)?(?P<times>off|closed|{_TIMES})$")


def _days(text: str | None) -> list[int] | None:
    if text is None:
        return list(range(7))
    out: list[int] = []
    for part in text.split(","):
        ends = part.split("-")
        if any(e not in DAYS for e in ends) or len(ends) > 2:
            return None  # PH, SH, month names: not something we can rule on
        start, stop = DAYS.index(ends[0]), DAYS.index(ends[-1])
        out += [(start + i) % 7 for i in range((stop - start) % 7 + 1)]
    return out


def _span(text: str) -> tuple[int, int] | None:
    a, b = text.split("-")
    start = int(a[:2]) * 60 + int(a[3:])
    end = int(b[:2]) * 60 + int(b[3:])
    if not 0 <= start < end <= 24 * 60:
        return None  # overnight spans and malformed times stay unknown
    return start, end


def weekly_hours(raw: str) -> dict[int, list[tuple[int, int]]] | None:
    """Parse the plain weekly subset of OSM opening_hours. None means "not understood".

    Semantics follow the spec for that subset: rules apply in order, a later rule
    replaces an earlier one for the days it names, and unnamed days are closed.
    """
    raw = raw.strip()
    if raw == "24/7":
        return {d: [(0, 24 * 60)] for d in range(7)}
    week: dict[int, list[tuple[int, int]]] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        match = _RULE.match(part)
        if not match:
            return None
        days = _days(match["days"])
        if days is None:
            return None
        times = match["times"]
        spans = [] if times in ("off", "closed") else [_span(t) for t in times.split(",")]
        if any(s is None for s in spans):
            return None
        for day in days:
            week[day] = spans
    return week


def shut_for_window(raw: str | None, start: datetime, end: datetime) -> bool:
    """True only when a fully understood tag leaves no overlap with the window."""
    if not raw:
        return False
    week = weekly_hours(raw)
    if week is None:
        return False
    local_start, local_end = start.astimezone(SYDNEY), end.astimezone(SYDNEY)
    a = local_start.hour * 60 + local_start.minute
    b = local_end.hour * 60 + local_end.minute
    return not any(s < b and a < e for s, e in week.get(local_start.weekday(), []))


# ---- index --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE places (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
    lat REAL NOT NULL, lon REAL NOT NULL, obviousness REAL, tags TEXT NOT NULL
);
CREATE INDEX places_lat ON places(lat);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def index_rows(elements, bbox: tuple[float, float, float, float]):
    """Overpass-shaped elements -> index rows. Unnamed, unknown-kind and out-of-box
    objects are dropped; ways and relations use the centre Overpass computed."""
    south, west, north, east = bbox
    for element in elements:
        tags = element.get("tags") or {}
        kind = kind_of(tags)
        point = element if "lat" in element else element.get("center") or {}
        lat, lon = point.get("lat"), point.get("lon")
        if not tags.get("name") or kind is None or lat is None or lon is None:
            continue
        if not (south <= lat <= north and west <= lon <= east):
            continue
        yield (
            f"osm:{element['type']}/{element['id']}",
            tags["name"],
            kind.code,
            lat,
            lon,
            None if kind.everyday else obviousness(tags),
            json.dumps({k: tags[k] for k in KEPT_TAGS if k in tags}, ensure_ascii=False),
        )


def build_index(elements, out: Path, meta: dict[str, str], bbox) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    with sqlite3.connect(tmp) as db:
        db.executescript(SCHEMA)
        db.executemany("INSERT OR IGNORE INTO places VALUES (?,?,?,?,?,?,?)",
                       index_rows(elements, bbox))
        count = db.execute("SELECT COUNT(*) FROM places").fetchone()[0]
        db.executemany("INSERT INTO meta VALUES (?,?)",
                       {**meta, "count": str(count), "attribution": OSM_ATTRIBUTION}.items())
    db.close()
    tmp.replace(out)  # a half-built index is never visible under the real name
    return count


def index_path() -> Path:
    return Path(os.environ.get("SIDEQUEST_PLACES_DB") or DEFAULT_INDEX)


class PlaceIndex:
    def __init__(self, path: Path | None = None):
        self.path = path or index_path()
        if not self.path.exists():
            raise PlaceIndexMissing(
                "OSM 地点索引尚未构建：先运行 scripts/fetch_places.py 与 scripts/build_places.py"
            )
        with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as db:
            self.meta = dict(db.execute("SELECT key, value FROM meta"))
            self.snapshot_at = datetime.fromisoformat(self.meta["source_timestamp"]).astimezone(
                SYDNEY
            )

    def near(self, lat: float, lon: float, radius_km: float) -> list[dict]:
        dlat = radius_km / 111.0
        dlon = radius_km / (111.0 * math.cos(math.radians(lat)))
        with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM places WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
            ).fetchall()
        found = []
        for row in rows:
            distance = km(lat, lon, row["lat"], row["lon"])
            if distance <= radius_km:
                found.append({**dict(row), "tags": json.loads(row["tags"]), "km": distance})
        return sorted(found, key=lambda r: (r["km"], r["id"]))


def radius_for(request: Request) -> float:
    minutes = (request.deadline - request.departure).total_seconds() / 60
    return max(MIN_RADIUS_KM, min(MAX_RADIUS_KM, minutes / 3 * KM_PER_MINUTE))


def select(rows: list[dict], radius_km: float) -> list[dict]:
    """Cap each kind in each ring so a thousand pocket parks cannot crowd out the one
    museum, and so the far half of the radius is always represented."""
    taken: dict[tuple[str, bool], int] = {}
    out = []
    for row in rows:  # nearest first
        slot = (row["kind"], row["km"] > radius_km / 2)
        if taken.get(slot, 0) < PER_KIND_PER_RING:
            taken[slot] = taken.get(slot, 0) + 1
            out.append(row)
    return out


# The full stay is used when it takes at most this share of the window, leaving the rest
# for getting there and back; otherwise a kind with a short visit falls back to it.
FULL_STAY_SHARE = 0.5
DUPLICATE_KM = 0.15


def stay_for(kind: Kind, window_minutes: float) -> tuple[int, bool]:
    """(minutes, is_short). A planning assumption either way, never a venue fact."""
    if kind.short_stay is not None and kind.stay > window_minutes * FULL_STAY_SHARE:
        return kind.short_stay, True
    return kind.stay, False


def _name_key(name: str) -> str:
    return " ".join(name.casefold().replace("’", "'").split())


def dedupe(rows: list[dict]) -> list[dict]:
    """Drop the farther copy of a same-named place within DUPLICATE_KM.

    OSM often carries a shop twice (a node and its building, or two mappers). A chain's
    other branch is a different place, so only near-identical positions collapse.
    """
    kept: dict[str, list[dict]] = {}
    out = []
    for row in rows:  # nearest first, so the copy closer to the user survives
        twins = kept.setdefault(_name_key(row["name"]), [])
        if any(km(row["lat"], row["lon"], t["lat"], t["lon"]) <= DUPLICATE_KM for t in twins):
            continue
        twins.append(row)
        out.append(row)
    return out


def to_candidate(row: dict, index: PlaceIndex, window_minutes: float = 24 * 60) -> Candidate:
    kind = KIND_BY_CODE[row["kind"]]
    stay, short = stay_for(kind, window_minutes)
    tags = row["tags"]
    kind_type, osm_id = row["id"].removeprefix("osm:").split("/")
    hours = tags.get("opening_hours")
    summary = f"OpenStreetMap 标注为{kind.category}"
    if hours:
        summary += f"；众包营业时间标注 {hours}（未核实）"
    evidence = Evidence(
        id=f"{row['id']}@{index.meta['source_timestamp']}",
        field="place",
        value=summary,
        source_url=f"https://www.openstreetmap.org/{kind_type}/{osm_id}",
        fetched_at=index.snapshot_at,
        valid_from=index.snapshot_at,
        valid_until=index.snapshot_at + PLACE_TTL,
        synthetic=False,
    )
    return Candidate(
        id=row["id"],
        name=row["name"],
        category=kind.category,
        description=f"{kind.category}，数据来自 OpenStreetMap（{OSM_ATTRIBUTION}）。",
        lat=row["lat"],
        lon=row["lon"],
        tags=[*kind.tags, f"kind:{kind.code}", *(["visit:short"] if short else [])],
        stay=stay,
        cost=None,  # OSM fee tags are crowd-sourced; unknown cost is not zero cost
        obviousness=row["obviousness"],
        open_at=None,  # never promote crowd-sourced hours into a verdict input
        close_at=None,
        evidence=[evidence],
    )


def discover(request: Request, index: PlaceIndex | None = None) -> list[Candidate]:
    """Real places around the request's origin that are worth routing to."""
    from .planner import request_origin

    index = index or PlaceIndex()
    origin = request_origin(request)
    radius = radius_for(request)
    window = (request.deadline - request.departure).total_seconds() / 60
    rows = [
        r for r in dedupe(index.near(origin["lat"], origin["lon"], radius))
        # A stay that alone fills the window is a certain conflict, not a guess: thirty
        # free minutes never reach a museum, so do not spend attempts proving it.
        if stay_for(KIND_BY_CODE[r["kind"]], window)[0] < window
        and not shut_for_window(r["tags"].get("opening_hours"), request.departure, request.deadline)
    ]
    return [to_candidate(r, index, window) for r in select(rows, radius)]


def kind_for(candidate: Candidate) -> Kind | None:
    return KIND_BY_CODE.get(candidate_kind(candidate) or "")


def action_for(candidate: Candidate) -> str:
    kind = kind_for(candidate)
    if kind is None:
        return ""
    return kind.short_action if "visit:short" in candidate.tags else kind.action


def candidate_kind(candidate: Candidate) -> str | None:
    return next((t.removeprefix("kind:") for t in candidate.tags if t.startswith("kind:")), None)
