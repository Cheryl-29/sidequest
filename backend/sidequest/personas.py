"""Synthetic users for the convergence and cross-session experiments (plan v2.2 §8.2–8.3).

A persona is a hidden, fully structured taste plus deterministic reactions. The agent never
sees a persona: the harness only hands it `Feedback`, accepts, and yes/no answers to
proposals -- exactly what a real user could give through the API. Everything here is
labelled synthetic and must never be reported as user data.

Deterministic on purpose: the same quest always gets the same reaction, so a difference in
convergence between two groups is the groups' doing, not the simulator's mood.
"""

from datetime import date, datetime, time, timedelta

from pydantic import BaseModel, Field

from .memory import Context, MemoryItem, Proposal, Slot
from .models import SYDNEY, Candidate, Request
from .places import candidate_kind
from .taste import Dimension, Reason, aim_value, dimensions_of

FIRST_MONDAY = date(2026, 9, 21)


class Pattern(BaseModel):
    """One kind of free time in this person's week."""

    weekday: int = Field(ge=0, le=6)
    start: time
    minutes: int = Field(ge=30, le=480)
    lat: float
    lon: float

    def request(self, week: int) -> Request:
        day = FIRST_MONDAY + timedelta(weeks=week, days=self.weekday)
        departure = datetime.combine(day, self.start, tzinfo=SYDNEY)
        return Request.model_validate({
            "departure": departure, "deadline": departure + timedelta(minutes=self.minutes),
            "origin_id": "current", "origin_lat": self.lat, "origin_lon": self.lon,
            "catalog": "osm", "venue_facts": "advisory",
        })


class Drift(BaseModel):
    at_session: int
    dims: dict[str, int] = Field(default_factory=dict)


class Persona(BaseModel):
    id: str
    layer: str
    holdout: bool = False
    synthetic: bool = True
    opener: str = ""
    requires: list[str] = Field(default_factory=list)
    dims: dict[str, int] = Field(default_factory=dict)
    context_dims: dict[Slot, dict[str, int]] = Field(default_factory=dict)
    likes: list[str] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    never: list[str] = Field(default_factory=list)  # place names, matched exactly
    drift: Drift | None = None
    accept_all_proposals: bool = False
    sessions: int = 6
    patterns: list[Pattern]

    def blocked_by(self, active: set[str]) -> list[str]:
        """Dimensions this persona needs that the pool's gate does not pass (plan v2 §8.3)."""
        return sorted(set(self.requires) - active)

    def pattern(self, session: int) -> Pattern:
        return self.patterns[session % len(self.patterns)]

    def prefs(self, session: int, slot: Slot) -> dict[str, int]:
        """What this person actually wants in this session and time slot."""
        out = dict(self.dims)
        if self.drift and session >= self.drift.at_session:
            out.update(self.drift.dims)
        out.update(self.context_dims.get(slot, {}))
        return out

    def judge(self, session: int, context: Context, anchor: Candidate,
              km: float = 0.0) -> Reason | None:
        """The reroll reason this person gives, or None to accept. Fixed order of concerns.
        `km` is the anchor's distance from where this session starts (D2)."""
        if anchor.name in self.never:
            return Reason.NEVER_HERE
        if candidate_kind(anchor) in self.dislikes:
            return Reason.NOT_THIS_KIND
        axes = dimensions_of(anchor.tags, km, anchor.obviousness)
        prefs = self.prefs(session, context.slot)
        if (want := prefs.get("D1")) is not None and axes[Dimension.FORM] not in (None, want):
            return Reason.WANT_SIT if want == 1 else Reason.WANT_MOVE
        if (want := prefs.get("D2")) is not None and axes[Dimension.TRAVEL] != want:
            return Reason.WANT_FARTHER if want == 1 else Reason.TOO_FAR
        return None

    # --- Ground truth for memory metrics ------------------------------------------------

    def holds(self, item: MemoryItem, session: int, names: dict[str, str]) -> bool | None:
        """Whether a memory item is true of this person now. None: not judgeable (notes)."""
        key = item.key
        if key.kind == "dimension":
            slots = [item.context.slot] if item.context else list(Slot)
            want = aim_value(Dimension(key.key), int(key.value))
            return all(self.prefs(session, s).get(key.key) == want for s in slots)
        if key.kind == "category":
            return key.key in (self.dislikes if key.value == "dislike" else self.likes)
        if key.kind == "place":
            return key.value == "never" and names.get(key.key) in self.never
        return None

    def answer(self, proposal: Proposal, items: list[MemoryItem], session: int,
               names: dict[str, str]) -> bool:
        if self.accept_all_proposals:
            return True
        if proposal.action == "retire":
            target = next((i for i in items if i.id == proposal.item_id), None)
            return target is not None and self.holds(target, session, names) is False
        contexts = proposal.contexts if proposal.action == "narrow" else [proposal.context]
        candidates = [MemoryItem(key=proposal.key, context=c, source="agent_proposed")
                      for c in contexts]
        return all(self.holds(i, session, names) is not False for i in candidates)

    def truths(self, session: int) -> list[tuple[str, str, str, Slot | None]]:
        """The structured facts a perfect memory would hold: (kind, key, value, slot)."""
        out = []
        for dim, value in (self.dims | (self.drift.dims if self.drift
                                        and session >= self.drift.at_session else {})).items():
            overridden = any(dim in rules for rules in self.context_dims.values())
            if not overridden:
                out.append(("dimension", dim, str(value), None))
        for slot, rules in self.context_dims.items():
            out += [("dimension", dim, str(value), slot) for dim, value in rules.items()]
        out += [("category", k, "dislike", None) for k in self.dislikes]
        out += [("category", k, "like", None) for k in self.likes]
        return out


def covered(truth: tuple[str, str, str, Slot | None], items: list[MemoryItem]) -> bool:
    kind, key, value, slot = truth
    for item in items:
        if (item.key.kind, item.key.key, item.key.value) != (kind, key, value):
            continue
        if slot is None or item.context is None or item.context.slot == slot:
            return True
    return False
