"""What the product learns from a reroll -- and what it refuses to learn.

Session beliefs die with the session; long-term memory (`memory.py`) holds typed items
written ONLY on explicit user confirmation; the consumed set is dedup hygiene. The split
exists because the cheapest way to manufacture a wrong memory is to treat every
rejection as a lasting preference.

This file owns the evidence rules (plan v2.2 §7.4): which reroll or accept supports or
contradicts which memory item. `memory.py` only counts what it is given.

Rules here are load-bearing and easy to erode:

* "去过了" expresses no taste at all. The user may well have gone because they liked it.
  It feeds the consumed set and nothing else.
* "太远了" stays qualitative. It never becomes a max_walk_minutes, because no number
  the user did not say can be recovered from the word (plan v2 §7.3).
* A reroll without a taste reason is not evidence for any item.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .memory import Context, Episode, Key, Memory, MemoryItem, Proposal, Signal
from .models import SYDNEY
from .places import KIND_BY_CODE

FAR_KM = 1.5  # D2 split: mean distance from the fixed origins
# D3 is retained as a research-only place annotation: 0 冷门 | 1 有点名气 | 2 大众答案.
# Cut points on the 0-1 obviousness score. A real open-world pool has only a few percent of
# guidebook answers, so a binary split could never separate; the middle tier can.
D3_TIERS = (0.35, 0.65)
ACCEPT_WEIGHT = 0.5  # an accept is weak evidence: the user may just have wanted to go out

# Score adjustments. Memory together never outweighs one dimension hit (3.0, agent.score):
# a remembered category must not overrule a dimension the user is expressing right now.
MEMORY_CAP = 3.0
CATEGORY_WEIGHT = 1.5
FAVORITE_WEIGHT = 1.0
SESSION_KIND_PENALTY = 3.5  # "not this kind" said this session: > one axis hit


class Dimension(StrEnum):
    """The frozen taste dimensions (plan v2 §4.3). Poles match scripts/check_dimensions.py."""

    FORM = "D1"  # 0 = 户外走动, 1 = 室内静态
    TRAVEL = "D2"  # 0 = 少折腾, 1 = 愿意走远
    OBVIOUSNESS = "D3"  # research-only; never a global user preference


LEARNED_DIMENSIONS = (Dimension.FORM, Dimension.TRAVEL)


class Reason(StrEnum):
    WANT_SIT = "want_sit"
    WANT_MOVE = "want_move"
    TOO_FAR = "too_far"
    WANT_FARTHER = "want_farther"
    TOO_OBVIOUS = "too_obvious"
    TOO_OBSCURE = "too_obscure"
    NOT_THIS_KIND = "not_this_kind"
    NEVER_HERE = "never_here"
    NO_SPEND = "no_spend"
    BAD_TIME = "bad_time"
    BEEN_THERE = "been_there"
    OTHER = "other"  # free text the model could not map onto any reason above


# Reason -> (dimension, pole). A reason absent from this table never touches a belief.
TASTE_REASONS: dict[Reason, tuple[Dimension, int]] = {
    Reason.WANT_SIT: (Dimension.FORM, 1),
    Reason.WANT_MOVE: (Dimension.FORM, 0),
    Reason.TOO_FAR: (Dimension.TRAVEL, 0),
    Reason.WANT_FARTHER: (Dimension.TRAVEL, 1),
}
LOCAL_REASONS = frozenset({Reason.TOO_OBVIOUS, Reason.TOO_OBSCURE})
KIND_REASONS = frozenset({Reason.NOT_THIS_KIND})  # category taste, not a dimension
STATED_REASONS = frozenset({Reason.NEVER_HERE})  # the user's own long-term statement
CONSTRAINT_REASONS = frozenset({Reason.NO_SPEND, Reason.BAD_TIME})
DEDUP_REASONS = frozenset({Reason.BEEN_THERE})
SESSION_REASONS = frozenset({Reason.OTHER})  # soft context for this session, evidence for nothing

SCOPES = (frozenset(TASTE_REASONS), LOCAL_REASONS, KIND_REASONS, STATED_REASONS, CONSTRAINT_REASONS,
          DEDUP_REASONS, SESSION_REASONS)
# A new Reason must be classified into exactly one scope before it can ship.
assert frozenset().union(*SCOPES) == set(Reason)
assert sum(len(s) for s in SCOPES) == len(Reason)

LABELS = {
    (Dimension.FORM, 1): "更想要室内、可以坐下来的地方",
    (Dimension.FORM, 0): "更想要户外、能走动的地方",
    (Dimension.TRAVEL, 1): "愿意为一个地方多走一段路",
    (Dimension.TRAVEL, 0): "不想在路上花太多时间",
    (Dimension.OBVIOUSNESS, 1): "更想要有名气、稳妥的地方",
    (Dimension.OBVIOUSNESS, 0): "更想要不那么大众的地方",
}


def obvious_tier(obviousness: float | None) -> int | None:
    if obviousness is None:
        return None
    return 0 if obviousness < D3_TIERS[0] else (1 if obviousness < D3_TIERS[1] else 2)


def aim_value(dimension: Dimension, pole: int) -> int:
    """A feedback pole as a target value. For D3 the poles are directions, so they aim at
    the ends; the middle tier is reached only through mixed evidence (see agent.targets)."""
    return 2 * pole if dimension is Dimension.OBVIOUSNESS else pole


def pole_of(dimension: Dimension, value: int | None) -> int | None:
    """Where a candidate's value counts as evidence for a pole. The middle D3 tier is none."""
    if value is None:
        return None
    return {0: 0, 2: 1}.get(value) if dimension is Dimension.OBVIOUSNESS else value


def fit(dimension: Dimension, value: int | None, want: int) -> int:
    """+1 hit, -1 miss, 0 says nothing. One D3 tier away is neither: it is not what was
    asked for, but it is not the opposite either."""
    if value is None:
        return 0
    if dimension is Dimension.OBVIOUSNESS:
        return 1 if value == want else (-1 if abs(value - want) == 2 else 0)
    return 1 if value == want else -1


def aim_label(dimension: Dimension, want: int) -> str:
    if dimension is Dimension.OBVIOUSNESS and want == 1:
        return "有点名气、但不是标准答案的地方"
    return LABELS[(dimension, pole_of(dimension, want))]


def dimension_key(dimension: Dimension, pole: int) -> Key:
    return Key(kind="dimension", key=dimension.value, value=str(pole))


def category_key(kind: str, like: bool) -> Key:
    return Key(kind="category", key=kind, value="like" if like else "dislike")


def place_key(candidate_id: str, value: Literal["never", "favorite"]) -> Key:
    return Key(kind="place", key=candidate_id, value=value)


def note_key(text: str) -> Key:
    return Key(kind="note", key=text.strip()[:60], value="note")


def describe(key: Key, context: Context | None = None, name: str = "") -> str:
    if key.kind == "dimension":
        text = LABELS[(Dimension(key.key), int(key.value))]
    elif key.kind == "category":
        category = kind.category if (kind := KIND_BY_CODE.get(key.key)) else key.key
        text = f"喜欢去{category}" if key.value == "like" else f"不太想去{category}"
    elif key.kind == "place":
        text = f"{'以后别再推' if key.value == 'never' else '喜欢'}{name or key.key}"
    else:
        text = key.key
    return f"{context.label}：{text}" if context else text


def reroll_signals(reason: Reason, kind: str | None) -> list[Signal]:
    """The evidence a reroll carries. Everything not listed here carries none."""
    if reason in TASTE_REASONS:
        dimension, pole = TASTE_REASONS[reason]
        return [Signal(key=dimension_key(dimension, pole), weight=1.0, supports=True),
                Signal(key=dimension_key(dimension, 1 - pole), weight=1.0, supports=False)]
    if reason in KIND_REASONS and kind:
        return [Signal(key=category_key(kind, like=False), weight=1.0, supports=True),
                Signal(key=category_key(kind, like=True), weight=1.0, supports=False)]
    return []


def accept_signals(kind: str | None, axes: dict[Dimension, int | None]) -> list[Signal]:
    """Weak support for where the accepted quest sat. Not a contradiction of the other
    pole: accepting an outdoor walk says little against a taste for galleries."""
    out = [Signal(key=dimension_key(d, pole), weight=ACCEPT_WEIGHT, supports=True)
           for d, value in axes.items() if d in LEARNED_DIMENSIONS
           and (pole := pole_of(d, value)) is not None]
    if kind:
        out += [Signal(key=category_key(kind, like=True), weight=ACCEPT_WEIGHT, supports=True),
                Signal(key=category_key(kind, like=False), weight=ACCEPT_WEIGHT, supports=False)]
    return out


class Belief(BaseModel):
    """A count of observations, deliberately NOT a calibrated probability.

    `uncertainty` only answers "how much have I seen", which is what choose_probe
    needs. Dressing these counts up as a posterior would invent precision the three
    or four data points do not contain.
    """

    high: int = 0
    low: int = 0
    sources: list[str] = Field(default_factory=list)

    @property
    def total(self) -> int:
        return self.high + self.low

    @property
    def value(self) -> float | None:
        """Toward which pole, or None while nothing has been observed."""
        return None if not self.total else self.high / self.total

    @property
    def uncertainty(self) -> float:
        return 1 / (1 + self.total)

    @property
    def unanimous(self) -> bool:
        return self.total > 0 and min(self.high, self.low) == 0

    def observe(self, pole: int, source: str) -> None:
        if pole:
            self.high += 1
        else:
            self.low += 1
        self.sources.append(source)


class Feedback(BaseModel):
    """One reroll. `candidate_ids` is what was on screen when the user rejected it."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    quest_id: str
    candidate_ids: list[str] = Field(default_factory=list, max_length=3)
    anchor_id: str | None = None  # the stop the agent bet on; "never here" refers to it
    kind: str | None = None  # the anchor's category, for "not this kind"
    anchor_obviousness: float | None = Field(default=None, ge=0, le=1)
    reason: Reason
    note: str = Field(default="", max_length=300)
    created_at: datetime = Field(default_factory=lambda: datetime.now(SYDNEY))


class Outcome(BaseModel):
    """What one reroll actually changed. Every field traces back to a Feedback id."""

    feedback_id: str
    dimension: Dimension | None = None
    kind: str | None = None
    request_patch: dict = Field(default_factory=dict)
    clarify: Literal["time"] | None = None
    consumed: list[str] = Field(default_factory=list)
    local: bool = False
    # A statement the chip itself confirms ("以后别推这个"). The caller passes it to
    # confirm(); apply() still never writes memory.
    stated: Proposal | None = None


class TasteState(BaseModel):
    session_id: str = Field(default_factory=lambda: uuid4().hex)
    session: dict[Dimension, Belief] = Field(default_factory=dict)
    session_kinds: dict[str, int] = Field(default_factory=dict)
    memory: Memory = Field(default_factory=Memory)
    # Set by the agent from the request; episodes are only recorded once it is known.
    context: Context | None = None
    incognito: bool = False  # "本次不保存": the session still learns, memory does not
    # What the user typed under "其他" and the model could not map: re-read by infer_intent
    # for the rest of this session, never scored, never remembered.
    session_notes: list[str] = Field(default_factory=list)
    # Proposals that do not come from episode counting (a note distilled from free text).
    # They still go through confirm(); nothing here is active memory.
    pending: list[Proposal] = Field(default_factory=list)
    consumed: dict[str, Literal["shown", "visited"]] = Field(default_factory=dict)
    # Everything rerolled away this session. Plan v2 §4.3 asks for a skip, not a penalty,
    # and it has to accumulate: overwriting it per reroll let a round with no quest (and so
    # no candidate ids) wipe the memory and hand back a place rejected two turns earlier.
    rejected: list[str] = Field(default_factory=list)
    # One-shot, category-local reroll hint. It is consumed by the next planning round and
    # never becomes an episode signal, belief, proposal, or long-term memory.
    local_obviousness: Literal["less", "more"] | None = None
    local_kind: str | None = None
    local_reference: float | None = Field(default=None, ge=0, le=1)

    def record_shown(self, candidate_ids: list[str]) -> None:
        """Dedup hygiene, not a taste signal: having seen a place is not a verdict on it."""
        for cid in candidate_ids:
            self.consumed.setdefault(cid, "shown")

    def _episode(self, event, reason, candidate_ids, signals) -> Episode | None:
        if self.incognito or self.context is None:
            return None
        episode = Episode(session_id=self.session_id, event=event, reason=reason,
                          context=self.context, candidate_ids=list(candidate_ids),
                          signals=signals)
        self.memory.record(episode)
        return episode

    def apply(self, feedback: Feedback) -> Outcome:
        outcome = Outcome(feedback_id=feedback.id)
        self.record_shown(feedback.candidate_ids)
        for cid in feedback.candidate_ids:
            if cid not in self.rejected:
                self.rejected.append(cid)
        reason = feedback.reason
        self._episode("reroll", reason.value, feedback.candidate_ids,
                      reroll_signals(reason, feedback.kind))

        if reason in TASTE_REASONS:
            dimension, pole = TASTE_REASONS[reason]
            self.session.setdefault(dimension, Belief()).observe(pole, feedback.id)
            outcome.dimension = dimension
            # No request_patch: a qualitative complaint never becomes a numeric limit.
            return outcome

        if reason in LOCAL_REASONS:
            self.local_obviousness = "less" if reason is Reason.TOO_OBVIOUS else "more"
            self.local_kind = feedback.kind
            self.local_reference = feedback.anchor_obviousness
            outcome.local = True
            return outcome

        if reason in KIND_REASONS:
            if feedback.kind:
                self.session_kinds[feedback.kind] = self.session_kinds.get(feedback.kind, 0) + 1
                outcome.kind = feedback.kind
            return outcome

        if reason in STATED_REASONS:
            anchor = feedback.anchor_id or next(iter(feedback.candidate_ids), None)
            if anchor:
                outcome.stated = Proposal(action="add", key=place_key(anchor, "never"),
                                          source="user_stated", text=describe(
                                              place_key(anchor, "never")))
            return outcome

        if reason in SESSION_REASONS:
            if feedback.note.strip():
                self.session_notes.append(feedback.note.strip())
            return outcome

        if reason is Reason.NO_SPEND:
            outcome.request_patch = {"budget_aud": 0.0}
            return outcome
        if reason is Reason.BAD_TIME:
            # The user said the time is wrong, not what the right time is. Ask.
            outcome.clarify = "time"
            return outcome

        for cid in feedback.candidate_ids:
            self.consumed[cid] = "visited"
        outcome.consumed = list(feedback.candidate_ids)
        return outcome

    def accept(self, candidate_ids: list[str], kind: str | None,
               axes: dict[Dimension, int | None]) -> Episode | None:
        """"就这个": the only positive signal, and the only implicit one we learn from."""
        self.record_shown(candidate_ids)
        return self._episode("accept", None, candidate_ids, accept_signals(kind, axes))

    def recall(self) -> list[MemoryItem]:
        # Ignore any D3 items left in an older local database after D3 was retired.
        return [item for item in self.memory.recall(self.context)
                if not (item.key.kind == "dimension" and item.key.key == Dimension.OBVIOUSNESS)]

    def effective(self, dimension: Dimension) -> Belief | None:
        """Session feedback, then remembered items for this context (plan v2.2 §3.2)."""
        live = self.session.get(dimension)
        if live and live.total:
            return live
        for item in self.recall():
            if item.key.kind == "dimension" and item.key.key == dimension.value:
                count = max(1, len(item.episode_ids))
                high = count if item.key.value == "1" else 0
                return Belief(high=high, low=count - high, sources=[item.id])
        return None

    def banned(self) -> set[str]:
        return {i.key.key for i in self.recall()
                if i.key.kind == "place" and i.key.value == "never"}

    def notes(self) -> list[str]:
        """Soft prompt context only. Never a score, never a filter."""
        return [i.key.key for i in self.recall() if i.key.kind == "note"]

    def adjustment(self, candidate_id: str, kind: str | None,
                   obviousness: float | None = None) -> tuple[float, list[str]]:
        """Score change for one candidate, and the memory item ids that caused it."""
        session = -SESSION_KIND_PENALTY if kind and self.session_kinds.get(kind) else 0.0
        local = 0.0
        if self.local_obviousness and kind == self.local_kind and obviousness is not None:
            reference = self.local_reference if self.local_reference is not None else 0.5
            delta = ((reference - obviousness) if self.local_obviousness == "less"
                     else (obviousness - reference))
            # Prefer the same kind and the requested direction, without making it a hard gate.
            local = 2.0 + max(-1.0, min(1.0, delta * 2))
        bonus, used = 0.0, []
        for item in self.recall():
            weight = 0.0
            if item.key.kind == "category" and kind == item.key.key:
                weight = CATEGORY_WEIGHT if item.key.value == "like" else -CATEGORY_WEIGHT
            elif item.key.kind == "place" and item.key.value == "favorite" \
                    and item.key.key == candidate_id:
                weight = FAVORITE_WEIGHT
            if weight:
                bonus += weight / 2 if self.memory.stale(item) else weight
                used.append(item.id)
        return session + local + max(-MEMORY_CAP, min(MEMORY_CAP, bonus)), used

    def clear_local_reroll(self) -> None:
        self.local_obviousness = None
        self.local_kind = None
        self.local_reference = None

    def propose_note(self, text: str) -> Proposal | None:
        """Offer to remember a sentence the user said. Only confirm() makes it memory."""
        key = note_key(text)
        if not key.key or any(i.key == key for i in self.memory.active()) \
                or any(p.key == key for p in self.pending):
            return None
        proposal = Proposal(action="add", key=key, source="agent_proposed", text=key.key)
        if proposal.signature in self.memory.declined:
            return None
        self.pending.append(proposal)
        return proposal

    def proposals(self) -> list[Proposal]:
        out = [proposal for proposal in self.memory.proposals()
               if not (proposal.key.kind == "dimension"
                       and proposal.key.key == Dimension.OBVIOUSNESS)]
        for proposal in out:
            where = None if proposal.action == "narrow" else proposal.context
            text = describe(proposal.key, where)
            if proposal.action == "narrow":
                text = f"「{text}」只在{'、'.join(c.label for c in proposal.contexts)}时成立"
            elif proposal.action == "retire":
                text = f"不再记住「{text}」"
            proposal.text = text
        return [*out, *self.pending]

    def confirm(self, proposal: Proposal) -> list[MemoryItem]:
        """The only path into long-term memory. Nothing else may write it."""
        self.pending = [p for p in self.pending if p.signature != proposal.signature]
        return self.memory.confirm(proposal)

    def decline(self, proposal: Proposal) -> None:
        self.pending = [p for p in self.pending if p.signature != proposal.signature]
        self.memory.decline(proposal)


def dimensions_of(
    tags: set[str] | list[str], mean_km: float, obviousness: float | None
) -> dict[Dimension, int | None]:
    """Where a candidate sits on each dimension. The single definition of the poles.

    `scripts/check_dimensions.py` and the agent both call this: if the gate and the
    selector ever disagreed about what D1 means, the eval would be measuring something
    the product does not do. None means the candidate says nothing about that dimension.
    """
    tags = set(tags)
    return {
        Dimension.FORM: 1 if "室内" in tags else (0 if "户外" in tags else None),
        Dimension.TRAVEL: int(mean_km > FAR_KM),
        Dimension.OBVIOUSNESS: obvious_tier(obviousness),
    }
