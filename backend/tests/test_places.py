from datetime import datetime

import pytest
from sidequest.agent import propose_quest
from sidequest.models import SYDNEY, Request, Status
from sidequest.places import (
    PlaceIndex,
    PlaceIndexMissing,
    action_for,
    build_index,
    candidate_kind,
    dedupe,
    discover,
    obviousness,
    radius_for,
    shut_for_window,
    weekly_hours,
)
from sidequest.planner import SIDE_KM, distance, plan
from sidequest.taste import Feedback, Reason, TasteState
from test_agent import FakeModel

BBOX = (-34.2, 150.8, -33.5, 151.5)
TOWN_HALL = (-33.8732, 151.2067)


def element(i, name, lat, lon, kind=("tourism", "museum"), way=False, **tags):
    body = {"type": "way" if way else "node", "id": i,
            "tags": {kind[0]: kind[1], **({"name": name} if name else {}), **tags}}
    point = {"lat": lat, "lon": lon}
    return {**body, "center": point} if way else {**body, **point}


ELEMENTS = [
    element(1, "Near Museum", -33.8740, 151.2070, opening_hours="Mo-Su 10:00-17:00",
            wikipedia="en:Near Museum"),
    element(2, "Night Gallery", -33.8750, 151.2080, kind=("tourism", "gallery"),
            opening_hours="Mo-Su 18:00-23:00"),
    element(3, "Pocket Park", -33.8760, 151.2050, kind=("leisure", "park"), way=True),
    element(4, "Far Park", -33.9100, 151.2400, kind=("leisure", "park"), way=True),
    element(5, None, -33.8745, 151.2075),  # unnamed: dropped
    element(6, "Bank", -33.8745, 151.2075, kind=("amenity", "bank")),  # not a side-quest kind
    element(7, "Newcastle Museum", -32.9270, 151.7760),  # outside the Sydney box
    element(8, "Mystery Hours", -33.8741, 151.2071, opening_hours="Mo-Fr 09:00-17:00; PH off"),
]


@pytest.fixture
def index(tmp_path, monkeypatch):
    path = tmp_path / "places.sqlite"
    build_index(ELEMENTS, path, {"source": "test", "source_timestamp": "2026-09-13T00:00:00+00:00"},
                BBOX)
    monkeypatch.setenv("SIDEQUEST_PLACES_DB", str(path))
    return PlaceIndex(path)


def request(**changes):
    return Request.model_validate({
        "departure": "2026-09-14T10:00:00+10:00", "deadline": "2026-09-14T13:00:00+10:00",
        "catalog": "osm", "venue_facts": "advisory", **changes,
    })


def at(hour):
    return datetime(2026, 9, 14, hour, tzinfo=SYDNEY)  # a Monday


def test_only_named_side_quest_kinds_inside_sydney_are_indexed(index):
    names = {r["name"] for r in index.near(*TOWN_HALL, 50)}
    assert names == {"Near Museum", "Night Gallery", "Pocket Park", "Far Park", "Mystery Hours"}


def test_radius_comes_from_the_window_not_from_the_user():
    assert radius_for(request()) > radius_for(request(deadline="2026-09-14T11:00:00+10:00"))
    assert radius_for(request(deadline="2026-09-14T22:00:00+10:00")) <= 12.0


def test_discovery_is_centred_on_the_origin(index):
    near_ids = {c.id for c in discover(request())}
    assert "osm:way/4" in near_ids  # ~4.6 km, inside a 3 h radius
    moved = request(origin_id="current", origin_lat=-33.60, origin_lon=151.30)
    assert not discover(moved)


def test_crowd_sourced_facts_never_become_verdict_inputs(index):
    museum = next(c for c in discover(request()) if c.name == "Near Museum")
    assert museum.open_at is None and museum.close_at is None and museum.cost is None
    assert museum.evidence[0].synthetic is False
    assert museum.evidence[0].source_url == "https://www.openstreetmap.org/node/1"
    assert "未核实" in museum.evidence[0].value
    assert candidate_kind(museum) == "museum" and "室内" in museum.tags


def test_a_place_shut_all_window_by_an_understood_tag_is_dropped(index):
    evening = request(departure="2026-09-14T18:30:00+10:00", deadline="2026-09-14T21:30:00+10:00")
    names = {c.name for c in discover(evening)}
    assert "Near Museum" not in names and "Night Gallery" in names
    # PH rules are not understood, so that place stays: missing facts stay missing
    assert "Mystery Hours" in names


def test_opening_hours_parser_only_rules_on_what_it_understands():
    assert shut_for_window("Tu-Su 10:00-17:00; Mo off", at(11), at(13))
    assert not shut_for_window("Mo-Su 10:00-17:00", at(16), at(18))  # overlap is enough
    assert not shut_for_window("24/7", at(2), at(4))
    assert weekly_hours("sunrise-sunset") is None
    assert weekly_hours("Mo-Fr 22:00-02:00") is None  # overnight stays unknown
    assert weekly_hours("Fr-Mo 10:00-12:00")[0] == [(600, 720)]  # wraps the week


def test_obviousness_is_a_property_of_the_place():
    assert obviousness({"wikipedia": "en:X", "wikidata": "Q1"}) > 0.5
    assert obviousness({"wikidata": "Q1"}) < 0.5
    assert obviousness({"tourism": "attraction", "wikipedia": "en:X"}) <= 1.0


def test_osm_places_plan_under_the_time_budget_verdict(index):
    target = next(c for c in discover(request()) if c.name == "Near Museum")
    result, _ = plan(request(locked_ids=[target.id], max_stops=1))
    itinerary = result.itineraries[0]
    assert itinerary.status == Status.VERIFIED
    assert any("营业时间未核实" in a for a in itinerary.advisories)
    assert "OpenStreetMap" in result.data_notice
    strict, _ = plan(request(locked_ids=[target.id], max_stops=1, venue_facts="verdict"))
    assert strict.itineraries[0].status == Status.CONDITIONAL


def test_missing_index_says_how_to_build_it(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEQUEST_PLACES_DB", str(tmp_path / "absent.sqlite"))
    with pytest.raises(PlaceIndexMissing, match="build_places"):
        discover(request())


def test_the_agent_searches_by_kind_and_bets_inside_that_kind(index):
    model = FakeModel(replies={"search_places": {"kinds": ["park"], "summary": "去公园"}})
    result = propose_quest(request(), TasteState(), model, said="想出去透透气")
    assert result.quest is not None
    assert result.quest.anchor_id in {"osm:way/3", "osm:way/4"}
    assert any(t.action == "search_places" for t in result.trace)
    assert "2026-09-14" in dict(model.seen)["search_places"]


def test_a_search_that_finds_nothing_does_not_empty_the_round(index):
    model = FakeModel(replies={"search_places": {"kinds": ["beach"], "summary": "海边"}})
    result = propose_quest(request(), TasteState(), model, said="去海边")
    assert result.quest is not None


def test_errand_kinds_are_indexed_without_a_d3_opinion(tmp_path):
    rows = [
        element(20, "Corner Store", -33.8736, 151.2068, kind=("shop", "convenience")),
        element(21, "Tea Lab", -33.8737, 151.2069, kind=("amenity", "cafe"),
                cuisine="bubble_tea;coffee"),
        element(22, "Flat White", -33.8738, 151.2069, kind=("amenity", "cafe"),
                wikipedia="en:Flat White"),
    ]
    path = tmp_path / "errands.sqlite"
    build_index(rows, path, {"source": "test", "source_timestamp": "2026-09-13T00:00:00+00:00"},
                BBOX)
    found = {r["name"]: r for r in PlaceIndex(path).near(*TOWN_HALL, 1)}
    assert found["Tea Lab"]["kind"] == "bubble_tea"  # refined kind wins over plain cafe
    assert found["Flat White"]["kind"] == "cafe"
    assert all(r["obviousness"] is None for r in found.values())


def test_thirty_minutes_never_reach_a_museum_but_do_reach_the_corner_shop(tmp_path, monkeypatch):
    rows = [*ELEMENTS, element(20, "Corner Store", -33.8736, 151.2068, kind=("shop", "convenience"))]
    path = tmp_path / "errands.sqlite"
    build_index(rows, path, {"source": "test", "source_timestamp": "2026-09-13T00:00:00+00:00"},
                BBOX)
    monkeypatch.setenv("SIDEQUEST_PLACES_DB", str(path))
    short = request(deadline="2026-09-14T10:30:00+10:00")
    names = {c.name for c in discover(short)}
    assert "Corner Store" in names and "Near Museum" not in names
    model = FakeModel(replies={"search_places": {"kinds": ["convenience"], "summary": "楼下"}})
    result = propose_quest(short, TasteState(), model, said="有半小时", seed=7)
    assert result.quest.anchor_id == "osm:node/20"
    assert result.quest.itinerary.return_at <= short.deadline
    narration = dict(model.seen)["narrate_quest"]
    assert "买一样没吃过的零食" in narration


def test_a_short_window_reaches_the_beach_as_a_short_visit(tmp_path, monkeypatch):
    rows = [element(30, "Bondi Beach", -33.8908, 151.2743, kind=("natural", "beach"), way=True)]
    path = tmp_path / "beach.sqlite"
    build_index(rows, path, {"source": "test", "source_timestamp": "2026-09-13T00:00:00+00:00"},
                BBOX)
    monkeypatch.setenv("SIDEQUEST_PLACES_DB", str(path))
    at_bondi = {"origin_id": "current", "origin_lat": -33.8915, "origin_lon": 151.2767}
    short = discover(request(deadline="2026-09-14T10:30:00+10:00", **at_bondi))
    assert [c.name for c in short] == ["Bondi Beach"]
    assert short[0].stay == 15 and "visit:short" in short[0].tags
    assert action_for(short[0]) == "走到水边站一会儿再回来"
    long = discover(request(**at_bondi))  # 3 h: the full hour on the sand fits
    assert long[0].stay == 60 and "visit:short" not in long[0].tags
    assert action_for(long[0]) == ""


def test_a_place_mapped_twice_is_offered_once_but_chain_branches_stay():
    rows = [
        {"name": "Ben & Jerry's", "lat": -33.8910, "lon": 151.2770, "km": 0.1},
        {"name": "ben & jerry’s ", "lat": -33.8911, "lon": 151.2771, "km": 0.11},  # same shop
        {"name": "Ben & Jerry's", "lat": -33.8700, "lon": 151.2070, "km": 6.8},  # other branch
    ]
    kept = dedupe(rows)
    assert [r["km"] for r in kept] == [0.1, 6.8]


# The reported day: "想去海港边坐坐" from home, a harbour beach ~4.5 km east.
HARBOUR_DAY = [
    element(40, "Corner Store", -33.8736, 151.2068, kind=("shop", "convenience")),
    element(41, "Home Cafe", -33.8737, 151.2069, kind=("amenity", "cafe")),
    element(42, "Pocket Park", -33.8745, 151.2080, kind=("leisure", "park"), way=True),
    element(43, "Harbour Beach", -33.8696, 151.2540, kind=("natural", "beach"), way=True),
    element(44, "Next Beach", -33.8700, 151.2590, kind=("natural", "beach"), way=True),
    element(45, "Beach Kiosk", -33.8703, 151.2545, kind=("amenity", "cafe")),
]
BEACH = "osm:way/43"


@pytest.fixture
def harbour(tmp_path, monkeypatch):
    path = tmp_path / "harbour.sqlite"
    build_index(HARBOUR_DAY, path,
                {"source": "test", "source_timestamp": "2026-09-13T00:00:00+00:00"}, BBOX)
    monkeypatch.setenv("SIDEQUEST_PLACES_DB", str(path))


def day(**changes):
    return request(deadline="2026-09-14T18:00:00+10:00", **changes)


def test_side_stops_around_a_lock_are_near_the_lock_not_near_home(harbour):
    """Seen live: corner shop at home -> beach 4 km away -> cafe at home."""
    result, _ = plan(day(locked_ids=[BEACH]))
    assert result.itineraries
    for itinerary in result.itineraries:
        beach = next(s.candidate for s in itinerary.stops if s.candidate.id == BEACH)
        others = [s.candidate for s in itinerary.stops if s.candidate.id != BEACH]
        assert all(distance(beach.model_dump(), c.model_dump()) <= SIDE_KM for c in others)
        assert all(c.category != beach.category for c in others)  # not a second beach
    assert any(s.candidate.name == "Beach Kiosk" for i in result.itineraries for s in i.stops)


def test_a_named_kind_goes_first_even_when_the_search_mixes_in_others(harbour):
    for seed in range(5):
        model = FakeModel(replies={
            "infer_intent": {"form": "outdoor", "places": ["beach"], "summary": "想去海边坐坐"},
            "search_places": {"kinds": ["park", "cafe"]},  # the model forgot the beach
        })
        result = propose_quest(day(), TasteState(), model, said="想去海港边坐坐", seed=seed)
        assert result.quest.anchor_id in {BEACH, "osm:way/44"}
    assert "可选类型" in dict(model.seen)["infer_intent"]


def test_a_named_kind_does_not_overrule_what_the_user_settled_this_session(harbour):
    """After "太远了" on the beach, a far beach must not jump the queue."""
    state = TasteState()
    state.apply(Feedback(quest_id="q", candidate_ids=[BEACH], reason=Reason.TOO_FAR))
    model = FakeModel(replies={
        "infer_intent": {"form": "outdoor", "places": ["beach"], "summary": "想去海边坐坐"},
        "search_places": {"kinds": ["park", "beach"]},
    })
    result = propose_quest(day(), state, model, said="想去海港边坐坐", seed=1)
    assert result.quest.anchor_id == "osm:way/42"
    assert any("没有符合条件" in t.summary for t in result.trace)


def test_keeping_the_anchor_rebets_on_it_without_probing_or_searching(harbour):
    state = TasteState()
    state.apply(Feedback(quest_id="q", candidate_ids=["osm:node/45"], anchor_id=BEACH,
                         reason=Reason.OFF_ROUTE))
    model = FakeModel()
    result = propose_quest(day(), state, model, said="想去海港边坐坐", keep=BEACH, seed=3)
    assert result.quest.anchor_id == BEACH
    assert "osm:node/45" not in {s.candidate.id for s in result.quest.itinerary.stops}
    assert [name for name, _ in model.seen] == ["infer_intent", "narrate_quest"]
