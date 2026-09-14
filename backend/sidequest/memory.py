"""Long-term memory: typed items consolidated from episodes (plan v2.2 §7.4).

This module knows nothing about reroll reasons. `taste.py` turns a reroll or an accept
into `Signal`s -- which item it supports or contradicts, and how strongly -- and this
module only counts them. Keeping the evidence rules in one place and the bookkeeping in
another is what lets "a reason that is not evidence" stay a one-line decision.

The rules that are easy to erode:

* Nothing here writes an active item except `confirm()`. Consolidation only makes an
  item *proposable*; the model may choose when to ask, never what is askable.
* Evidence is counted per context. A contradiction in another context is not a
  contradiction -- it is exactly the case that context-conditioned memory exists for.
* One session is not a habit: a proposal needs support from at least two sessions.
* Deleting is immediate and sticky: a deleted item stops affecting the next round and is
  not re-proposed from the same evidence.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .models import SYDNEY

# Initial values, to be calibrated on the cross-session harness (plan v2.2 §13 item 8).
PROPOSE_SUPPORT = 3.0  # weighted support in one context before an item is proposable
PROPOSE_SESSIONS = 2  # ... spread over at least this many sessions
PROPOSE_MAX_CONTRA = 0.0  # ... with no more contradiction than this in that context
REVISE_CONTRA = 2.0  # contradiction since confirmation that triggers narrow / retire
REPROPOSE_FACTOR = 2.0  # a declined proposal returns only once its support has doubled
STALE_SESSIONS = 10  # trailing matching sessions without support before an item is stale

ItemKind = Literal["dimension", "category", "place", "note"]
Source = Literal["user_stated", "agent_proposed"]


class Slot(StrEnum):
    LUNCH = "lunch"
    AFTER_WORK = "after_work"
    WEEKEND_DAY = "weekend_day"
    OTHER = "other"


class Span(StrEnum):
    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"


SLOT_LABELS = {
    Slot.LUNCH: "工作日午休",
    Slot.AFTER_WORK: "工作日下班后",
    Slot.WEEKEND_DAY: "周末白天",
    Slot.OTHER: "其他时段",
}
SPAN_LABELS = {Span.SHORT: "一个半小时以内", Span.MEDIUM: "半天以内", Span.LONG: "大半天"}


class Context(BaseModel, frozen=True):
    """A closed bucket computed from the request. Never asked, never named by the model."""

    slot: Slot
    span: Span

    @property
    def label(self) -> str:
        return f"{SLOT_LABELS[self.slot]} · {SPAN_LABELS[self.span]}"


def context_of(departure: datetime, deadline: datetime) -> Context:
    local = departure.astimezone(SYDNEY)
    hour = local.hour + local.minute / 60
    if local.weekday() >= 5:
        slot = Slot.WEEKEND_DAY if 8 <= hour < 18 else Slot.OTHER
    elif 11 <= hour < 14:
        slot = Slot.LUNCH
    elif 17 <= hour < 21:
        slot = Slot.AFTER_WORK
    else:
        slot = Slot.OTHER
    minutes = (deadline - departure).total_seconds() / 60
    span = Span.SHORT if minutes < 90 else Span.MEDIUM if minutes < 240 else Span.LONG
    return Context(slot=slot, span=span)


class Key(BaseModel, frozen=True):
    """What an item is about. `value` is the pole, like/dislike, never/favorite or 'note'."""

    kind: ItemKind
    key: str
    value: str


class Signal(BaseModel, frozen=True):
    key: Key
    weight: float
    supports: bool


class Episode(BaseModel):
    """One reroll or accept, with the context it happened in. Append-only, deletable."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    session_id: str
    event: Literal["reroll", "accept"]
    reason: str | None = None  # audit trail for the drawer; evidence lives in `signals`
    context: Context
    candidate_ids: list[str] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(SYDNEY))


class MemoryItem(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    key: Key
    context: Context | None = None  # None = every context
    status: Literal["active", "retired"] = "active"
    source: Source
    episode_ids: list[str] = Field(default_factory=list)
    confirmed_at: datetime = Field(default_factory=lambda: datetime.now(SYDNEY))
    label: str = ""  # wording at confirmation time, e.g. a place's name the key cannot carry


class Proposal(BaseModel):
    """A change to long-term memory the user is asked to confirm. Never auto-applied."""

    action: Literal["add", "narrow", "retire"]
    key: Key
    context: Context | None = None  # for "add"
    contexts: list[Context] = Field(default_factory=list)  # for "narrow"
    item_id: str | None = None  # the item a narrow / retire acts on
    replaces: list[str] = Field(default_factory=list)  # context items a global add supersedes
    episode_ids: list[str] = Field(default_factory=list)
    support: float = 0.0
    source: Source = "agent_proposed"
    text: str = ""  # filled by taste.py, which owns the wording

    @property
    def signature(self) -> str:
        where = self.context.label if self.context else "*"
        return "|".join((self.action, self.key.kind, self.key.key, self.key.value, where,
                         self.item_id or ""))


@dataclass
class Tally:
    support: float = 0.0
    contra: float = 0.0
    rerolled: bool = False  # some support came from what the user asked for, not an accept
    sessions: set[str] = field(default_factory=set)
    support_ids: list[str] = field(default_factory=list)
    contra_ids: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return (self.support >= PROPOSE_SUPPORT and len(self.sessions) >= PROPOSE_SESSIONS
                and self.contra <= PROPOSE_MAX_CONTRA)


class Memory(BaseModel):
    episodes: list[Episode] = Field(default_factory=list)
    items: list[MemoryItem] = Field(default_factory=list)
    declined: dict[str, float] = Field(default_factory=dict)  # signature -> support then

    def record(self, episode: Episode) -> None:
        self.episodes.append(episode)

    def active(self) -> list[MemoryItem]:
        return [i for i in self.items if i.status == "active"]

    def tally(self, key: Key, context: Context, since: datetime | None = None) -> Tally:
        t = Tally()
        for episode in self.episodes:
            if episode.context != context or (since and episode.created_at <= since):
                continue
            for signal in episode.signals:
                if signal.key != key:
                    continue
                if signal.supports:
                    t.support += signal.weight
                    t.rerolled = t.rerolled or episode.event == "reroll"
                    t.sessions.add(episode.session_id)
                    if episode.id not in t.support_ids:
                        t.support_ids.append(episode.id)
                else:
                    t.contra += signal.weight
                    if episode.id not in t.contra_ids:
                        t.contra_ids.append(episode.id)
        return t

    def contexts(self, key: Key) -> list[Context]:
        seen: list[Context] = []
        for episode in self.episodes:
            if episode.context not in seen and any(s.key == key for s in episode.signals):
                seen.append(episode.context)
        return seen

    def proposals(self) -> list[Proposal]:
        out: list[Proposal] = []
        keys: list[Key] = []
        for episode in self.episodes:
            for signal in episode.signals:
                # Places and notes are only ever stated by the user, never consolidated.
                if signal.supports and signal.key.kind in ("dimension", "category") \
                        and signal.key not in keys:
                    keys.append(signal.key)

        for key in keys:
            active = [i for i in self.active() if i.key == key]
            if any(i.context is None for i in active):
                continue
            tallies = {c: self.tally(key, c) for c in self.contexts(key)}
            ready = [c for c, t in tallies.items() if t.ready]
            covered = {i.context for i in active}
            clean = all(t.contra <= PROPOSE_MAX_CONTRA for t in tallies.values())
            # Agreement across contexts is the strongest sign of a global taste, so pooled
            # support over >=2 clean contexts is enough even when no context is ready alone.
            # One contradiction anywhere still keeps the key context-scoped. Pooling needs a
            # reroll in >=2 of those contexts: accepts alone mostly echo the ranker's own
            # defaults (a short window leans near), and pooling them turned that echo into
            # a global "near" proposal for people with no distance preference.
            supporting = [t for t in tallies.values() if t.support > 0]
            pooled = (sum(1 for t in supporting if t.rerolled) >= 2
                      and sum(t.support for t in supporting) >= PROPOSE_SUPPORT
                      and len(set().union(*(t.sessions for t in supporting))) >= PROPOSE_SESSIONS)
            if clean and (len(set(ready) | covered) >= 2 or pooled):
                ids = [e for c in tallies for e in tallies[c].support_ids]
                out.append(Proposal(action="add", key=key, context=None, episode_ids=ids,
                                    replaces=[i.id for i in active],
                                    support=sum(t.support for t in tallies.values())))
                continue
            for c in ready:
                if c not in covered:
                    out.append(Proposal(action="add", key=key, context=c,
                                        episode_ids=tallies[c].support_ids,
                                        support=tallies[c].support))

        out.extend(self.revisions())
        return [p for p in out
                if p.signature not in self.declined
                or p.support >= self.declined[p.signature] * REPROPOSE_FACTOR]

    def revisions(self) -> list[Proposal]:
        """Confirmed items that keep being contradicted where they apply."""
        out: list[Proposal] = []
        for item in self.active():
            if item.source != "agent_proposed":
                continue  # the user said it; only the user takes it back
            where = [item.context] if item.context else self.contexts(item.key)
            recent = {c: self.tally(item.key, c, since=item.confirmed_at) for c in where}
            hot = [c for c, t in recent.items() if t.contra >= REVISE_CONTRA]
            if not hot:
                continue
            contra_ids = [e for c in hot for e in recent[c].contra_ids]
            weight = sum(recent[c].contra for c in hot)
            keep = [] if item.context else [
                c for c in where
                if c not in hot and self.tally(item.key, c).support > 0 and not recent[c].contra
            ]
            if keep:
                out.append(Proposal(action="narrow", key=item.key, item_id=item.id,
                                    contexts=keep, episode_ids=contra_ids, support=weight))
            else:
                out.append(Proposal(action="retire", key=item.key, item_id=item.id,
                                    context=item.context, episode_ids=contra_ids,
                                    support=weight))
        return out

    def confirm(self, proposal: Proposal) -> list[MemoryItem]:
        """The only path to an active item."""
        by_id = {i.id: i for i in self.items}
        created: list[MemoryItem] = []
        if proposal.action == "add":
            for item_id in proposal.replaces:
                if item_id in by_id:
                    by_id[item_id].status = "retired"
            for item in self.active():
                # Two poles of one key cannot both hold in the same context.
                if (item.key.kind, item.key.key) == (proposal.key.kind, proposal.key.key) \
                        and item.key.value != proposal.key.value \
                        and item.context == proposal.context:
                    item.status = "retired"
            created.append(MemoryItem(key=proposal.key, context=proposal.context,
                                      source=proposal.source, label=proposal.text,
                                      episode_ids=list(proposal.episode_ids)))
        elif proposal.action == "narrow":
            if proposal.item_id in by_id:
                by_id[proposal.item_id].status = "retired"
            for c in proposal.contexts:
                created.append(MemoryItem(key=proposal.key, context=c, source=proposal.source,
                                          episode_ids=self.tally(proposal.key, c).support_ids))
        elif proposal.item_id in by_id:
            by_id[proposal.item_id].status = "retired"
        self.items.extend(created)
        return created

    def decline(self, proposal: Proposal) -> None:
        self.declined[proposal.signature] = max(proposal.support, 1.0)

    def forget(self, item_id: str) -> bool:
        """User deletion. Also blocks re-proposing the same item from the same evidence."""
        item = next((i for i in self.items if i.id == item_id), None)
        if item is None:
            return False
        self.items.remove(item)
        if item.source == "agent_proposed":
            where = [item.context] if item.context else self.contexts(item.key)
            support = sum(self.tally(item.key, c).support for c in where)
            self.decline(Proposal(action="add", key=item.key, context=item.context,
                                  support=support))
        return True

    def forget_episode(self, episode_id: str) -> bool:
        """Delete an episode and recount every proposed item that leaned on it."""
        episode = next((e for e in self.episodes if e.id == episode_id), None)
        if episode is None:
            return False
        self.episodes.remove(episode)
        for item in list(self.items):
            if episode_id not in item.episode_ids:
                continue
            item.episode_ids.remove(episode_id)
            if item.source == "user_stated":
                continue
            where = [item.context] if item.context else self.contexts(item.key)
            if sum(self.tally(item.key, c).support for c in where) < PROPOSE_SUPPORT:
                self.items.remove(item)  # back to unconfirmed; may be proposed again later
        return True

    def recall(self, context: Context | None) -> list[MemoryItem]:
        """Items that apply here. A context-specific item beats a global one on its key."""
        here = [i for i in self.active() if i.context is None or i.context == context]
        specific = {(i.key.kind, i.key.key) for i in here if i.context is not None}
        return [i for i in here if i.context is not None
                or (i.key.kind, i.key.key) not in specific]

    def stale(self, item: MemoryItem) -> bool:
        sessions: list[str] = []
        supported: set[str] = set()
        for episode in self.episodes:
            if episode.created_at <= item.confirmed_at:
                continue
            if item.context is not None and episode.context != item.context:
                continue
            if episode.session_id not in sessions:
                sessions.append(episode.session_id)
            if any(s.key == item.key and s.supports for s in episode.signals):
                supported.add(episode.session_id)
        trailing = 0
        for session_id in reversed(sessions):
            if session_id in supported:
                break
            trailing += 1
        return trailing >= STALE_SESSIONS


def memory_id(owner: str) -> str:
    return f"memory:{owner}"


def load(store, owner: str) -> Memory:
    """Owner-scoped like every other document: another session's memory is unreadable."""
    row = store.get(owner, memory_id(owner))
    return Memory.model_validate(row["data"]) if row else Memory()


def save(store, owner: str, memory: Memory) -> None:
    data = memory.model_dump(mode="json")
    if not store.change(owner, memory_id(owner), data=data):
        store.put(owner, "memory_profile", data, id=memory_id(owner))
