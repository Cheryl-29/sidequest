"""M5′: the simulator must be deterministic, hidden from the agent, and judge the truth."""

import json
from datetime import datetime, time, timezone
from pathlib import Path

import pytest
from sidequest.harness import GROUPS, ONE_CONTEXT, Harness, RuleModel, summarise
from sidequest.memory import Context, MemoryItem, Proposal, Slot, Span
from sidequest.models import Request
from sidequest.personas import Pattern, Persona, covered
from sidequest.places import build_index
from sidequest.reference import compare, enumerate_feasible
from sidequest.taste import Dimension, Feedback, Reason, TasteState, category_key, dimension_key

ROOT = Path(__file__).resolve().parents[2]
PROBES = ROOT / "data" / "probes" / "20260914T041841Z"
LUNCH = Context(slot=Slot.LUNCH, span=Span.MEDIUM)
WEEKEND = Context(slot=Slot.WEEKEND_DAY, span=Span.MEDIUM)
CBD = Pattern(weekday=0, start=time(12), minutes=90, lat=-33.8731, lon=151.2065)
GLEBE = Pattern(weekday=5, start=time(10), minutes=180, lat=-33.8792, lon=151.1850)


@pytest.fixture
def pool(tmp_path, monkeypatch):
    elements = []
    for name in ("harbour", "glebe", "surry_hills"):
        elements += json.loads((PROBES / f"{name}.response").read_text())["elements"]
    path = tmp_path / "probe.sqlite"
    build_index(elements, path, {"source": "test", "source_timestamp":
                                 datetime(2026, 9, 14, tzinfo=timezone.utc).isoformat()},
                (-34.2, 150.8, -33.5, 151.5))
    monkeypatch.setenv("SIDEQUEST_PLACES_DB", str(path))
    return path


def persona(**kw):
    return Persona.model_validate({"id": "t", "layer": "test", "patterns": [CBD, GLEBE], **kw})


def place(name="X", tags=("室内",), obviousness=None, kind="gallery"):
    from sidequest.fixtures import candidates

    base = candidates(Request.model_validate({"departure": "2026-09-14T10:00:00+10:00",
                                              "deadline": "2026-09-14T14:00:00+10:00"}))[0]
    return base.model_copy(update={"name": name, "tags": [*tags, f"kind:{kind}"],
                                   "obviousness": obviousness})


def test_the_persona_set_matches_the_plan():
    data = json.loads((ROOT / "evals" / "personas.json").read_text())
    rows = data["personas"]
    assert len(rows) == 12 and sum(r.get("holdout", False) for r in rows) == 5
    assert any(r.get("holdout") and r["layer"] == "情境条件" for r in rows)
    patterns = {k: Pattern.model_validate(v) for k, v in data["patterns"].items()}
    loaded = [Persona.model_validate({**r, "patterns": [patterns[n] for n in r["patterns"]]})
              for r in rows]
    assert {p.id for p in loaded if p.blocked_by({"D1", "D3"})} == {
        "p02-d2-near", "p03-d2-far", "p04-d1-outdoor-x-d2-near", "p05-d1-x-d2"}
    assert not {p.id for p in loaded if p.blocked_by({"D1", "D2"})}
    assert all(p.synthetic for p in loaded)


def test_judge_has_a_fixed_order_of_concerns_and_ignores_retired_d3():
    p = persona(never=["Hated"], dislikes=["museum"], dims={"D1": 0})
    assert p.judge(0, LUNCH, place("Hated", kind="museum")) is Reason.NEVER_HERE
    assert p.judge(0, LUNCH, place(kind="museum")) is Reason.NOT_THIS_KIND
    assert p.judge(0, LUNCH, place(tags=("室内",), obviousness=0.1)) is Reason.WANT_MOVE
    assert p.judge(0, LUNCH, place(tags=("户外",), obviousness=0.1)) is None
    assert p.judge(0, LUNCH, place(tags=("户外",), obviousness=0.9)) is None
    assert p.judge(0, LUNCH, place(tags=("户外",), obviousness=None)) is None


def test_context_rules_and_drift_decide_what_is_wanted():
    p = persona(dims={"D1": 1}, context_dims={"weekend_day": {"D1": 0}},
                drift={"at_session": 2, "dims": {"D2": 0}})
    assert p.prefs(0, Slot.LUNCH) == {"D1": 1}
    assert p.prefs(0, Slot.WEEKEND_DAY) == {"D1": 0}
    assert p.prefs(2, Slot.LUNCH) == {"D1": 1, "D2": 0}


def test_ground_truth_respects_context():
    p = persona(context_dims={"lunch": {"D1": 1}, "weekend_day": {"D1": 0}})
    item = lambda ctx: MemoryItem(key=dimension_key(Dimension.FORM, 1), context=ctx,  # noqa: E731
                                  source="agent_proposed")
    assert p.holds(item(LUNCH), 0, {}) is True
    assert p.holds(item(WEEKEND), 0, {}) is False
    assert p.holds(item(None), 0, {}) is False  # a global item over-generalises
    assert covered(("dimension", "D1", "1", Slot.LUNCH), [item(LUNCH)])
    assert not covered(("dimension", "D1", "0", Slot.WEEKEND_DAY), [item(LUNCH)])


def test_proposals_are_answered_from_the_hidden_truth_unless_the_persona_agrees_to_all():
    wrong = Proposal(action="add", key=category_key("park", like=False), context=None)
    right = Proposal(action="add", key=category_key("museum", like=False), context=None)
    p = persona(dislikes=["museum"])
    assert p.answer(right, [], 0, {}) and not p.answer(wrong, [], 0, {})
    assert persona(dislikes=["museum"], accept_all_proposals=True).answer(wrong, [], 0, {})


def test_ablations_only_change_what_a_group_may_keep():
    state = TasteState(context=LUNCH)
    state.memory.items += [
        MemoryItem(key=dimension_key(Dimension.FORM, 1), context=LUNCH, source="agent_proposed"),
        MemoryItem(key=category_key("museum", like=False), context=LUNCH, source="agent_proposed"),
    ]
    state.apply(Feedback(quest_id="q", reason=Reason.WANT_SIT))
    Harness(persona(), "flat_profile").restrict(state)
    assert [i.key.kind for i in state.memory.items] == ["dimension"]
    assert state.memory.items[0].context is None
    assert {e.context for e in state.memory.episodes} == {ONE_CONTEXT}


def test_runs_are_deterministic_and_the_agent_never_sees_the_persona(pool):
    seen = []

    class Recording(RuleModel):
        def decide(self, name, system, user, spec):
            seen.append(system + user)
            return super().decide(name, system, user, spec)

    p = persona(id="secret-persona-7", dims={"D1": 1}, dislikes=["museum"], sessions=3)
    first = Harness(p, "typed_context", Recording).run()
    again = Harness(p, "typed_context", Recording).run()
    assert first == again
    assert seen and not any("secret-persona-7" in s or "museum" in s.split("可选类型")[0]
                            for s in seen)


def test_every_group_runs_and_reports_raw_counts(pool):
    p = persona(dims={"D1": 0}, sessions=2)
    runs = [Harness(p, g).run() for g in GROUPS]
    summary = summarise(runs, {p.id: p})
    assert set(summary) == set(GROUPS)
    for row in summary.values():
        assert {"n", "d", "rate"} <= set(row["accept@1"])
        assert row["accept@1"]["d"] == 2
    assert summary["fixed"]["model_calls"] == 0
    assert summary["no_memory"]["memory_recall"]["n"] == 0


def test_the_reference_contains_everything_the_planner_returns():
    request = Request.model_validate({"departure": "2026-09-14T10:00:00+10:00",
                                      "deadline": "2026-09-14T14:00:00+10:00"})
    row = compare(request)
    assert row["exists"] and row["planner_found"] and row["unexplained"] == []
    assert row["combinations"] >= row["planner_combinations"]


def test_the_reference_refuses_live_mode():
    request = Request.model_validate({"departure": "2026-09-14T10:00:00+10:00",
                                      "deadline": "2026-09-14T14:00:00+10:00", "mode": "live"})
    with pytest.raises(ValueError):
        enumerate_feasible(request)


def test_a_model_failure_is_a_wasted_round_not_a_crash(pool):
    from sidequest.llm import ModelRateLimitError

    class Limited(RuleModel):
        def decide(self, *a):
            raise ModelRateLimitError("模型请求频率或额度受限")

    run = Harness(persona(dims={"D1": 0}, sessions=2), "typed_context", Limited).run()
    assert all(s.rounds == [s.rounds[0]] and s.rounds[0].error == "ModelRateLimitError"
               for s in run.sessions)
    assert summarise([run], {"t": persona()})["typed_context"]["model_errors"] == 2


def test_a_kind_filter_cannot_override_what_the_user_said_this_session(pool):
    """Real-model run: errand kinds only (all indoor) after five "想动一动" rerolls."""
    from sidequest.agent import propose_quest
    from sidequest.places import candidate_kind

    class ErrandsOnly(RuleModel):
        def decide(self, name, system, user, spec):
            self.last = user if name == "search_places" else getattr(self, "last", "")
            if name == "search_places":
                return {"kinds": ["gallery", "museum"], "summary": "只找室内"}
            return super().decide(name, system, user, spec)

    state = TasteState()
    state.apply(Feedback(quest_id="q", reason=Reason.WANT_MOVE))
    model = ErrandsOnly()
    result = propose_quest(CBD.request(week=0), state, model, said="", seed=1)
    assert candidate_kind(next(s.candidate for s in result.quest.itinerary.stops
                               if s.candidate.id == result.quest.anchor_id)) in {
        "park", "garden", "viewpoint", "beach", "nature_reserve"}
    assert any(t.action == "restore_feedback" for t in result.trace)
    assert "更想要户外" in model.last  # the prompt now carries what the user said

    remembered = TasteState()  # a new session: only a confirmed memory for this context
    remembered.memory.items.append(MemoryItem(key=dimension_key(Dimension.FORM, 0),
                                              source="agent_proposed", episode_ids=["e"]))
    again = propose_quest(CBD.request(week=1), remembered, ErrandsOnly(), said="", seed=1)
    assert any(t.action == "restore_feedback" for t in again.trace)
