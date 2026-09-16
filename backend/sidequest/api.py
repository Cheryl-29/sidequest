import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from . import memory as memory_store
from .agent import AgentResult, Intent, interpret_feedback, propose_quest, rank_itineraries
from .fixtures import ORIGINS
from .llm import ModelError, OpenAIModel, openai_configured
from .memory import Proposal, context_of
from .models import SYDNEY, Candidate, Request, Result, Trace
from .places import candidate_kind
from .planner import distance, plan, request_origin
from .providers import tfnsw_configured
from .storage import Store
from .taste import (
    LOCAL_REASONS,
    TASTE_REASONS,
    Dimension,
    Feedback,
    Reason,
    TasteState,
    describe,
    dimensions_of,
    note_key,
)

logger = logging.getLogger("sidequest")


class Revision(BaseModel):
    locked_ids: list[str] | None = Field(default=None, max_length=3)
    excluded_ids: list[str] | None = Field(default=None, max_length=12)
    preference: str | None = Field(default=None, max_length=300)
    deadline: datetime | None = None
    stay_minutes: dict[str, int] | None = None
    max_stops: int | None = Field(default=None, ge=1, le=3)


AGENT_DECISIONS = 8  # intent, probe, search, narration, plus room for one fallback narration
AGENT_TIMEOUT = 45.0  # leaves the fixed planner room inside the 60 s server deadline

Strategy = Literal["fixed", "agent"]


class Reroll(BaseModel):
    itinerary_id: str = Field(max_length=100)
    reason: Reason
    note: str = Field(default="", max_length=100)  # the "其他" sentence; data, never instructions


class Accept(BaseModel):
    itinerary_id: str = Field(max_length=100)


class Decision(BaseModel):
    signature: str = Field(max_length=500)
    accept: bool


class Note(BaseModel):
    text: str = Field(min_length=1, max_length=60)


class Settings(BaseModel):
    incognito: bool


REASON_LABELS = {
    Reason.WANT_SIT: "想坐着",
    Reason.WANT_MOVE: "想动一动",
    Reason.TOO_FAR: "太远了",
    Reason.WANT_FARTHER: "想走远点",
    Reason.TOO_OBVIOUS: "太大众了",
    Reason.TOO_OBSCURE: "太冷门了",
    Reason.NOT_THIS_KIND: "不想要这类",
    Reason.NEVER_HERE: "以后别推这个",
    Reason.NO_SPEND: "不想花钱",
    Reason.BAD_TIME: "时间不合适",
    Reason.BEEN_THERE: "去过了",
    Reason.OFF_ROUTE: "顺路的站太绕",
    Reason.OTHER: "其他",
}
ACTION_LABELS = {"add": "记住", "narrow": "收窄", "retire": "不再记住"}


def proposal_view(proposal: Proposal) -> dict:
    return {
        "signature": proposal.signature,
        "action": proposal.action,
        "text": proposal.text,
        "evidence": len(proposal.episode_ids),
    }


class AgentAborted(Exception):
    """Raised inside the agent thread once its run has been abandoned (timeout, cancel)."""


def agent_payload(request: Request, outcome: AgentResult, elapsed_ms: float) -> dict:
    """An agent round in the shape the client already renders, plus what the bet was."""
    quest = outcome.quest
    itineraries = []
    if quest:
        itineraries = [quest.itinerary.model_copy(update={"title": quest.brief,
                                                          "reason": quest.hook})]
    result = Result(
        request=request, itineraries=itineraries, trace=outcome.trace, rejected=[],
        status="completed" if quest else "search_exhausted", message=outcome.message,
        tool_calls=0, cache_hits=0, elapsed_ms=elapsed_ms, strategy="agent-v1",
    ).model_dump(mode="json")
    result["agent"] = {
        "anchor_id": quest.anchor_id if quest else None,
        "memory_ids": quest.memory_ids if quest else [],
        "evidence_ids": quest.evidence_ids if quest else [],
        "probe": quest.probe.model_dump(mode="json") if quest else None,
        "intent": quest.intent.model_dump(mode="json") if quest else None,
        "seed": outcome.seed,
        "model_calls": outcome.model_calls,
        "tokens": outcome.tokens,
    }
    return result


def create_app(store: Store | None = None, model_factory=None, agent_timeout=AGENT_TIMEOUT):
    db = store or Store()
    # Injected in tests; in the app the key only ever comes from .env via llm.py.
    make_model = model_factory or (lambda: OpenAIModel(limit=AGENT_DECISIONS))
    agent_available = (lambda: True) if model_factory else openai_configured
    tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(app):
        db.recover()
        yield
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Sidequest", version="0.1.0", lifespan=lifespan)

    def owner(sidequest_session: str | None = Cookie(default=None)):
        if not sidequest_session or not db.get(sidequest_session, sidequest_session):
            raise HTTPException(401, "请先初始化本地演示会话")
        return sidequest_session

    # One taste session per quest thread: a fresh "领一个支线" starts it, rerolls and
    # revisions continue it. Long-term memory lives in its own owner-scoped document.
    def taste_id(uid):
        return f"taste:{uid}"

    def load_taste(uid) -> tuple[TasteState, dict]:
        row = db.get(uid, taste_id(uid))
        data = row["data"] if row else {"state": {}, "offered": False}
        state = TasteState.model_validate(data["state"])
        state.memory = memory_store.load(db, uid)
        return state, data

    def save_taste(uid, state: TasteState, offered: bool):
        memory_store.save(db, uid, state.memory)
        data = {"state": state.model_dump(mode="json", exclude={"memory"}), "offered": offered}
        if not db.change(uid, taste_id(uid), data=data):
            db.put(uid, "taste_session", data, id=taste_id(uid))

    def offer(state: TasteState, data: dict) -> dict | None:
        """At most one unprompted proposal per session (plan v2.2 §2.2); the drawer lists all."""
        proposals = state.proposals()
        if not proposals or data.get("offered"):
            return None
        data["offered"] = True
        return proposal_view(proposals[0])

    def with_bans(request: Request, state: TasteState, extra=()) -> Request:
        """Rerolled-away and never-again places join the exclusions; a lock still wins."""
        locked = set(request.locked_ids)
        ids = [i for i in dict.fromkeys([*request.excluded_ids, *extra, *sorted(state.banned())])
               if i not in locked]
        return request.model_copy(update={"excluded_ids": ids[-64:]})

    def get_run(uid, run_id):
        run = db.get(uid, run_id)
        if not run or run["kind"] != "run":
            raise HTTPException(404, "未找到这次规划")
        return run

    async def fixed_round(uid, request, previous, note=None, keep=None):
        if keep:
            # Only this round is locked; the stored request stays unlocked, so a later
            # "不想要这类" on the same place is not overruled by a lock the user never set.
            request = request.model_copy(
                update={"locked_ids": list(dict.fromkeys([*request.locked_ids, keep]))[:3]})
        result, cache = await asyncio.wait_for(
            asyncio.to_thread(plan, request, 20, previous), timeout=60
        )
        if note:
            result.trace.insert(0, Trace(sequence=0, node="agent", action="degrade",
                                         summary=note, elapsed_ms=0.0))
            for i, step in enumerate(result.trace, 1):
                step.sequence = i
            result.strategy = "fixed-v1（降级）"
        state, data = load_taste(uid)
        rank_itineraries(result, state)
        state.clear_local_reroll()
        if result.itineraries:
            state.record_shown([s.candidate.id for s in result.itineraries[0].stops])
        save_taste(uid, state, data.get("offered", False))
        return result.model_dump(mode="json"), cache, result.status

    async def agent_round(uid, run_id, request, record):
        state, data = load_taste(uid)
        progress: list[dict] = []
        abandoned = False

        def on_step(step):
            if abandoned:
                raise AgentAborted()
            progress.append(step.model_dump(mode="json"))
            current = get_run(uid, run_id)["data"]
            current["progress"] = progress
            if not db.change(uid, run_id, data=current, expected=["running"]):
                raise AgentAborted()  # cancelled while the model was thinking

        intent, keep = record.get("intent"), record.get("keep")
        started = datetime.now()
        try:
            model = make_model()
            outcome = await asyncio.wait_for(asyncio.to_thread(
                propose_quest, request, state, model, request.preference,
                None if intent is None else Intent.model_validate(intent), 3, None, on_step, keep,
            ), timeout=agent_timeout)
        except (ModelError, asyncio.TimeoutError) as exc:
            abandoned = True
            reason = "模型超时" if isinstance(exc, asyncio.TimeoutError) else str(exc)
            logger.warning("Agent degraded for %s: %s", run_id, reason)
            return await fixed_round(uid, request, None,
                                     f"{reason}，这一轮改用固定策略，没有调用模型推荐", keep)
        elapsed = round((datetime.now() - started).total_seconds() * 1000, 2)
        if outcome.quest is None and outcome.model_calls:
            # The agent only tries a few bets. When they all fail the clock, a wider search may
            # still find something; say that the model's pick is not what came back.
            save_taste(uid, state, data.get("offered", False))
            return await fixed_round(uid, request, None,
                                     "模型押注的几个地方都赶不回来，这一轮改用固定策略找了一个走得通的",
                                     keep)
        state.clear_local_reroll()
        save_taste(uid, state, data.get("offered", False))
        payload = agent_payload(request, outcome, elapsed)
        return payload, {}, payload["status"]

    async def execute(uid, run_id, request, previous):
        if not db.change(uid, run_id, status="running", expected=["queued"]):
            return
        try:
            record = get_run(uid, run_id)["data"]
            if record.get("strategy") == "agent":
                result, cache, status = await agent_round(uid, run_id, request, record)
            else:
                result, cache, status = await fixed_round(uid, request, previous,
                                                          keep=record.get("keep"))
            record = get_run(uid, run_id)["data"]
            record.update(result=result, cache=cache)
            # Cancellation and supersession are compare-and-swap guards against late writes.
            db.change(uid, run_id, data=record, status=status, expected=["running"])
        except AgentAborted:
            return
        except asyncio.CancelledError:
            db.change(uid, run_id, status="interrupted", expected=["running", "queued"])
            raise
        except Exception:
            logger.exception("Planning failed: %s", run_id)
            db.change(uid, run_id, status="failed", expected=["running"])

    def launch(uid, request, parent=None, previous=None, key=None, strategy="fixed", intent=None,
               keep=None):
        digest = hashlib.sha256(f"{strategy}:{request.model_dump_json()}".encode()).hexdigest()
        run_id = hashlib.sha256(f"{uid}:{key}".encode()).hexdigest()[:32] if key else uuid4().hex
        old = db.get(uid, run_id)
        if old:
            if old["data"].get("digest") != digest:
                raise HTTPException(409, "相同幂等键不能用于不同请求")
            return {"id": run_id, "status": old["status"]}
        recent = db.list(uid, "run", 20)
        if (
            len(
                [
                    r
                    for r in recent
                    if datetime.fromisoformat(r["created_at"]).timestamp()
                    > datetime.now().timestamp() - 60
                ]
            )
            >= 15
        ):
            raise HTTPException(429, "请求过于频繁，请稍后再试")
        data = {
            "request": request.model_dump(mode="json"),
            "parent_id": parent,
            "digest": digest,
            "result": None,
            "cache": {},
            "strategy": strategy,
            "intent": intent,
            "keep": keep,
        }
        try:
            db.put(uid, "run", data, id=run_id, status="queued")
        except IntegrityError:
            old = get_run(uid, run_id)
            if old["data"].get("digest") != digest:
                raise HTTPException(409, "幂等键冲突")
            return {"id": run_id, "status": old["status"]}
        task = asyncio.create_task(execute(uid, run_id, request, previous))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"id": run_id, "status": "queued"}

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "version": "0.1.0",
            "modes": ["replay", "live"],
            "live_configured": tfnsw_configured(),
            "strategy": "fixed-v1",
            "strategies": ["fixed", "agent"] if agent_available() else ["fixed"],
        }

    @app.get("/api/session")
    def session(response: Response, sidequest_session: str | None = Cookie(default=None)):
        uid = sidequest_session
        if not uid or not db.get(uid, uid):
            uid = uuid4().hex
            db.put(uid, "session", {}, id=uid)
        response.set_cookie(
            "sidequest_session", uid, httponly=True, samesite="strict", max_age=60 * 60 * 24 * 30
        )
        now = datetime.now(SYDNEY)
        return {
            "origins": ORIGINS,
            "now": now.isoformat(),
            "today": now.date().isoformat(),
            "tomorrow": (now + timedelta(days=1)).date().isoformat(),
            "mode": "replay",
            "available_modes": ["replay", "live"] if tfnsw_configured() else ["replay"],
            "agent_available": agent_available(),
        }

    @app.post("/api/runs", status_code=202)
    async def create_run_async(
        request: Request,
        uid=Depends(owner),
        idempotency_key: str | None = Header(default=None, max_length=100),
        strategy: Strategy = "fixed",
    ):
        if strategy == "agent" and not agent_available():
            raise HTTPException(422, "模型未配置，只能使用固定策略")
        state, data = load_taste(uid)
        retry = idempotency_key and db.get(
            uid, hashlib.sha256(f"{uid}:{idempotency_key}".encode()).hexdigest()[:32]
        )
        if not retry:  # a double-click must not wipe the session it started
            state = TasteState(memory=state.memory, incognito=state.incognito)
            save_taste(uid, state, offered=False)
        return launch(uid, with_bans(request, state), key=idempotency_key, strategy=strategy)

    @app.get("/api/runs")
    def history(uid=Depends(owner)):
        return [
            {
                "id": r["id"],
                "status": r["status"],
                "created_at": r["created_at"],
                "request": r["data"]["request"],
            }
            for r in db.list(uid, "run")
        ]

    @app.get("/api/runs/{run_id}")
    def read_run(run_id: str, uid=Depends(owner)):
        r = get_run(uid, run_id)
        return {
            "id": run_id,
            "status": r["status"],
            "parent_id": r["data"].get("parent_id"),
            "result": r["data"].get("result"),
        }

    @app.post("/api/runs/{run_id}/revise", status_code=202)
    async def revise(run_id: str, revision: Revision, uid=Depends(owner)):
        r = get_run(uid, run_id)
        if r["status"] in ("queued", "running"):
            raise HTTPException(409, "请等待本次规划结束或先取消")
        merged = r["data"]["request"] | revision.model_dump(mode="json", exclude_none=True)
        try:
            request = Request.model_validate(merged)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return launch(uid, request, parent=run_id, previous=r["data"].get("cache"))

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str, uid=Depends(owner)):
        get_run(uid, run_id)
        changed = db.change(uid, run_id, status="cancelled", expected=["queued", "running"])
        return {"cancelled": changed, "status": get_run(uid, run_id)["status"]}

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, uid=Depends(owner)):
        get_run(uid, run_id)

        async def stream():
            last_status = None
            sent = 0  # agent steps stream as they happen; the rest arrive with the result
            for _ in range(650):
                r = get_run(uid, run_id)
                if r["status"] != last_status:
                    yield f"event: status\ndata: {json.dumps({'status': r['status']})}\n\n"
                    last_status = r["status"]
                for step in r["data"].get("progress", [])[sent:]:
                    sent += 1
                    yield f"event: trace\ndata: {json.dumps(step, ensure_ascii=False)}\n\n"
                if r["status"] not in ("queued", "running"):
                    result = r["data"].get("result")
                    if result:
                        for step in result["trace"][sent:]:
                            yield f"event: trace\ndata: {json.dumps(step, ensure_ascii=False)}\n\n"
                    yield f"event: done\ndata: {json.dumps({'id': run_id, 'status': r['status'], 'result': result}, ensure_ascii=False)}\n\n"
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def quest_of(uid, run_id, itinerary_id):
        r = get_run(uid, run_id)
        if r["status"] in ("queued", "running"):
            raise HTTPException(409, "请等待本次规划结束或先取消")
        result = r["data"].get("result") or {}
        itinerary = next(
            (i for i in result.get("itineraries", []) if i["id"] == itinerary_id), None
        )
        if itinerary is None:
            raise HTTPException(404, "未找到这个支线")
        request = Request.model_validate(r["data"]["request"])
        stops = [Candidate.model_validate(s["candidate"]) for s in itinerary["stops"]]
        # An agent quest knows which stop it bet on; the fixed planner's first stop stands in.
        anchor_id = (result.get("agent") or {}).get("anchor_id")
        stops.sort(key=lambda c: c.id != anchor_id)
        return r, request, stops

    @app.post("/api/runs/{run_id}/reroll", status_code=202)
    async def reroll(run_id: str, body: Reroll, uid=Depends(owner)):
        r, request, stops = quest_of(uid, run_id, body.itinerary_id)
        strategy = r["data"].get("strategy", "fixed")
        state, data = load_taste(uid)
        state.context = context_of(request.departure, request.deadline)
        anchor = stops[0]
        reason, lasting, understood = body.reason, "", True
        if reason is Reason.OTHER:
            if not body.note.strip():
                raise HTTPException(422, "写一句为什么想换")
            if strategy == "agent" and agent_available():
                quest = r["data"]["result"]["itineraries"][0]["title"]
                try:
                    reason, lasting = await asyncio.wait_for(asyncio.to_thread(
                        interpret_feedback, make_model(), body.note, quest, stops,
                        request_origin(request)), timeout=15)
                except (ModelError, asyncio.TimeoutError):
                    understood = False
            else:
                understood = False
        if reason is Reason.OFF_ROUTE and len(stops) < 2:
            raise HTTPException(422, "这一趟只有一站，没有顺路的站可换")
        # Complaining about the side stops rejects only them; the anchor goes into the next round.
        rejected = stops[1:] if reason is Reason.OFF_ROUTE else stops
        outcome = state.apply(Feedback(
            quest_id=body.itinerary_id, candidate_ids=[c.id for c in rejected][:3],
            anchor_id=anchor.id, kind=candidate_kind(anchor), reason=reason,
            anchor_obviousness=anchor.obviousness, note=body.note.strip(),
        ))
        label = REASON_LABELS[reason]
        if lasting and reason is Reason.OTHER:
            # Mapped text already counts as evidence for a typed item, and that item has to earn
            # its proposal across sessions. A note would let one sentence skip that rule.
            state.propose_note(lasting)  # offered like any proposal; only confirm() writes it
        if outcome.keep:
            message = (f"理解成「{label}」：" if body.reason is Reason.OTHER else "") + \
                f"保留 {anchor.name}，只换顺路的站。"
        elif body.reason is Reason.OTHER and reason is not Reason.OTHER:
            message = f"理解成「{label}」。"
        elif reason is Reason.OTHER:
            message = ("这句话会带进这一轮的推荐，但不会被记住。" if understood
                       else "这会儿没法理解这句话，先换一个；它只留在这一轮。")
        elif outcome.stated:
            # The chip is the user's own long-term statement; it confirms itself.
            outcome.stated.text = f"以后别再推 {anchor.name}"
            state.confirm(outcome.stated)
            message = f"记住了：以后不再推 {anchor.name}。随时可以在「我的偏好」里删掉。"
        elif body.reason in TASTE_REASONS:
            message = f"「{label}」只影响这一轮；要是换个时候还这么说，才会问你要不要记住。"
        elif reason in LOCAL_REASONS:
            message = f"这一轮优先在同类地点里往{'冷门' if reason is Reason.TOO_OBVIOUS else '大众'}一点换；不会记成长期偏好。"
        elif outcome.kind:
            message = "这一轮先避开这类地方。"
        elif body.reason is Reason.BEEN_THERE:
            message = "记下了，这几处这次不再出现。“去过了”不会被当成你不喜欢。"
        else:
            message = ""
        if outcome.clarify:
            save_taste(uid, state, data["offered"])
            return {"id": None, "clarify": outcome.clarify, "message": "想换到什么时候？",
                    "proposal": None}
        try:
            merged = Request.model_validate(
                request.model_dump(mode="json") | outcome.request_patch
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        proposal = offer(state, data)
        save_taste(uid, state, data["offered"])
        # Re-infer only when the user added words the parent intent never saw.
        intent = None if reason is Reason.OTHER else r["data"].get("intent") or (
            (r["data"]["result"].get("agent") or {}).get("intent"))
        launched = launch(uid, with_bans(merged, state, state.rejected), parent=run_id,
                          previous=r["data"].get("cache"), strategy=strategy, intent=intent,
                          keep=outcome.keep)
        return {**launched, "clarify": None, "message": message, "proposal": proposal}

    @app.post("/api/runs/{run_id}/accept")
    def accept(run_id: str, body: Accept, uid=Depends(owner)):
        _, request, stops = quest_of(uid, run_id, body.itinerary_id)
        state, data = load_taste(uid)
        state.context = context_of(request.departure, request.deadline)
        anchor = stops[0]
        axes = dimensions_of(anchor.tags, distance(request_origin(request), anchor.model_dump()),
                             anchor.obviousness)
        episode = state.accept([c.id for c in stops], candidate_kind(anchor), axes)
        proposal = offer(state, data)
        save_taste(uid, state, data["offered"])
        return {"recorded": episode is not None, "proposal": proposal}

    @app.get("/api/taste")
    def taste(uid=Depends(owner)):
        state, _ = load_taste(uid)
        items = []
        for item in state.memory.active():
            text = item.label if item.key.kind == "place" and item.label else describe(item.key)
            items.append({
                "id": item.id,
                "kind": item.key.kind,
                "text": text,
                "context": item.context.label if item.context else None,
                "source": item.source,
                "evidence": len(item.episode_ids),
                "stale": state.memory.stale(item),
                "confirmed_at": item.confirmed_at.isoformat(),
            })
        episodes = [
            {
                "id": e.id,
                "event": e.event,
                "reason": REASON_LABELS.get(Reason(e.reason)) if e.reason else "就这个",
                "context": e.context.label,
                "counts": bool(e.signals),
                "created_at": e.created_at.isoformat(),
            }
            for e in reversed(state.memory.episodes[-50:])
        ]
        session = {
            d.value: b.value for d in Dimension if (b := state.session.get(d)) and b.total
        }
        return {
            "items": items,
            "proposals": [proposal_view(p) for p in state.proposals()],
            "episodes": episodes,
            "incognito": state.incognito,
            "session": session,
        }

    @app.post("/api/taste/proposals")
    def decide(body: Decision, uid=Depends(owner)):
        # Recomputed server-side: a client can only answer a proposal, never author one.
        state, data = load_taste(uid)
        proposal = next((p for p in state.proposals() if p.signature == body.signature), None)
        if proposal is None:
            raise HTTPException(404, "这条提议已经不成立了")
        if body.accept:
            state.confirm(proposal)
        else:
            state.decline(proposal)
        save_taste(uid, state, data["offered"])
        return {"accepted": body.accept, "action": ACTION_LABELS[proposal.action]}

    @app.post("/api/taste/notes", status_code=201)
    def add_note(note: Note, uid=Depends(owner)):
        state, data = load_taste(uid)
        (item,) = state.confirm(Proposal(action="add", key=note_key(note.text),
                                         source="user_stated", text=note.text.strip()))
        save_taste(uid, state, data["offered"])
        return {"id": item.id}

    @app.delete("/api/taste/items/{item_id}")
    def delete_item(item_id: str, uid=Depends(owner)):
        state, data = load_taste(uid)
        if not state.memory.forget(item_id):
            raise HTTPException(404, "未找到这条记忆")
        save_taste(uid, state, data["offered"])
        return {"deleted": True}

    @app.delete("/api/taste/episodes/{episode_id}")
    def delete_episode(episode_id: str, uid=Depends(owner)):
        state, data = load_taste(uid)
        if not state.memory.forget_episode(episode_id):
            raise HTTPException(404, "未找到这条经历")
        save_taste(uid, state, data["offered"])
        return {"deleted": True}

    @app.put("/api/taste/settings")
    def settings(body: Settings, uid=Depends(owner)):
        state, data = load_taste(uid)
        state.incognito = body.incognito
        save_taste(uid, state, data["offered"])
        return {"incognito": state.incognito}

    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app


app = create_app()
