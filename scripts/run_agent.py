"""Drive the agent loop from the terminal, with real model calls.

Needs OPENAI_API_KEY in .env (OPENAI_MODEL and OPENAI_BASE_URL optional). Every reroll
feeds a typed reason back into the TasteState, so a run with --reroll shows whether the
next suggestion actually moved in that direction.

  PYTHONPATH=backend .venv/bin/python scripts/run_agent.py \
      --say "下午空出来了，不想待在家" --hours 3 --reroll too_obvious --reroll want_move

  # real places around a chosen origin, from the local OSM index
  PYTHONPATH=backend .venv/bin/python scripts/run_agent.py \
      --catalog osm --at -33.8886,151.1873 --say "想走走" --hours 2
"""

import argparse
from datetime import datetime, timedelta

from sidequest.agent import propose_quest
from sidequest.llm import ModelError, OpenAIModel
from sidequest.models import SYDNEY, Request
from sidequest.places import PlaceIndexMissing, candidate_kind
from sidequest.taste import Dimension, Feedback, Reason, TasteState

BAR = "─" * 68


def window(hours: float) -> tuple[datetime, datetime]:
    now = datetime.now(SYDNEY).replace(second=0, microsecond=0)
    deadline = now + timedelta(hours=hours)
    if deadline.date() != now.date():  # no cross-midnight windows; use tomorrow morning
        now = (now + timedelta(days=1)).replace(hour=10, minute=0)
        deadline = now + timedelta(hours=hours)
    return now, deadline


def show(result, state):
    print(BAR)
    if not result.quest:
        print(f"没有可用方案：{result.message}")
    else:
        q = result.quest
        anchor = next(s for s in q.itinerary.stops if s.candidate.id == q.anchor_id)
        print(f"【{q.brief}】\n{q.hook}\n")
        aim = f"{q.probe.dimension} → {q.probe.pole}" if q.probe.dimension else "不试探"
        print(f"押注 {aim}（{q.probe.mode}）：{q.probe.summary}")
        print(f"下注的候选：{anchor.candidate.name}")
        for i, stop in enumerate(q.itinerary.stops, 1):
            print(f"  {i}. {stop.start:%H:%M}–{stop.end:%H:%M}  {stop.candidate.name}")
        print(f"  ↩ {q.itinerary.return_at:%H:%M} 返回 · {q.itinerary.status} · "
              f"步行 {q.itinerary.walking_minutes} 分钟 · 已知费用 AUD {q.itinerary.known_cost:g}")
        if q.itinerary.unknowns:
            print(f"  未知项：{'；'.join(q.itinerary.unknowns[:3])}")
        if q.itinerary.advisories:
            print(f"  未核实（不影响判定）：{'；'.join(q.itinerary.advisories[:3])}")
        print(f"  引用证据 {len(q.evidence_ids)} 条")
    for step in result.trace:
        print(f"  · {step.action}: {step.summary}")
    beliefs = {
        d.value: (b.value, b.total) for d in Dimension if (b := state.effective(d)) and b.total
    }
    print(f"  口味状态：{beliefs or '尚无观测'}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--say", default="", help="用户的一句话")
    p.add_argument("--hours", type=float, default=3.0)
    p.add_argument("--mode", choices=["replay", "live"], default="replay")
    p.add_argument("--reroll", action="append", default=[],
                   choices=[r.value for r in Reason], help="可重复；每次换一个并带理由")
    p.add_argument("--limit", type=int, default=12, help="模型决策预算")
    p.add_argument("--catalog", choices=["fixed", "osm"], default="fixed",
                   help="osm：从本地 OSM 索引围绕出发点发现地点（先运行 build_places.py）")
    p.add_argument("--at", metavar="LAT,LON", help="出发点坐标（悉尼范围内），默认 Town Hall")
    p.add_argument("--strict-hours", action="store_true",
                   help="缺营业时间时降为 conditional（默认只作提示，判定只看时间预算）")
    args = p.parse_args()

    departure, deadline = window(args.hours)
    origin = {}
    if args.at:
        lat, lon = (float(v) for v in args.at.split(","))
        origin = {"origin_id": "current", "origin_lat": lat, "origin_lon": lon}
    request = Request.model_validate(
        {"departure": departure, "deadline": deadline, "mode": args.mode, **origin,
         "catalog": args.catalog, "venue_facts": "verdict" if args.strict_hours else "advisory"}
    )
    print(f"悉尼时间 {departure:%m-%d %H:%M}–{deadline:%H:%M} · {args.mode} · {args.catalog} · "
          f"“{args.say or '（没说什么）'}”")

    try:
        model = OpenAIModel(limit=args.limit)
    except ModelError as exc:
        raise SystemExit(f"模型未就绪：{exc}")
    print(f"模型 {model.model}")

    state = TasteState()
    try:
        result = propose_quest(request, state, model, said=args.say)
        show(result, state)
        intent = result.quest.intent if result.quest else None
        for reason in args.reroll:
            q = result.quest
            stops = {s.candidate.id: s.candidate for s in q.itinerary.stops} if q else {}
            outcome = state.apply(
                Feedback(quest_id=q.itinerary.id if q else "-", candidate_ids=list(stops)[:3],
                         anchor_id=q.anchor_id if q else None,
                         kind=candidate_kind(stops[q.anchor_id]) if q else None,
                         reason=Reason(reason))
            )
            if outcome.stated:  # the chip is the user's own statement; confirm it as such
                state.confirm(outcome.stated)
            print(f"\n换一个 · 理由 {reason} → "
                  f"{'维度 ' + outcome.dimension if outcome.dimension else '不改口味'}"
                  f"{'，本会话避开 ' + outcome.kind if outcome.kind else ''}"
                  f"{'，已记住：' + outcome.stated.text if outcome.stated else ''}"
                  f"{'，需澄清 ' + outcome.clarify if outcome.clarify else ''}"
                  f"{'，改约束 ' + str(outcome.request_patch) if outcome.request_patch else ''}")
            if outcome.request_patch:
                request = request.model_copy(update=outcome.request_patch)
            result = propose_quest(request, state, model, intent=intent)
            show(result, state)
        for proposal in state.proposals():
            print(f"\n提议：{proposal.text}（依据 {len(proposal.episode_ids)} 次经历，等你确认）")
    except ModelError as exc:
        raise SystemExit(f"\n模型调用失败：{exc}")
    except PlaceIndexMissing as exc:
        raise SystemExit(f"\n{exc}")
    finally:
        print(BAR)
        print(f"模型调用 {model.calls}/{model.limit} 次 · "
              f"token {model.prompt_tokens}+{model.completion_tokens}")


if __name__ == "__main__":
    main()
