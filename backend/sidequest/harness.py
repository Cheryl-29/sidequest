"""Simulated-user harness (plan v2.2 §8.2): in-session convergence and cross-session memory.

Each persona lives through `persona.sessions` sessions, one free-time pattern per session;
each session allows up to MAX_ROUNDS quests. Between sessions only long-term memory
survives. Groups differ in what they are allowed to learn and keep:

  fixed             planner order, a rejection only excludes what was shown
  no_memory         agent + session feedback; memory wiped every session
  last_feedback     agent that only remembers the latest reroll reason
  flat_profile      v2's design: dimension items only, no context
  typed_no_context  typed items, but every context collapsed into one
  typed_context     plan v2.2 §7.4, the design under test

The ablations are applied by rewriting what the group may store (episode contexts, item
kinds) -- never by changing product code, so `typed_context` is exactly what ships.

`RuleModel` stands in for the LLM so runs are free and reproducible. Its choices are crude
on purpose; results obtained with it say how the memory and ranking machinery behaves,
not how good a real model's judgement is. Real-model runs are opt-in and repeated.
"""

import ast
import re
import zlib
from collections.abc import Callable

from pydantic import BaseModel, Field

from .agent import Intent, propose_quest
from .llm import ModelError
from .memory import Context, Memory, Slot, Span, context_of
from .models import Candidate, Request
from .personas import Persona, covered
from .places import candidate_kind
from .planner import distance, plan, request_origin
from .taste import Feedback, TasteState, dimensions_of

MAX_ROUNDS = 5
GROUPS = ["fixed", "no_memory", "last_feedback", "flat_profile", "typed_no_context",
          "typed_context"]
ONE_CONTEXT = Context(slot=Slot.OTHER, span=Span.MEDIUM)  # what ablated groups see everywhere


class RuleModel:
    """A deterministic stand-in for the LLM. Reads only what the real model would be sent."""

    def __init__(self, seed: int = 0):
        self.seed = seed  # picks the explored pole, so no persona pole is favoured by default
        self.calls = self.prompt_tokens = self.completion_tokens = 0
        self.seen: list[str] = []

    def decide(self, name: str, system: str, user: str, spec: dict) -> dict:
        self.calls += 1
        self.seen.append(name)
        if name == "infer_intent":
            said = user.split("\n")[0]
            form = ("outdoor" if any(w in said for w in ("走走", "户外", "散步")) else
                    "indoor" if any(w in said for w in ("坐", "安静", "室内")) else "either")
            return {"form": form, "time_source": "other",
                    "keywords": [], "inferred": [], "summary": said}
        if name == "choose_probe":
            allowed = [d for d in spec["schema"]["properties"]["dimension"]["enum"] if d != "none"]
            # Exploit what is already known, as the real prompt asks; explore the rest in order.
            match = re.search(r"当前对该用户的了解：(\{.*?\})\n", user)
            known = ast.literal_eval(match.group(1)) if match else {}
            allowed = [d for d in allowed if not (known.get(d) or {}).get("observations")]
            if not allowed:
                return {"dimension": "none", "lean": "indoor", "mode": "exploit", "summary": ""}
            dimension = allowed[0]
            leans = (("indoor", "outdoor") if dimension == "D1"
                     else ("near", "farther"))
            return {"dimension": dimension, "lean": leans[self.seed % 2], "mode": "explore",
                    "summary": "规则模型：按顺序试探"}
        if name == "search_places":
            return {"kinds": [], "summary": "规则模型：不限类型"}
        if name == "interpret_feedback":
            return {"reason": "none", "lasting": "", "summary": ""}
        return {"brief": "一段支线", "hook": "这段时间走得通。", "evidence_ids": [], "summary": ""}


class Round(BaseModel):
    quest: bool
    anchor: str | None = None
    reason: str | None = None  # None = accepted
    error: str | None = None  # a model failure; the round produced nothing
    memory_ids: list[str] = Field(default_factory=list)  # remembered items that shaped it
    violates_other_context: bool = False


class Session(BaseModel):
    index: int
    slot: Slot
    rounds: list[Round] = Field(default_factory=list)
    proposals: int = 0
    proposals_accepted: int = 0

    @property
    def accepted_at(self) -> int | None:
        return next((i + 1 for i, r in enumerate(self.rounds) if r.quest and r.reason is None),
                    None)


class PersonaRun(BaseModel):
    persona: str
    group: str
    sessions: list[Session]
    active_items: int = 0
    wrong_items: int = 0
    truths: int = 0
    truths_covered: int = 0
    model_calls: int = 0
    tokens: int = 0


def seed_for(*parts) -> int:
    return zlib.crc32(":".join(map(str, parts)).encode())


def other_context_violation(persona: Persona, session: int, slot: Slot, anchor: Candidate) -> bool:
    """Served the pole another slot wants, against what this slot wants."""
    axes = {d.value: pole for d, pole in dimensions_of(anchor.tags, 0.0, anchor.obviousness).items()}
    here = persona.prefs(session, slot)
    for other, rules in persona.context_dims.items():
        if other == slot:
            continue
        for dim, want in rules.items():
            if here.get(dim) is not None and axes.get(dim) == want != here[dim]:
                return True
    return False


class Harness:
    def __init__(self, persona: Persona, group: str, model_factory: Callable = RuleModel):
        self.persona, self.group, self.make_model = persona, group, model_factory
        self.memory = Memory()
        self.names: dict[str, str] = {}
        self.calls = 0
        self.tokens = 0

    # --- ablations: what the group may keep ---------------------------------------------

    def restrict(self, state: TasteState) -> None:
        if self.group in ("flat_profile", "typed_no_context"):
            for episode in state.memory.episodes:
                episode.context = ONE_CONTEXT
            for item in state.memory.items:
                item.context = None
        if self.group == "flat_profile":
            state.memory.items = [i for i in state.memory.items if i.key.kind == "dimension"]

    def offer(self, state: TasteState, session: Session, index: int) -> None:
        """At most one proposal per session, as the API does; the persona answers it."""
        if session.proposals or self.group in ("fixed", "no_memory", "last_feedback"):
            return
        self.restrict(state)
        proposals = state.proposals()
        if self.group == "flat_profile":
            proposals = [p for p in proposals if p.key.kind == "dimension"]
        if not proposals:
            return
        proposal = proposals[0]
        session.proposals += 1
        if self.persona.answer(proposal, state.memory.active(), index, self.names):
            session.proposals_accepted += 1
            state.confirm(proposal)
        else:
            state.decline(proposal)
        self.restrict(state)

    # --- one session ----------------------------------------------------------------------

    def fixed_session(self, request: Request, session: Session, index: int, context) -> None:
        excluded: list[str] = []
        for _ in range(MAX_ROUNDS):
            result, _ = plan(request.model_copy(update={"excluded_ids": excluded[-64:]}))
            if not result.itineraries:
                session.rounds.append(Round(quest=False))
                return
            anchor = result.itineraries[0].stops[0].candidate
            km = distance(request_origin(request), anchor.model_dump())
            reason = self.persona.judge(index, context, anchor, km)
            session.rounds.append(Round(quest=True, anchor=anchor.name,
                                        reason=reason.value if reason else None))
            if reason is None:
                return
            excluded += [s.candidate.id for s in result.itineraries[0].stops]

    def agent_session(self, request: Request, session: Session, index: int, context) -> None:
        memory = Memory() if self.group == "no_memory" else self.memory
        state = TasteState(session_id=f"{self.persona.id}:{index}", memory=memory)
        intent: Intent | None = None
        last: Feedback | None = None
        for round_index in range(MAX_ROUNDS):
            if self.group == "last_feedback":
                kept = TasteState(session_id=state.session_id, memory=Memory(),
                                  rejected=state.rejected, consumed=state.consumed)
                if last:
                    kept.apply(last)
                state = kept
            seed = seed_for(self.persona.id, index, round_index)
            model = self.make_model(seed)
            try:
                outcome = propose_quest(request, state, model, said=self.persona.opener,
                                        intent=intent, seed=seed)
            except ModelError as exc:
                # A real model can time out or be rate limited. Count it as a wasted round
                # (the product would degrade to the fixed planner; the report shows both).
                self.calls += model.calls
                self.tokens += model.prompt_tokens + model.completion_tokens
                session.rounds.append(Round(quest=False, error=type(exc).__name__))
                return
            self.calls += model.calls
            self.tokens += model.prompt_tokens + model.completion_tokens
            self.restrict(state)
            if outcome.quest is None:
                session.rounds.append(Round(quest=False))
                return
            intent = outcome.quest.intent
            stops = {s.candidate.id: s.candidate for s in outcome.quest.itinerary.stops}
            anchor = stops[outcome.quest.anchor_id]
            self.names.update({c.id: c.name for c in stops.values()})
            km = distance(request_origin(request), anchor.model_dump())
            reason = self.persona.judge(index, context, anchor, km)
            session.rounds.append(Round(
                quest=True, anchor=anchor.name, reason=reason.value if reason else None,
                memory_ids=outcome.quest.memory_ids,
                # Only a memory-driven first round can misuse another context's preference.
                violates_other_context=round_index == 0 and bool(outcome.quest.memory_ids)
                and other_context_violation(self.persona, index, context.slot, anchor),
            ))
            if reason is None:
                origin = request_origin(request)
                axes = dimensions_of(anchor.tags, distance(origin, anchor.model_dump()),
                                     anchor.obviousness)
                state.accept(list(stops), candidate_kind(anchor), axes)
                self.restrict(state)
                self.offer(state, session, index)
                return
            last = Feedback(quest_id=outcome.quest.itinerary.id, candidate_ids=list(stops)[:3],
                            anchor_id=anchor.id, kind=candidate_kind(anchor), reason=reason)
            result = state.apply(last)
            if result.stated and self.group != "last_feedback":
                result.stated.text = f"以后别再推 {anchor.name}"
                state.confirm(result.stated)
            self.restrict(state)
            self.offer(state, session, index)

    def run(self) -> PersonaRun:
        sessions = []
        for index in range(self.persona.sessions):
            request = self.persona.pattern(index).request(week=index)
            context = context_of(request.departure, request.deadline)
            session = Session(index=index, slot=context.slot)
            if self.group == "fixed":
                self.fixed_session(request, session, index, context)
            else:
                self.agent_session(request, session, index, context)
            sessions.append(session)
        last = self.persona.sessions - 1
        items = self.memory.active() if self.group not in ("fixed", "no_memory",
                                                           "last_feedback") else []
        judged = [i for i in items if self.persona.holds(i, last, self.names) is not None]
        truths = self.persona.truths(last)
        return PersonaRun(
            persona=self.persona.id, group=self.group, sessions=sessions,
            active_items=len(judged),
            wrong_items=sum(1 for i in judged if not self.persona.holds(i, last, self.names)),
            truths=len(truths), truths_covered=sum(covered(t, items) for t in truths),
            model_calls=self.calls, tokens=self.tokens,
        )


def ratio(numerator: int, denominator: int) -> dict:
    return {"n": numerator, "d": denominator,
            "rate": round(numerator / denominator, 3) if denominator else None}


def summarise(runs: list[PersonaRun], personas: dict[str, Persona]) -> dict:
    """Raw numerators and denominators per group (plan v2 §8.5: no pre-filled numbers)."""
    out = {}
    for group in GROUPS:
        rows = [r for r in runs if r.group == group]
        if not rows:
            continue
        sessions = [s for r in rows for s in r.sessions]
        by_index: dict[int, list[Session]] = {}
        for s in sessions:
            by_index.setdefault(s.index, []).append(s)
        contextual = [s for r in rows if personas[r.persona].context_dims for s in r.sessions]
        agree = [r for r in rows if personas[r.persona].accept_all_proposals]
        accepted = [s.accepted_at for s in sessions if s.accepted_at]
        out[group] = {
            "accept@1": ratio(sum(1 for s in sessions if s.accepted_at == 1), len(sessions)),
            "accept@3": ratio(sum(1 for s in sessions if s.accepted_at and s.accepted_at <= 3),
                              len(sessions)),
            "accept@5": ratio(len(accepted), len(sessions)),
            "mean_rounds_when_accepted": round(sum(accepted) / len(accepted), 2) if accepted
            else None,
            "first_round_accept_by_session": {
                i: ratio(sum(1 for s in group_sessions if s.accepted_at == 1), len(group_sessions))
                for i, group_sessions in sorted(by_index.items())
            },
            "first_round_accept_session_3_plus": ratio(
                sum(1 for s in sessions if s.index >= 2 and s.accepted_at == 1),
                sum(1 for s in sessions if s.index >= 2)),
            "other_context_misuse": ratio(
                sum(1 for s in contextual if s.rounds and s.rounds[0].violates_other_context),
                sum(1 for s in contextual if s.rounds and s.rounds[0].memory_ids)),
            "first_rounds_shaped_by_memory": ratio(
                sum(1 for s in sessions if s.rounds and s.rounds[0].memory_ids),
                sum(1 for s in sessions if s.rounds and s.rounds[0].quest)),
            "proposal_precision": ratio(sum(s.proposals_accepted for s in sessions),
                                        sum(s.proposals for s in sessions)),
            "false_memory_all_agree": ratio(sum(r.wrong_items for r in agree),
                                            sum(r.active_items for r in agree)),
            "memory_recall": ratio(sum(r.truths_covered for r in rows),
                                   sum(r.truths for r in rows)),
            "empty_rounds": sum(1 for s in sessions for x in s.rounds if not x.quest),
            "model_errors": sum(1 for s in sessions for x in s.rounds if x.error),
            "model_calls": sum(r.model_calls for r in rows),
            "tokens": sum(r.tokens for r in rows),
        }
    return out
