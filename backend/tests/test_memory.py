"""Plan v2.2 §7.4: every consolidation, conflict and deletion rule has a test here."""

from datetime import datetime, timedelta

import pytest
from sidequest.memory import (
    PROPOSE_SUPPORT,
    REVISE_CONTRA,
    Memory,
    Slot,
    Span,
    context_of,
    load,
    save,
)
from sidequest.models import SYDNEY
from sidequest.storage import Store
from sidequest.taste import Dimension, Feedback, Reason, TasteState, category_key, dimension_key


def at(day, hour, minutes=60):
    start = datetime(2026, 9, day, hour, 0, tzinfo=SYDNEY)
    return context_of(start, start + timedelta(minutes=minutes))


LUNCH = at(14, 12)  # Monday
WEEKEND = at(19, 13)  # Saturday
AFTER_WORK = at(15, 18)
NEAR = dimension_key(Dimension.TRAVEL, 0)
FAR = dimension_key(Dimension.TRAVEL, 1)


class User:
    """Drives one TasteState across sessions the way the API will: memory carries over,
    everything else starts fresh."""

    def __init__(self):
        self.memory = Memory()
        self.count = 0

    def session(self, context, *reasons, kind=None):
        self.count += 1
        state = TasteState(session_id=f"s{self.count}", memory=self.memory, context=context)
        for reason in reasons:
            state.apply(Feedback(quest_id="q", candidate_ids=["x"], reason=reason, kind=kind))
        return state

    def state(self, context=None):
        return TasteState(memory=self.memory, context=context)


def keys(proposals):
    return [(p.action, p.key, p.context) for p in proposals]


@pytest.mark.parametrize(
    "day,hour,minutes,slot,span",
    [
        (14, 12, 60, Slot.LUNCH, Span.SHORT),  # Monday noon
        (15, 18, 120, Slot.AFTER_WORK, Span.MEDIUM),
        (19, 10, 300, Slot.WEEKEND_DAY, Span.LONG),  # Saturday
        (19, 19, 60, Slot.OTHER, Span.SHORT),  # Saturday evening
        (16, 9, 89, Slot.OTHER, Span.SHORT),  # weekday morning
    ],
)
def test_context_comes_from_the_request_clock(day, hour, minutes, slot, span):
    context = at(day, hour, minutes)
    assert (context.slot, context.span) == (slot, span)


def test_three_times_in_one_session_is_not_a_habit():
    user = User()
    user.session(LUNCH, *[Reason.TOO_FAR] * 5)
    assert user.state().proposals() == []


def test_support_across_two_sessions_proposes_an_item_for_that_context_only():
    user = User()
    user.session(LUNCH, Reason.TOO_FAR, Reason.TOO_FAR)
    user.session(LUNCH, Reason.TOO_FAR)
    (proposal,) = user.state().proposals()
    assert (proposal.action, proposal.key, proposal.context) == ("add", NEAR, LUNCH)
    assert len(proposal.episode_ids) == PROPOSE_SUPPORT
    assert "工作日午休" in proposal.text


def test_a_contradiction_in_the_same_context_blocks_the_proposal():
    user = User()
    user.session(LUNCH, Reason.TOO_FAR, Reason.TOO_FAR)
    user.session(LUNCH, Reason.TOO_FAR, Reason.WANT_FARTHER)
    assert user.state().proposals() == []


def test_a_contradiction_in_another_context_is_not_a_contradiction():
    user = User()
    user.session(LUNCH, Reason.TOO_FAR, Reason.TOO_FAR)
    user.session(LUNCH, Reason.TOO_FAR)
    user.session(WEEKEND, Reason.WANT_FARTHER)
    assert ("add", NEAR, LUNCH) in keys(user.state().proposals())


def test_opposite_poles_in_two_contexts_become_two_items_and_recall_picks_per_context():
    user = User()
    for context, reason in ((LUNCH, Reason.TOO_FAR), (WEEKEND, Reason.WANT_FARTHER)):
        user.session(context, reason, reason)
        user.session(context, reason)
    proposals = user.state().proposals()
    assert set(keys(proposals)) == {("add", NEAR, LUNCH), ("add", FAR, WEEKEND)}
    state = user.state()
    for proposal in proposals:
        state.confirm(proposal)
    assert user.state(LUNCH).effective(Dimension.TRAVEL).value == 0
    assert user.state(WEEKEND).effective(Dimension.TRAVEL).value == 1
    assert user.state(AFTER_WORK).effective(Dimension.TRAVEL) is None


def test_clean_support_in_two_contexts_generalises_to_one_global_item():
    user = User()
    for context in (LUNCH, AFTER_WORK):
        user.session(context, Reason.WANT_SIT, Reason.WANT_SIT)
        user.session(context, Reason.WANT_SIT)
    (proposal,) = user.state().proposals()
    assert (proposal.key, proposal.context) == (dimension_key(Dimension.FORM, 1), None)
    user.state().confirm(proposal)
    assert user.state(WEEKEND).effective(Dimension.FORM).value == 1


def test_consistent_support_spread_over_contexts_generalises_before_any_context_is_ready():
    user = User()
    for context in (LUNCH, WEEKEND, AFTER_WORK):
        user.session(context, Reason.WANT_MOVE)  # 1.0 each: no context reaches the bar alone
    (proposal,) = user.state().proposals()
    assert (proposal.key, proposal.context) == (dimension_key(Dimension.FORM, 0), None)


def test_spread_support_needs_two_sessions_and_two_contexts():
    user = User()
    user.session(LUNCH, *[Reason.WANT_MOVE] * 4)  # one session, one context
    assert user.state().proposals() == []
    user = User()
    user.session(LUNCH, Reason.WANT_MOVE)
    user.session(WEEKEND, Reason.WANT_MOVE)  # two contexts, but 2.0 < PROPOSE_SUPPORT
    assert user.state().proposals() == []


def test_accepts_spread_over_contexts_never_generalise_on_their_own():
    user = User()
    axes = {Dimension.FORM: None, Dimension.TRAVEL: 0, Dimension.OBVIOUSNESS: None}
    for context in (LUNCH, WEEKEND, AFTER_WORK) * 2:  # 6 x 0.5 = PROPOSE_SUPPORT
        user.session(context).accept(["x"], None, axes)
    assert user.state().proposals() == []


def test_a_contradiction_in_any_context_keeps_spread_support_from_going_global():
    user = User()
    user.session(LUNCH, Reason.WANT_SIT, Reason.WANT_SIT)
    user.session(LUNCH, Reason.WANT_SIT)
    user.session(AFTER_WORK, Reason.WANT_SIT)
    user.session(WEEKEND, Reason.WANT_MOVE)  # sit is contradicted on weekends
    assert ("add", dimension_key(Dimension.FORM, 1), LUNCH) in keys(user.state().proposals())
    assert ("add", dimension_key(Dimension.FORM, 1), None) not in keys(user.state().proposals())


def test_a_second_context_upgrades_a_confirmed_context_item_to_global():
    user = User()
    user.session(LUNCH, Reason.WANT_SIT, Reason.WANT_SIT)
    user.session(LUNCH, Reason.WANT_SIT)
    user.state().confirm(user.state().proposals()[0])
    user.session(AFTER_WORK, Reason.WANT_SIT, Reason.WANT_SIT)
    user.session(AFTER_WORK, Reason.WANT_SIT)
    (proposal,) = user.state().proposals()
    assert proposal.context is None and proposal.replaces
    user.state().confirm(proposal)
    (item,) = user.memory.active()
    assert item.context is None


def test_a_context_item_beats_a_global_item_on_the_same_key():
    user = User()
    for context in (LUNCH, AFTER_WORK):
        user.session(context, Reason.WANT_FARTHER, Reason.WANT_FARTHER)
        user.session(context, Reason.WANT_FARTHER)
    user.state().confirm(user.state().proposals()[0])  # global: willing to go far
    for _ in range(2):
        user.session(WEEKEND, Reason.TOO_FAR, Reason.TOO_FAR)
    (proposal,) = [p for p in user.state().proposals() if p.action == "add"]
    user.state().confirm(proposal)
    assert user.state(WEEKEND).effective(Dimension.TRAVEL).value == 0
    assert user.state(LUNCH).effective(Dimension.TRAVEL).value == 1


def confirmed(user, context, reason):
    user.session(context, reason, reason)
    user.session(context, reason)
    user.state().confirm(user.state().proposals()[0])
    return user.memory.active()[0]


def test_one_exception_does_not_touch_a_confirmed_item():
    user = User()
    confirmed(user, LUNCH, Reason.TOO_FAR)
    user.session(LUNCH, Reason.WANT_FARTHER)
    assert user.state().proposals() == []
    assert user.state(LUNCH).effective(Dimension.TRAVEL).value == 0


def test_repeated_contradiction_proposes_retirement_never_applies_it():
    user = User()
    item = confirmed(user, LUNCH, Reason.TOO_FAR)
    for _ in range(int(REVISE_CONTRA)):
        user.session(LUNCH, Reason.WANT_FARTHER)
    retire = [p for p in user.state().proposals() if p.action == "retire"]
    assert [p.item_id for p in retire] == [item.id]
    assert item.status == "active"
    user.state().confirm(retire[0])
    assert item.status == "retired"
    assert user.state(LUNCH).effective(Dimension.TRAVEL) is None


def test_a_global_item_contradicted_in_one_context_is_narrowed_to_the_others():
    user = User()
    for context in (LUNCH, AFTER_WORK):
        user.session(context, Reason.WANT_SIT, Reason.WANT_SIT)
        user.session(context, Reason.WANT_SIT)
    user.state().confirm(user.state().proposals()[0])
    for _ in range(2):
        user.session(AFTER_WORK, Reason.WANT_MOVE)
    (narrow,) = [p for p in user.state().proposals() if p.action == "narrow"]
    assert narrow.contexts == [LUNCH]
    user.state().confirm(narrow)
    assert user.state(LUNCH).effective(Dimension.FORM).value == 1
    assert user.state(AFTER_WORK).effective(Dimension.FORM) is None


def test_a_declined_proposal_waits_until_its_support_doubles():
    user = User()
    user.session(LUNCH, Reason.TOO_FAR, Reason.TOO_FAR)
    user.session(LUNCH, Reason.TOO_FAR)
    user.state().decline(user.state().proposals()[0])
    user.session(LUNCH, Reason.TOO_FAR, Reason.TOO_FAR)
    assert user.state().proposals() == []
    user.session(LUNCH, Reason.TOO_FAR)
    assert keys(user.state().proposals()) == [("add", NEAR, LUNCH)]


def test_deleting_an_item_takes_effect_next_round_and_is_not_reproposed():
    user = User()
    item = confirmed(user, LUNCH, Reason.TOO_FAR)
    assert user.memory.forget(item.id)
    assert user.state(LUNCH).effective(Dimension.TRAVEL) is None
    assert user.state().proposals() == []


def test_deleting_an_episode_unconfirms_an_item_it_held_up():
    user = User()
    item = confirmed(user, LUNCH, Reason.TOO_FAR)
    assert user.memory.forget_episode(item.episode_ids[0])
    assert user.memory.active() == []
    assert user.state().proposals() == []  # the remaining support is below the threshold


def test_deleting_an_episode_never_removes_what_the_user_stated():
    user = User()
    state = user.session(LUNCH)
    outcome = state.apply(Feedback(quest_id="q", candidate_ids=["park"], reason=Reason.NEVER_HERE))
    state.confirm(outcome.stated)
    assert user.memory.forget_episode(user.memory.episodes[0].id)
    assert user.state(WEEKEND).banned() == {"park"}


def test_category_dislike_consolidates_like_a_dimension():
    user = User()
    user.session(LUNCH, Reason.NOT_THIS_KIND, Reason.NOT_THIS_KIND, kind="museum")
    user.session(LUNCH, Reason.NOT_THIS_KIND, kind="museum")
    (proposal,) = user.state().proposals()
    assert proposal.key == category_key("museum", like=False)
    assert "博物馆" in proposal.text
    user.state().confirm(proposal)
    assert user.state(LUNCH).adjustment("m", "museum")[0] < 0
    assert user.state(WEEKEND).adjustment("m", "museum")[0] == 0


def test_memory_is_owner_scoped_in_storage(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'memory.db'}")
    user = User()
    confirmed(user, LUNCH, Reason.TOO_FAR)
    save(store, "alice", user.memory)
    save(store, "alice", user.memory)  # second save updates in place
    assert load(store, "alice") == user.memory
    assert load(store, "bob") == Memory()
    assert store.get("bob", "memory:alice") is None
