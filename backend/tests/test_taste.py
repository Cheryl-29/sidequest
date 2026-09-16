from datetime import datetime, timedelta

import pytest
from sidequest.memory import STALE_SESSIONS, MemoryItem, context_of
from sidequest.models import SYDNEY
from sidequest.taste import (
    CONSTRAINT_REASONS,
    DEDUP_REASONS,
    KIND_REASONS,
    LOCAL_REASONS,
    MEMORY_CAP,
    SCOPES,
    SESSION_REASONS,
    STATED_REASONS,
    TASTE_REASONS,
    Belief,
    Dimension,
    Feedback,
    Reason,
    TasteState,
    category_key,
    dimension_key,
    place_key,
)

LUNCH = context_of(datetime(2026, 9, 14, 12, 0, tzinfo=SYDNEY),
                   datetime(2026, 9, 14, 13, 0, tzinfo=SYDNEY))


def fb(reason, candidates=("gallery",), quest="q1", kind=None, anchor=None):
    return Feedback(quest_id=quest, candidate_ids=list(candidates), reason=reason, kind=kind,
                    anchor_id=anchor)


def push(state, reason, times=1, **kw):
    return [state.apply(fb(reason, **kw)) for _ in range(times)][-1]


def in_lunch():
    return TasteState(context=LUNCH)


def test_every_reason_belongs_to_exactly_one_scope():
    assert set().union(*SCOPES) == set(Reason)
    for i, a in enumerate(SCOPES):
        for b in SCOPES[i + 1 :]:
            assert not a & b
    assert Reason.NOT_THIS_KIND in KIND_REASONS and Reason.NEVER_HERE in STATED_REASONS
    assert {Reason.NO_SPEND, Reason.BAD_TIME} == CONSTRAINT_REASONS
    assert {Reason.BEEN_THERE} == DEDUP_REASONS
    assert {Reason.OTHER} == SESSION_REASONS
    assert LOCAL_REASONS == {Reason.TOO_OBVIOUS, Reason.TOO_OBSCURE}


@pytest.mark.parametrize(
    "reason,dimension,pole",
    [
        (Reason.WANT_SIT, Dimension.FORM, 1),
        (Reason.WANT_MOVE, Dimension.FORM, 0),
        (Reason.TOO_FAR, Dimension.TRAVEL, 0),
        (Reason.WANT_FARTHER, Dimension.TRAVEL, 1),
    ],
)
def test_taste_reason_moves_only_its_own_dimension(reason, dimension, pole):
    state = TasteState()
    outcome = state.apply(fb(reason))
    assert outcome.dimension is dimension
    assert list(state.session) == [dimension]
    assert state.session[dimension].value == pole
    assert TASTE_REASONS[reason] == (dimension, pole)


def test_been_there_records_consumption_without_touching_taste():
    state = in_lunch()
    outcome = state.apply(fb(Reason.BEEN_THERE, candidates=["gallery", "library"]))
    assert state.session == {}
    assert outcome.dimension is None
    assert state.consumed == {"gallery": "visited", "library": "visited"}
    assert all(not e.signals for e in state.memory.episodes)


@pytest.mark.parametrize("reason", [Reason.NO_SPEND, Reason.BAD_TIME, Reason.BEEN_THERE,
                                    Reason.OFF_ROUTE, Reason.OTHER])
def test_non_taste_reasons_are_evidence_for_nothing(reason):
    state = in_lunch()
    push(state, reason, times=5, kind="museum")
    assert all(not e.signals for e in state.memory.episodes)
    assert state.proposals() == []


def test_off_route_keeps_the_anchor_and_learns_nothing():
    """"咖啡店离海滩太远" is about a side stop; it says nothing about travel willingness."""
    state = in_lunch()
    outcome = state.apply(fb(Reason.OFF_ROUTE, candidates=["cafe"], anchor="beach"))
    assert outcome.keep == "beach"
    assert state.rejected == ["cafe"] and state.session == {}
    assert outcome.dimension is None and outcome.request_patch == {}


def test_no_spend_patches_the_request_without_touching_taste():
    state = TasteState()
    outcome = state.apply(fb(Reason.NO_SPEND))
    assert outcome.request_patch == {"budget_aud": 0.0}
    assert state.session == {}


def test_bad_time_asks_instead_of_inventing_a_time():
    state = TasteState()
    outcome = state.apply(fb(Reason.BAD_TIME))
    assert outcome.clarify == "time"
    assert outcome.request_patch == {}
    assert state.session == {}


def test_too_far_never_becomes_a_walking_limit():
    state = in_lunch()
    outcome = push(state, Reason.TOO_FAR, times=5)
    assert outcome.request_patch == {}
    assert state.session[Dimension.TRAVEL].value == 0
    assert {s.key.kind for e in state.memory.episodes for s in e.signals} == {"dimension"}


def test_not_this_kind_penalises_the_kind_for_the_session_only():
    state = in_lunch()
    outcome = push(state, Reason.NOT_THIS_KIND, kind="museum")
    assert outcome.kind == "museum"
    assert state.session == {}
    assert state.adjustment("x", "museum")[0] < -3.0  # stronger than one dimension hit
    assert state.adjustment("x", "gallery")[0] == 0
    assert TasteState(memory=state.memory, context=LUNCH).adjustment("x", "museum")[0] == 0


def test_popularity_feedback_is_category_local_one_shot_and_not_memory():
    state = in_lunch()
    outcome = state.apply(Feedback(quest_id="q", candidate_ids=["gallery"],
                                   kind="gallery", anchor_obviousness=0.8,
                                   reason=Reason.TOO_OBVIOUS))
    assert outcome.local and outcome.dimension is None and state.session == {}
    assert state.adjustment("cold", "gallery", 0.2)[0] > state.adjustment(
        "known", "gallery", 0.9)[0]
    assert state.adjustment("museum", "museum", 0.1)[0] == 0
    assert all(not episode.signals for episode in state.memory.episodes)
    assert state.proposals() == []
    state.clear_local_reroll()
    assert state.adjustment("cold", "gallery", 0.2)[0] == 0


def test_legacy_d3_memory_is_ignored():
    state = in_lunch()
    state.memory.items.append(MemoryItem(key=dimension_key(Dimension.OBVIOUSNESS, 0),
                                         source="agent_proposed", episode_ids=["old-d3"]))
    assert state.recall() == []
    assert state.effective(Dimension.OBVIOUSNESS) is None


def test_never_here_is_a_statement_that_still_needs_confirm():
    state = in_lunch()
    outcome = state.apply(fb(Reason.NEVER_HERE, candidates=["gallery", "park"], anchor="park"))
    assert outcome.stated.key == place_key("park", "never")
    assert outcome.stated.source == "user_stated"
    assert state.memory.items == []  # apply() never writes memory
    state.confirm(outcome.stated)
    assert state.banned() == {"park"}


def test_apply_never_writes_memory():
    state = in_lunch()
    for session in range(3):
        state.session_id = f"s{session}"
        push(state, Reason.WANT_SIT, times=3)
    assert state.memory.items == []
    state.confirm(state.proposals()[0])
    assert state.memory.items


def test_session_evidence_overrides_memory():
    state = TasteState(context=LUNCH)
    state.memory.items.append(MemoryItem(key=dimension_key(Dimension.FORM, 1),
                                         source="agent_proposed", episode_ids=["a", "b", "c"]))
    assert state.effective(Dimension.FORM).value == 1
    push(state, Reason.WANT_MOVE)
    assert state.effective(Dimension.FORM).value == 0


def test_memory_survives_when_the_session_is_silent():
    state = TasteState(context=LUNCH)
    state.memory.items.append(MemoryItem(key=dimension_key(Dimension.TRAVEL, 0),
                                         source="agent_proposed", episode_ids=["a"]))
    push(state, Reason.WANT_SIT)
    assert state.effective(Dimension.TRAVEL).value == 0


def test_uncertainty_falls_as_observations_accumulate():
    state = TasteState()
    seen = [Belief().uncertainty]
    for _ in range(3):
        push(state, Reason.WANT_SIT)
        seen.append(state.session[Dimension.FORM].uncertainty)
    assert seen == sorted(seen, reverse=True)
    assert state.effective(Dimension.TRAVEL) is None


def test_shown_candidates_are_not_marked_visited():
    state = TasteState()
    state.record_shown(["gallery"])
    state.apply(fb(Reason.WANT_SIT, candidates=["gallery", "park"]))
    assert state.consumed == {"gallery": "shown", "park": "shown"}


def test_state_round_trips_through_json_for_storage():
    state = in_lunch()
    for session in range(2):
        state.session_id = f"s{session}"
        push(state, Reason.TOO_FAR, times=2)
    state.confirm(state.proposals()[0])
    state.accept(["park"], "park", {Dimension.FORM: 0, Dimension.TRAVEL: 0})
    restored = TasteState.model_validate(state.model_dump(mode="json"))
    assert restored == state
    assert restored.effective(Dimension.TRAVEL).value == 0


def test_rejections_accumulate_and_an_empty_reroll_does_not_wipe_them():
    """A round with no quest sends no candidate ids; that must not forget earlier rejections."""
    state = TasteState()
    state.apply(fb(Reason.TOO_OBVIOUS, candidates=["harbour", "park"]))
    state.apply(fb(Reason.WANT_MOVE, candidates=[]))
    state.apply(fb(Reason.WANT_SIT, candidates=["park", "gallery"]))
    assert state.rejected == ["harbour", "park", "gallery"]


def test_incognito_sessions_learn_but_do_not_remember():
    state = TasteState(context=LUNCH, incognito=True)
    push(state, Reason.TOO_FAR, times=4)
    state.accept(["park"], "park", {Dimension.TRAVEL: 0})
    assert state.session[Dimension.TRAVEL].total == 4
    assert state.memory.episodes == []


def test_no_episode_before_the_context_is_known():
    state = TasteState()
    push(state, Reason.TOO_FAR)
    assert state.memory.episodes == []


def test_accept_is_weak_support_and_contradicts_a_category_dislike():
    state = in_lunch()
    episode = state.accept(["cafe-1"], "cafe", {Dimension.FORM: 1, Dimension.TRAVEL: None})
    weights = {(s.key, s.supports): s.weight for s in episode.signals}
    assert weights[(dimension_key(Dimension.FORM, 1), True)] == 0.5
    assert weights[(category_key("cafe", like=True), True)] == 0.5
    assert weights[(category_key("cafe", like=False), False)] == 0.5
    assert (dimension_key(Dimension.FORM, 0), False) not in weights
    assert not any(s.key.key == Dimension.TRAVEL.value for s in episode.signals)


def test_memory_never_outweighs_one_dimension_hit():
    state = in_lunch()
    for n in range(6):
        state.memory.items.append(MemoryItem(key=category_key("cafe", like=True),
                                             source="agent_proposed", episode_ids=[f"e{n}"]))
    state.memory.items.append(MemoryItem(key=place_key("cafe-1", "favorite"),
                                         source="user_stated"))
    bonus, used = state.adjustment("cafe-1", "cafe")
    assert bonus == MEMORY_CAP
    assert len(used) == 7


def test_stale_items_count_half():
    state = in_lunch()
    item = MemoryItem(key=category_key("cafe", like=True), source="agent_proposed",
                      confirmed_at=datetime.now(SYDNEY) - timedelta(days=1))
    state.memory.items.append(item)
    fresh = state.adjustment("x", "cafe")[0]
    for session in range(STALE_SESSIONS):
        state.session_id = f"quiet{session}"
        push(state, Reason.WANT_SIT)
    assert state.adjustment("x", "cafe")[0] == fresh / 2


def test_unmapped_free_text_is_session_context_only():
    state = in_lunch()
    state.apply(Feedback(quest_id="q", candidate_ids=["gallery"], reason=Reason.OTHER,
                         note="想找个能充电的地方"))
    assert state.session_notes == ["想找个能充电的地方"]
    assert state.session == {} and state.memory.items == []
    assert TasteState(memory=state.memory).session_notes == []


def test_a_note_proposal_waits_for_confirm_and_is_not_offered_twice():
    state = in_lunch()
    proposal = state.propose_note("不喜欢要排队拍照的地方")
    assert proposal in state.proposals() and state.memory.items == []
    assert state.propose_note("不喜欢要排队拍照的地方") is None
    state.decline(proposal)
    assert state.proposals() == [] and state.propose_note("不喜欢要排队拍照的地方") is None
    again = in_lunch().propose_note("喜欢有长椅的地方")
    assert again.source == "agent_proposed"


def test_d3_is_ordinal_with_a_middle_tier_that_is_neither_pole():
    from sidequest.taste import D3_TIERS, aim_value, dimensions_of, fit, obvious_tier, pole_of

    assert [obvious_tier(v) for v in (0.2, D3_TIERS[0], 0.5, D3_TIERS[1], 0.9, None)] == [
        0, 1, 1, 2, 2, None]
    assert dimensions_of(["户外"], 0.0, 0.5)[Dimension.OBVIOUSNESS] == 1
    assert (aim_value(Dimension.OBVIOUSNESS, 1), aim_value(Dimension.OBVIOUSNESS, 0)) == (2, 0)
    assert pole_of(Dimension.OBVIOUSNESS, 1) is None  # a middle-tier accept is no evidence
    assert (fit(Dimension.OBVIOUSNESS, 1, 2), fit(Dimension.OBVIOUSNESS, 0, 2)) == (0, -1)
    assert fit(Dimension.FORM, 0, 1) == -1


def test_accepting_a_place_never_creates_d3_evidence():
    state = in_lunch()
    episode = state.accept(["x"], None, {Dimension.OBVIOUSNESS: 2, Dimension.FORM: 1})
    assert {s.key.key for s in episode.signals} == {"D1"}
