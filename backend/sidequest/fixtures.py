"""Synthetic scenario templates. Names are illustrative, facts are NOT live claims."""

from datetime import datetime, time, timedelta

from .models import SYDNEY, Candidate, Evidence, Request

ORIGINS = {
    "central": {"name": "Central Station", "lat": -33.8830, "lon": 151.2065},
    "townhall": {"name": "Town Hall", "lat": -33.8732, "lon": 151.2067},
    "circular": {"name": "Circular Quay", "lat": -33.8610, "lon": 151.2105},
}

# id, name, category, description, latitude, longitude, tags, stay, cost, open, close, obviousness
ROWS = [
    (
        "gallery",
        "Art Gallery of NSW",
        "艺术",
        "让一幅画留住你，给下午一点新的视角。",
        -33.8688,
        151.2173,
        ["艺术", "室内", "安静", "art"],
        50,
        0,
        10,
        17,
        0.9,
    ),
    (
        "garden",
        "Royal Botanic Garden",
        "自然",
        "沿着树荫慢走，把城市的声音留在身后。",
        -33.8642,
        151.2166,
        ["自然", "户外", "散步", "nature"],
        35,
        0,
        7,
        18,
        0.95,
    ),
    (
        "library",
        "State Library of NSW",
        "文化",
        "从一段城市故事开始一次不赶时间的漫游。",
        -33.8662,
        151.2127,
        ["文化", "室内", "安静", "阅读"],
        35,
        0,
        10,
        17,
        0.5,
    ),
    (
        "harbour",
        "Circular Quay waterfront",
        "海港",
        "走到水边，给自己留一段什么也不做的时间。",
        -33.8606,
        151.2122,
        ["海港", "户外", "散步", "风景"],
        25,
        0,
        6,
        22,
        1.0,
    ),
    (
        "market",
        "The Rocks · market stop",
        "街区",
        "在小巷和摊位之间，寻找意料之外的小东西。",
        -33.8598,
        151.2082,
        ["市集", "街区", "热闹", "购物"],
        40,
        None,
        10,
        16,
        0.6,
    ),
    (
        "museum",
        "Australian Museum",
        "探索",
        "把好奇心交给一段自然史，适合慢慢看。",
        -33.8749,
        151.2120,
        ["探索", "室内", "博物馆", "文化"],
        55,
        0,
        10,
        17,
        0.7,
    ),
    (
        "park",
        "Hyde Park",
        "自然",
        "在城市中心拐进绿色，短暂换一个节奏。",
        -33.8731,
        151.2113,
        ["自然", "户外", "散步", "安静"],
        20,
        0,
        6,
        22,
        0.3,
    ),
    (
        "screening",
        "Harbour stories · screening",
        "定时活动",
        "合成场景：一场需要按时到达、完整参加的城市短片放映。",
        -33.8624,
        151.2091,
        ["电影", "室内", "文化", "活动"],
        60,
        18,
        13,
        16,
        0.25,
    ),
]


def at(request: Request, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(request.departure.date(), time(hour, minute), SYDNEY)


def candidates(request: Request) -> list[Candidate]:
    result = []
    for row in ROWS:
        cid, name, category, description, lat, lon, tags, stay, cost, opening, closing, obvious = row
        evidence = [
            Evidence(
                id=f"fixture:{cid}:{field}:{request.departure.date()}",
                field=field,
                value=value,
                source_url="https://example.org/sidequest/synthetic-fixtures-v1",
                fetched_at=at(request, 0),
                valid_from=at(request, 0),
                valid_until=at(request, 0) + timedelta(days=1),
            )
            for field, value in (
                ("hours", f"{opening:02}:00–{closing:02}:00（合成）"),
                ("cost", "未知" if cost is None else f"AUD {cost}（合成）"),
                ("status", "open"),
            )
        ]
        result.append(
            Candidate(
                id=cid,
                name=name,
                category=category,
                description=description,
                lat=lat,
                lon=lon,
                tags=tags,
                stay=stay,
                cost=cost,
                obviousness=obvious,
                open_at=at(request, opening),
                close_at=at(request, closing),
                event=cid == "screening",
                event_start=at(request, 14) if cid == "screening" else None,
                event_end=at(request, 15) if cid == "screening" else None,
                evidence=evidence,
            )
        )
    return result
