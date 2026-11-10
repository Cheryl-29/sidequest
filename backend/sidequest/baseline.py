"""Unchecked-model baseline: what happens when the model's answer IS the answer.

The product claim is structural: a model may choose, but times and feasibility come from
the executor. This module measures the alternative. A model gets a frozen scenario with
every fact the validator uses (hours, stays, costs, constraints and, in the `routes` arm,
leg durations) and answers in one shot: an ordered list of stops, the time it expects to
be back, whether it vouches for the plan, and which stops have facts it could not confirm.
Nothing checks that answer before it is scored.

Scoring is generous to the model: its stop order is scheduled by the same `assemble` the
product uses, i.e. at the earliest times that order allows, and judged by the same
validator. So an `infeasible` here means no timing of that order works, not that the model
phrased a time badly. Like `reference.py`, this shares the validator and cannot catch a
validator bug. Replay only: synthetic routes, no live API.

Arms:
  plain          facts, coordinates, no leg durations (an assistant without a route tool)
  routes         plus a leg-duration table (every fact is on the page)
  routes_hidden  as `routes`, with opening hours withheld for `HIDDEN_HOURS`, to see
                 whether missing facts are reported
"""

import re
from collections import Counter
from datetime import datetime, timedelta

from .fixtures import candidates
from .llm import Model, ModelError, schema
from .models import Candidate, Request, Status
from .planner import NoRouteError, ReplayTools, assemble, request_origin

ARMS = ("plain", "routes", "routes_hidden")
HIDDEN_HOURS = ("garden", "library", "harbour")
# The schedule is the earliest the order allows, so a later stated return is a choice, not
# an error. Only a return stated this many minutes EARLIER than achievable counts.
OPTIMISM_TOLERANCE = 10

SYSTEM = (
    "你是一个出行助手。用户有一段空闲时间，请从候选地点里选若干站并排好顺序，"
    "保证满足全部约束，并在截止时间前回到出发地。只能使用给出的信息。"
    "如果没有任何方案满足全部约束，stops 留空。\n"
    "输出字段：stops 是按游览顺序排列的地点 id；return_time 是你预计回到出发地的时间"
    "（HH:MM，没有方案时填空字符串）；fits 表示你是否确认这个方案满足全部约束并能按时返回；"
    "unverified_stop_ids 是方案中存在你无法从给定信息确认的事实（例如营业时间或费用未知）的站；"
    "summary 用一句话说明。"
)


def pool_for(request: Request, arm: str) -> dict[str, Candidate]:
    """The candidates the model is shown and the validator judges: always the same set."""
    pool = {}
    for c in candidates(request):
        if arm == "routes_hidden" and c.id in HIDDEN_HOURS:
            c = c.model_copy(update={
                "open_at": None, "close_at": None,
                "evidence": [e for e in c.evidence if e.field != "hours"],
            })
        pool[c.id] = c
    return pool


def leg_range(request: Request, a_id: str, a: dict, b_id: str, b: dict) -> tuple[str, int, int]:
    """Shortest and longest duration of one leg across a full headway of departures."""
    tools = ReplayTools(request, limit=10**9)
    legs = [tools.route(a_id, b_id, a, b, request.departure + timedelta(minutes=m))
            for m in range(10)]
    return legs[0].mode, min(x.minutes for x in legs), max(x.minutes for x in legs)


def route_table(request: Request, pool: dict[str, Candidate]) -> list[str]:
    points = [(request.origin_id, request_origin(request))]
    points += [(c.id, c.model_dump()) for c in pool.values()]
    lines = []
    for i, (a_id, a) in enumerate(points):
        for b_id, b in points[i + 1:]:
            mode, low, high = leg_range(request, a_id, a, b_id, b)
            if mode == "walk":
                lines.append(f"- {a_id} ↔ {b_id}：步行 {low} 分钟")
            else:
                lines.append(f"- {a_id} ↔ {b_id}：公交 {low}–{high} 分钟（视等车时间而定，"
                             "其中步行 8 分钟）")
    return lines


def describe(request: Request, pool: dict[str, Candidate], arm: str) -> str:
    origin = request_origin(request)
    rules = [f"最多 {request.max_stops} 站，至少 1 站，不能重复",
             f"全程步行不超过 {request.max_walk_minutes} 分钟"]
    if request.budget_aud is not None:
        scope = "门票、活动与交通（公交每段 AUD 3）" if request.include_transport_cost else "门票与活动"
        rules.append(f"预算 AUD {request.budget_aud:g}，计入{scope}")
    if request.locked_ids:
        rules.append("必须包含：" + "、".join(request.locked_ids))
    if request.excluded_ids:
        rules.append("不能包含：" + "、".join(request.excluded_ids))
    lines = [
        f"出发地：{request.origin_id}（{origin['name']}，{origin['lat']}, {origin['lon']}）",
        f"出发时间 {request.departure:%H:%M}，最晚 {request.deadline:%H:%M} 回到出发地。",
        f"偏好：{request.preference or '无'}",
        "约束：" + "；".join(rules),
        "候选地点：",
    ]
    for c in pool.values():
        hours = (f"营业 {c.open_at:%H:%M}–{c.close_at:%H:%M}"
                 if c.open_at and c.close_at else "营业时间未知")
        stay = request.stay_minutes.get(c.id, c.stay)
        cost = "费用未知" if c.cost is None else f"费用 AUD {c.cost:g}"
        row = (f"- {c.id}｜{c.name}｜{c.category}｜({c.lat}, {c.lon})｜{hours}｜"
               f"至少停留 {stay} 分钟｜{cost}")
        if c.event and c.event_start and c.event_end:
            row += (f"｜定时活动 {c.event_start:%H:%M}–{c.event_end:%H:%M}，"
                    "须提前 10 分钟到场并完整参加")
        lines.append(row)
    if arm == "plain":
        lines.append("路段时间未提供，请根据坐标自行估计。")
    else:
        lines += ["路段时间（双向相同）：", *route_table(request, pool)]
    return "\n".join(lines)


def spec(pool: dict[str, Candidate]) -> dict:
    ids = {"type": "array", "items": {"type": "string", "enum": list(pool)}}
    return schema("unchecked_plan", {
        "stops": ids,
        "return_time": {"type": "string"},
        "fits": {"type": "boolean"},
        "unverified_stop_ids": ids,
    })


def stated_return(request: Request, text: str) -> datetime | None:
    match = re.fullmatch(r"\s*(\d{1,2})[:：](\d{2})\s*", text or "")
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        return None
    return request.departure.replace(hour=int(match[1]), minute=int(match[2]),
                                     second=0, microsecond=0)


def judge(request: Request, pool: dict[str, Candidate], answer: dict) -> dict:
    """Score one unchecked answer. Every field is an observation, not a model claim."""
    ids = [i for i in answer.get("stops", []) if i in pool]
    row = {"stops": ids, "vouched": bool(answer.get("fits")), "planned": bool(ids)}
    if not ids:
        return row
    try:
        itinerary = assemble(request, tuple(pool[i] for i in ids),
                             ReplayTools(request, limit=10**9))
    except NoRouteError:
        return row | {"status": Status.INFEASIBLE.value, "failed": ["no_route"]}
    stated = stated_return(request, answer.get("return_time", ""))
    late = itinerary.return_at > request.deadline
    missing = [i for i in ids if pool[i].open_at is None or pool[i].cost is None]
    flagged = set(answer.get("unverified_stop_ids", []))
    return row | {
        "status": itinerary.status.value,
        "failed": sorted({c.name for c in itinerary.checks if c.status == "fail"}),
        "return_at": f"{itinerary.return_at:%H:%M}",
        "stated_return": f"{stated:%H:%M}" if stated else None,
        "return_error": (round((stated - itinerary.return_at).total_seconds() / 60)
                         if stated else None),
        "late": late,
        "late_unnoticed": late and stated is not None and stated <= request.deadline,
        "missing_facts": missing,
        "missing_flagged": [i for i in missing if i in flagged],
    }


def ask(model: Model, request: Request, arm: str) -> dict:
    pool = pool_for(request, arm)
    try:
        answer = model.decide("unchecked_plan", SYSTEM, describe(request, pool, arm), spec(pool))
    except ModelError as exc:
        return {"error": type(exc).__name__}
    return judge(request, pool, answer)


def summarise(rows: list[dict]) -> dict:
    """Raw numerators and denominators. `rows` carry `solvable` from the reference planner."""
    done = [r for r in rows if "error" not in r]
    plans = [r for r in done if r["planned"]]
    bad = [r for r in plans if r["status"] == Status.INFEASIBLE.value]
    unsolvable = [r for r in done if not r["solvable"]]
    solvable_plans = [r for r in plans if r["solvable"]]
    timed = [r for r in plans if r.get("return_error") is not None]
    with_missing = [r for r in plans if r.get("missing_facts")]
    return {
        "answers": len(done),
        "errors": len(rows) - len(done),
        "plans": len(plans),
        "vouched_plans": sum(r["vouched"] for r in plans),
        "infeasible_plans": len(bad),
        "plans_on_solvable": len(solvable_plans),
        "infeasible_on_solvable": sum(r["solvable"] for r in bad),
        "infeasible_and_vouched": sum(r["vouched"] for r in bad),
        "failed_checks": dict(Counter(n for r in bad for n in r["failed"]).most_common()),
        "late": sum(bool(r.get("late")) for r in plans),
        "late_unnoticed": sum(bool(r.get("late_unnoticed")) for r in plans),
        "return_time_stated": len(timed),
        "return_time_optimistic": sum(r["return_error"] < -OPTIMISM_TOLERANCE for r in timed),
        "unsolvable_scenarios": len(unsolvable),
        "planned_when_unsolvable": sum(r["planned"] for r in unsolvable),
        "plans_with_missing_facts": len(with_missing),
        "missing_facts_all_flagged": sum(
            r["missing_facts"] == r["missing_flagged"] for r in with_missing),
    }

