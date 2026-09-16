from datetime import date, datetime, time

import pytest
from sidequest.agent import (
    Intent,
    Probe,
    probeable,
    propose_quest,
    score,
    search_places,
    targets,
    why_rejected,
)
from sidequest.fixtures import candidates
from sidequest.llm import ModelBudgetExhausted, ModelSchemaError
from sidequest.memory import Key, MemoryItem, context_of
from sidequest.models import Request
from sidequest.moment import memory_facts, sunset
from sidequest.planner import request_origin
from sidequest.taste import (
    Dimension,
    Feedback,
    Reason,
    TasteState,
    dimension_key,
)


class FakeModel:
    """Scripted replies, no network. The agent must not care which provider it talks to."""

    def __init__(self, replies=None, limit=6):
        self.replies = replies or {}
        self.limit = limit
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.seen = []

    def decide(self, name, system, user, spec):
        if self.calls >= self.limit:
            raise ModelBudgetExhausted("模型决策预算已耗尽")
        self.calls += 1
        self.seen.append((name, user))
        base = {
            "infer_intent": {"form": "either", "time_source": "other",
                             "keywords": [], "inferred": [], "summary": "想出去走走"},
            "choose_probe": {"dimension": "none", "lean": "outdoor", "mode": "explore",
                             "summary": "先看看"},
            "search_places": {"kinds": [], "summary": "不限类型"},
            "interpret_feedback": {"reason": "none", "lasting": "", "summary": "对不上"},
            "narrate_quest": {"brief": "一段短程支线", "hook": "现在出门刚好",
                              "evidence_ids": [], "summary": "写好了"},
        }[name]
        return {**base, **self.replies.get(name, {})}


def request(**changes):
    return Request.model_validate(
        {"departure": "2026-09-14T10:00:00+10:00", "deadline": "2026-09-14T14:00:00+10:00",
         **changes}
    )


def run(state=None, **replies):
    model = FakeModel(replies=replies)
    return propose_quest(request(), state or TasteState(), model, said="出去走走"), model


def test_loop_returns_a_quest_backed_by_a_validated_itinerary():
    result, model = run()
    assert result.quest is not None
    assert result.quest.itinerary.status in ("verified", "conditional")
    assert all(c.status != "fail" for c in result.quest.itinerary.checks)
    assert model.calls == 3  # intent, probe, narrate


def test_probe_changes_which_candidate_the_agent_bets_on():
    """The point of choose_probe: a reroll must move in a direction, not at random."""
    indoor, _ = run(choose_probe={"dimension": "D1", "lean": "indoor"})
    outdoor, _ = run(choose_probe={"dimension": "D1", "lean": "outdoor"})
    pool = {c.id: c for c in candidates(request())}
    assert indoor.quest.anchor_id != outdoor.quest.anchor_id
    assert "室内" in pool[indoor.quest.anchor_id].tags
    assert "户外" in pool[outdoor.quest.anchor_id].tags


def test_obviousness_is_not_a_probe_dimension():
    result, _ = run(choose_probe={"dimension": "D3", "lean": "offbeat"})
    assert result.quest.probe.dimension is None


def remembering(key, context=None):
    """A state whose memory already holds one confirmed item."""
    state = TasteState()
    state.memory.items.append(MemoryItem(key=key, context=context, source="agent_proposed",
                                         episode_ids=["e1", "e2", "e3"]))
    return state


def test_a_remembered_item_outranks_an_inferred_opening():
    state = remembering(dimension_key(Dimension.FORM, 0))
    aim = targets(Intent(form="indoor", inferred=["form"]), state, Probe())
    assert aim[Dimension.FORM] == 0


def test_what_the_user_said_this_time_outranks_memory():
    """Plan v2.2 §3.2: the current explicit request beats anything remembered."""
    state = remembering(dimension_key(Dimension.FORM, 0))
    aim = targets(Intent(form="indoor"), state, Probe())
    assert aim[Dimension.FORM] == 1


def test_this_sessions_feedback_outranks_what_was_said_at_the_start():
    state = TasteState()
    state.apply(Feedback(quest_id="q", reason=Reason.WANT_MOVE))
    aim = targets(Intent(form="indoor"), state, Probe())
    assert aim[Dimension.FORM] == 0


def test_an_explicit_probe_outranks_a_learned_belief():
    state = remembering(dimension_key(Dimension.FORM, 0))
    aim = targets(Intent(), state, Probe(dimension=Dimension.FORM, pole=1))
    assert aim[Dimension.FORM] == 1


def test_visited_candidates_are_pushed_down_not_banned():
    req = request()
    target = {c.id: c for c in candidates(req)}["gallery"]
    aim = {Dimension.FORM: 1}
    origin = request_origin(req)
    span = (req.departure, req.deadline)
    fresh = score(target, aim, origin, {}, window=span)
    seen = score(target, aim, origin, {"gallery": "visited"}, window=span)
    assert seen < fresh


def test_fabricated_evidence_ids_are_dropped():
    result, _ = run(narrate_quest={"evidence_ids": ["fixture:gallery:hours:2026-09-14",
                                                    "totally-made-up"]})
    assert "totally-made-up" not in result.quest.evidence_ids


def test_the_model_sees_the_date_and_what_is_unverified(monkeypatch):
    def hours_missing(req):
        return [c.model_copy(update={"open_at": None, "close_at": None}) if c.id == "gallery"
                else c for c in candidates(req)]
    monkeypatch.setattr("sidequest.planner.candidates", hours_missing)
    model = FakeModel()
    req = request(venue_facts="advisory")
    result = propose_quest(req, TasteState(), model, said="出去走走")
    prompts = dict(model.seen)
    assert "2026-09-14" in prompts["infer_intent"]
    assert f"未核实：{result.quest.itinerary.advisories or '无'}" in prompts["narrate_quest"]


@pytest.mark.parametrize("day, expected", [
    (date(2026, 9, 14), "17:45"), (date(2026, 12, 21), "20:05"), (date(2026, 6, 21), "16:53"),
])
def test_sunset_matches_published_sydney_times(day, expected):
    at = sunset(day, -33.8688, 151.2093)
    published = datetime.combine(day, time.fromisoformat(expected), at.tzinfo)
    assert abs((at - published).total_seconds()) <= 180


def test_the_hook_gets_facts_about_now_and_may_cite_them():
    model = FakeModel(replies={"narrate_quest": {
        "evidence_ids": ["clock:window", "clock:sunset:2026-09-14", "made-up"]}})
    result = propose_quest(request(), TasteState(), model, said="出去走走")
    prompt = dict(model.seen)["narrate_quest"]
    quest = result.quest
    assert "空闲 240 分钟" in prompt
    assert all(leg.evidence.id in prompt for leg in quest.itinerary.legs)
    assert quest.evidence_ids == ["clock:window"]


def test_a_sunset_hours_away_is_not_a_reason():
    """10:00-14:00 with sunset at 17:45: a real model turned "天不会黑" into "正好在日落前回到"."""
    model = FakeModel()
    propose_quest(request(), TasteState(), model, said="出去走走")
    assert "日落" not in dict(model.seen)["narrate_quest"].split("此刻：")[1]


def test_narration_is_told_which_stop_the_agent_bet_on():
    """plan() may put a filler stop first; the title must not follow it."""
    model = FakeModel()
    quest = propose_quest(request(), TasteState(), model, said="出去走走", seed=1).quest
    stops = quest.itinerary.stops
    assert len(stops) > 1
    prompt = dict(model.seen)["narrate_quest"]
    anchor = next(s.candidate.name for s in stops if s.candidate.id == quest.anchor_id)
    assert f"'name': '{anchor}', 'role': '主要目的'" in prompt
    assert prompt.count("'role': '顺路'") == len(stops) - 1


def test_sunset_is_placed_relative_to_the_trip():
    evening = request(departure="2026-09-14T18:30:00+10:00", deadline="2026-09-14T21:00:00+10:00")
    model = FakeModel()
    propose_quest(evening, TasteState(), model, said="出去走走")
    assert "出发时已经日落 45 分钟" in dict(model.seen)["narrate_quest"]
    afternoon = request(departure="2026-09-14T15:00:00+10:00", deadline="2026-09-14T17:15:00+10:00")
    model = FakeModel()
    back = propose_quest(afternoon, TasteState(), model, said="出去走走", seed=1).quest.itinerary.return_at
    before = round((datetime.combine(back.date(), time(17, 45), back.tzinfo) - back).total_seconds() / 60)
    assert f"{back:%H:%M} 回到，再过 {before} 分钟日落" in dict(model.seen)["narrate_quest"]


def test_a_remembered_preference_behind_the_pick_reaches_the_hook():
    state = remembering(dimension_key(Dimension.FORM, 1))
    model = FakeModel()
    result = propose_quest(request(), state, model, said="出去走走", seed=1)
    item = state.memory.items[0]
    assert "室内" in {c.id: c for c in candidates(request())}[result.quest.anchor_id].tags
    assert item.id in result.quest.memory_ids
    assert f"'{item.id}': '你确认过的偏好" in dict(model.seen)["narrate_quest"]


def test_only_memory_the_pick_agrees_with_becomes_a_reason():
    pool = {c.id: c for c in candidates(request())}
    liked = MemoryItem(key=dimension_key(Dimension.FORM, 1), source="user_stated")
    against = MemoryItem(key=dimension_key(Dimension.FORM, 0), source="user_stated")
    disliked = MemoryItem(key=Key(kind="category", key="gallery", value="dislike"),
                          source="user_stated")
    favorite = MemoryItem(key=Key(kind="place", key="gallery", value="favorite"),
                          source="user_stated")
    note = MemoryItem(key=Key(kind="note", key="喜欢安静", value="note"), source="user_stated")
    facts = memory_facts([liked, against, disliked, favorite, note], pool["gallery"], "gallery",
                         {"gallery": pool["gallery"].name})
    assert [f.id for f in facts] == [liked.id, favorite.id]
    assert pool["gallery"].name in facts[1].text


def test_narration_without_a_brief_is_rejected():
    with pytest.raises(ModelSchemaError):
        run(narrate_quest={"brief": ""})


def test_model_budget_is_enforced():
    model = FakeModel(limit=1)
    with pytest.raises(ModelBudgetExhausted):
        propose_quest(request(), TasteState(), model, said="出去走走")


def test_the_executor_still_decides_feasibility():
    """A model preference cannot stretch the clock: the deadline is the executor's."""
    req = request(deadline="2026-09-14T10:40:00+10:00")
    result = propose_quest(req, TasteState(), FakeModel(), said="只有半小时")
    if result.quest:
        assert result.quest.itinerary.return_at <= req.deadline
        assert all(c.status != "fail" for c in result.quest.itinerary.checks)


def test_shown_candidates_are_recorded_for_dedup():
    state = TasteState()
    result, _ = run(state)
    assert set(state.consumed) >= {s.candidate.id for s in result.quest.itinerary.stops}
    assert set(state.consumed.values()) == {"shown"}


def evening(**changes):
    """18:00 in Sydney: every indoor fixture has shut, harbour and park are still open."""
    return request(
        departure="2026-09-14T18:00:00+10:00", deadline="2026-09-14T21:00:00+10:00", **changes
    )


def test_probe_cannot_overrule_what_the_user_just_said():
    """The user said want_move this turn; a probe must not flip D1 back to indoor."""
    state = TasteState()
    state.apply(Feedback(quest_id="q", reason=Reason.WANT_MOVE))
    aim = targets(Intent(), state, Probe(dimension=Dimension.FORM, pole=1))
    assert aim[Dimension.FORM] == 0


def test_settled_dimensions_are_not_offered_to_the_model():
    state = TasteState()
    assert probeable(state) == [Dimension.FORM]
    state.apply(Feedback(quest_id="q", reason=Reason.TOO_OBVIOUS))
    assert probeable(state) == [Dimension.FORM]  # local popularity feedback is not a belief


def test_d2_probe_is_only_offered_for_long_windows_without_d2_evidence():
    state = TasteState()
    assert probeable(state, 120) == [Dimension.FORM]
    assert probeable(state, 121) == [Dimension.FORM, Dimension.TRAVEL]
    state.apply(Feedback(quest_id="q", reason=Reason.TOO_FAR))
    assert probeable(state, 240) == [Dimension.FORM]


def test_a_long_window_can_probe_d2_without_changing_hard_constraints():
    model = FakeModel(replies={"choose_probe": {"dimension": "D2", "lean": "farther"}})
    result = propose_quest(request(), TasteState(), model, said="随便走走")
    assert result.quest is not None
    assert result.quest.probe.dimension is Dimension.TRAVEL
    assert result.quest.probe.pole == 1
    assert result.quest.itinerary.walking_minutes <= request().max_walk_minutes


def test_a_forbidden_probe_is_ignored_even_if_the_model_returns_one():
    state = TasteState()
    state.apply(Feedback(quest_id="q", reason=Reason.WANT_MOVE))
    model = FakeModel(replies={"choose_probe": {"dimension": "D1", "lean": "indoor"}})
    result = propose_quest(evening(), state, model, said="走走")
    assert result.quest is None or result.quest.probe.dimension is not Dimension.FORM


def closing_soon(**changes):
    """16:30-17:30: indoor fixtures are open but cannot fit their stay before 17:00."""
    return request(
        departure="2026-09-14T16:30:00+10:00", deadline="2026-09-14T17:30:00+10:00", **changes
    )


class FakeResult:
    def __init__(self, rejected, status="search_exhausted", message="调用预算耗尽"):
        self.rejected, self.status, self.message = rejected, status, message


def test_why_rejected_prefers_the_validator_detail_over_the_run_message():
    candidate = {c.id: c for c in candidates(request())}["gallery"]
    result = FakeResult([{"candidates": ["gallery"], "reasons": ["停留须位于开放窗口"]}])
    assert why_rejected(result, candidate) == "停留须位于开放窗口"
    assert "调用预算" not in why_rejected(result, candidate)


def test_why_rejected_keeps_the_specific_message_when_input_is_needed():
    candidate = {c.id: c for c in candidates(request())}["gallery"]
    result = FakeResult([], status="needs_input", message="锁定的站点已关闭。")
    assert why_rejected(result, candidate) == "锁定的站点已关闭。"


def test_rejection_reports_the_real_check_not_the_budget_line():
    """plan()'s run-level line blames the search budget; the honest reason is the clock."""
    model = FakeModel(replies={"choose_probe": {"dimension": "D1", "lean": "indoor"}})
    result = propose_quest(closing_soon(), TasteState(), model, said="出去走走")
    rejects = [t.summary for t in result.trace if t.action == "reject"]
    assert rejects
    assert not any("调用预算" in r for r in rejects)
    assert any("开放窗口" in r or "返回" in r for r in rejects)


def test_a_dead_end_probe_falls_back_to_known_preferences():
    """Indoor stays cannot finish before closing, but the hour is not actually hopeless."""
    model = FakeModel(replies={"choose_probe": {"dimension": "D1", "lean": "indoor"}})
    result = propose_quest(closing_soon(), TasteState(), model, said="出去走走")
    assert any(t.action == "drop_probe" for t in result.trace)
    assert result.quest is not None
    pool = {c.id: c for c in candidates(closing_soon())}
    assert "户外" in pool[result.quest.anchor_id].tags


def test_a_venue_shut_for_the_whole_window_is_outranked_by_an_open_one():
    req = evening()  # 18:00: the gallery shut at 17:00, the harbour runs to 22:00
    pool = {c.id: c for c in candidates(req)}
    aim, origin = {Dimension.FORM: 1}, request_origin(req)
    span = (req.departure, req.deadline)
    shut = score(pool["gallery"], aim, origin, {}, window=span)   # matches the aim, but closed
    open_ = score(pool["harbour"], aim, origin, {}, window=span)  # misses the aim, but open
    assert open_ > shut


def test_a_reroll_never_hands_back_what_was_just_rejected():
    """The one thing a reroll must not do."""
    state = TasteState()
    first, _ = run(state, choose_probe={"dimension": "D1", "lean": "outdoor"})
    shown = [s.candidate.id for s in first.quest.itinerary.stops]
    state.apply(Feedback(quest_id="q", candidate_ids=shown[:3], reason=Reason.TOO_OBVIOUS))
    second = propose_quest(request(), state, FakeModel(), said="再来一个")
    assert second.quest is not None
    assert second.quest.anchor_id not in shown
    assert not {s.candidate.id for s in second.quest.itinerary.stops} & set(shown)


def test_between_equally_good_fits_the_unseen_one_wins():
    """Both are indoor and both match the aim; only one has already been on screen."""
    req = request()
    pool = {c.id: c for c in candidates(req)}
    aim, origin, span = {Dimension.FORM: 1}, request_origin(req), (req.departure, req.deadline)
    shown = score(pool["gallery"], aim, origin, {"gallery": "shown"}, window=span)
    unseen = score(pool["museum"], aim, origin, {}, window=span)
    assert unseen > shown


def test_a_retired_d3_lean_is_ignored():
    model = FakeModel(replies={"choose_probe": {"dimension": "D1", "lean": "offbeat"}})
    result = propose_quest(request(), TasteState(), model, said="走走")
    assert result.quest.probe.dimension is None


def test_a_place_rejected_two_rounds_ago_stays_gone_after_an_empty_round():
    """The real CLI run: reject the only two open places, get nothing, reroll again."""
    state = TasteState()
    state.apply(Feedback(quest_id="q1", candidate_ids=["harbour", "park"],
                         reason=Reason.TOO_OBVIOUS))
    state.apply(Feedback(quest_id="-", candidate_ids=[], reason=Reason.WANT_MOVE))
    model = FakeModel()
    result = propose_quest(evening(), state, model, said="出去走走")
    assert result.quest is None
    assert "Circular Quay waterfront" in result.message and "Hyde Park" in result.message
    assert "放宽" not in result.message
    assert model.calls == 0  # nothing could come of this round, so no model spend


def test_venues_shut_for_the_whole_window_are_never_sent_to_the_planner():
    model = FakeModel(replies={"choose_probe": {"dimension": "D1", "lean": "indoor"}})
    result = propose_quest(evening(), TasteState(), model, said="出去走走")
    shut = {"Art Gallery of NSW", "State Library of NSW", "Australian Museum"}
    rejects = [t.summary for t in result.trace if t.action == "reject"]
    assert not any(name in r for r in rejects for name in shut)


def test_why_rejected_shows_only_this_candidates_failure_once():
    candidate = {c.id: c for c in candidates(request())}["library"]
    result = FakeResult([{
        "candidates": ["library", "museum"],
        "reasons": ["Australian Museum：停留须位于开放窗口", "State Library of NSW：停留须位于开放窗口"],
    }])
    assert why_rejected(result, candidate) == "停留须位于开放窗口"


def test_the_same_seed_reproduces_the_draw_and_records_it():
    first = propose_quest(request(), TasteState(), FakeModel(), said="走走", seed=42)
    again = propose_quest(request(), TasteState(), FakeModel(), said="走走", seed=42)
    assert first.seed == again.seed == 42
    assert first.quest.anchor_id == again.quest.anchor_id
    assert any("随机种子 42" in t.summary for t in first.trace)


def test_different_seeds_vary_the_bet_among_equal_fits():
    anchors = {propose_quest(request(), TasteState(), FakeModel(), said="走走", seed=s)
               .quest.anchor_id for s in range(30)}
    assert len(anchors) > 1


def test_the_draw_never_trades_a_dimension_hit_for_variety():
    pool = {c.id: c for c in candidates(request())}
    for s in range(30):
        model = FakeModel(replies={"choose_probe": {"dimension": "D1", "lean": "indoor"}})
        result = propose_quest(request(), TasteState(), model, said="走走", seed=s)
        assert "室内" in pool[result.quest.anchor_id].tags


def test_a_place_the_user_never_wants_again_stays_gone_in_a_new_session():
    first, _ = run()
    anchor = first.quest.anchor_id
    state = TasteState()
    outcome = state.apply(Feedback(quest_id="q", candidate_ids=[anchor], anchor_id=anchor,
                                   reason=Reason.NEVER_HERE))
    state.confirm(outcome.stated)
    for seed in range(10):
        later = TasteState(memory=state.memory)  # a new session: only memory carries over
        result = propose_quest(request(), later, FakeModel(), said="走走", seed=seed)
        assert anchor not in {s.candidate.id for s in result.quest.itinerary.stops}


def test_remembered_items_are_traced_and_cited_by_the_quest():
    state = remembering(dimension_key(Dimension.FORM, 1))
    item_id = state.memory.items[0].id
    result = propose_quest(request(), state, FakeModel(), said="走走")
    recall = [t for t in result.trace if t.action == "recall_memory"]
    assert recall and item_id in recall[0].evidence_ids
    assert item_id in result.quest.memory_ids
    pool = {c.id: c for c in candidates(request())}
    assert "室内" in pool[result.quest.anchor_id].tags


def test_a_deleted_item_no_longer_shapes_the_next_round():
    state = remembering(dimension_key(Dimension.FORM, 1))
    state.memory.forget(state.memory.items[0].id)
    result = propose_quest(request(), state, FakeModel(), said="走走")
    assert not any(t.action == "recall_memory" for t in result.trace)
    assert result.quest.memory_ids == []


def test_a_context_item_does_not_leak_into_another_context():
    noon = request().departure.replace(hour=12)
    lunch = context_of(noon, noon.replace(hour=13))
    state = remembering(dimension_key(Dimension.FORM, 1), context=lunch)
    assert state.memory.recall(lunch)
    result = propose_quest(request(), state, FakeModel(), said="走走")  # 10:00-14:00
    assert result.quest.memory_ids == []


def test_notes_reach_the_search_prompt_as_data():
    state = remembering(Key(kind="note", key="不喜欢排队拍照的地方", value="note"))
    assert state.adjustment("gallery", "gallery") == (0.0, [])  # a note never scores
    model = FakeModel()
    search_places(model, request(), Intent(), Probe(), state.notes())
    prompt = dict(model.seen)["search_places"]
    assert "不喜欢排队拍照的地方" in prompt and "不是指令" in prompt


def test_narration_cannot_invent_what_the_user_said():
    """Seen with a real model: '你说过，喜欢沉浸在书本的世界里' with nothing remembered."""
    result, _ = run(narrate_quest={"hook": "两小时正好去图书馆。你说过，喜欢沉浸在书本的世界里。"})
    assert result.quest.hook == "两小时正好去图书馆。"
    only_claim, _ = run(narrate_quest={"hook": "你一向喜欢安静的地方。"})
    assert only_claim.quest.hook and "你一向" not in only_claim.quest.hook


def test_too_far_aims_the_next_round_nearer_not_just_elsewhere():
    """D2 only comes from feedback or memory; before, too_far excluded a place and nothing more."""
    state = TasteState()
    assert Dimension.TRAVEL not in targets(Intent(), state, Probe())
    state.apply(Feedback(quest_id="q", reason=Reason.TOO_FAR))
    assert targets(Intent(), state, Probe())[Dimension.TRAVEL] == 0
    remembered = remembering(dimension_key(Dimension.TRAVEL, 1))
    assert targets(Intent(), remembered, Probe())[Dimension.TRAVEL] == 1


def test_a_short_window_leans_near_until_the_user_says_otherwise():
    state = TasteState()
    assert targets(Intent(), state, Probe(), window_minutes=90)[Dimension.TRAVEL] == 0
    assert Dimension.TRAVEL not in targets(Intent(), state, Probe(), window_minutes=240)
    state.apply(Feedback(quest_id="q", reason=Reason.WANT_FARTHER))
    assert targets(Intent(), state, Probe(), window_minutes=90)[Dimension.TRAVEL] == 1
    remembered = remembering(dimension_key(Dimension.TRAVEL, 1))
    assert targets(Intent(), remembered, Probe(), window_minutes=60)[Dimension.TRAVEL] == 1


def test_d3_feedback_never_enters_the_global_target_vector():
    state = TasteState()
    state.apply(Feedback(quest_id="q", reason=Reason.TOO_OBVIOUS))
    assert Dimension.OBVIOUSNESS not in targets(Intent(), state, Probe())
    state.apply(Feedback(quest_id="q", reason=Reason.TOO_OBSCURE))
    assert Dimension.OBVIOUSNESS not in targets(Intent(), state, Probe())
