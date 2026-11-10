"""Unchecked-model baseline on the frozen scenarios (real model calls).

Asks the model for a plan in one shot and scores the answer with the product's own
validator, without letting anything check it first. See `sidequest/baseline.py` for the
arms and what the numbers mean. Writes reports/latest-baseline.json; not part of CI.

  PYTHONPATH=backend .venv/bin/python scripts/run_baseline.py --repeats 3
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from sidequest.baseline import ARMS, HIDDEN_HOURS, ask, summarise
from sidequest.llm import OpenAIModel
from sidequest.models import Request
from sidequest.planner import plan
from sidequest.reference import enumerate_feasible

ROOT = Path(__file__).resolve().parents[1]
BASE = {"departure": "2026-09-14T10:00:00+10:00", "deadline": "2026-09-14T14:00:00+10:00"}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--arms", default=",".join(ARMS))
    p.add_argument("--out", default=str(ROOT / "reports/latest-baseline.json"))
    args = p.parse_args()
    arms = [a for a in args.arms.split(",") if a in ARMS]

    cases = json.loads((ROOT / "evals/scenarios.json").read_text())
    requests = {c["id"]: Request.model_validate(BASE | c["request"]) for c in cases}
    solvable = {i: bool(enumerate_feasible(r).feasible) for i, r in requests.items()}

    # What the product returns for the same scenarios: the fixed planner, no model.
    ours = []
    for i, r in requests.items():
        result, _ = plan(r)
        best = result.itineraries[0] if result.itineraries else None
        ours.append({"id": i, "solvable": solvable[i], "planned": bool(best),
                     "status": best.status.value if best else None})

    models = []

    def one(job):
        arm, repeat, case_id = job
        model = OpenAIModel(limit=1, timeout=30.0)
        models.append(model)
        row = ask(model, requests[case_id], arm)
        return {"arm": arm, "repeat": repeat, "id": case_id, "solvable": solvable[case_id], **row}

    jobs = [(a, n, i) for a in arms for n in range(1, args.repeats + 1) for i in requests]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(one, jobs))

    if all("error" in r for r in rows):
        kinds = sorted({r["error"] for r in rows})
        raise SystemExit(f"全部 {len(rows)} 次模型调用失败（{', '.join(kinds)}），未写报告")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "configuration": {
            "model": models[0].model if models else None,
            "scenarios": len(requests),
            "repeats": args.repeats,
            "fixture": "synthetic-fixtures-v1",
            "hidden_hours": list(HIDDEN_HOURS),
            "model_calls": sum(m.calls for m in models),
            "tokens": sum(m.prompt_tokens + m.completion_tokens for m in models),
        },
        "sidequest_fixed": {
            "scenarios": len(ours),
            "plans": sum(r["planned"] for r in ours),
            "infeasible_plans": sum(r["status"] == "infeasible" for r in ours),
            "unsolvable_scenarios": sum(not r["solvable"] for r in ours),
            "planned_when_unsolvable": sum(r["planned"] for r in ours if not r["solvable"]),
        },
        "summary": {a: summarise([r for r in rows if r["arm"] == a]) for a in arms},
        "results": rows,
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"sidequest_fixed": report["sidequest_fixed"], **report["summary"]},
                     ensure_ascii=False, indent=1))
    for arm in arms:
        errors = report["summary"][arm]["errors"]
        if errors:
            print(f"WARNING {arm}: {errors} 次模型调用失败，未计入分母")


if __name__ == "__main__":
    main()
