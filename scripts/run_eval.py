"""Run the frozen deterministic acceptance scenarios.

The expectations are intentionally outside the production validator. They check
observable outcomes rather than calling validator internals as the sole judge.
"""

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from sidequest.models import Request
from sidequest.planner import plan

ROOT = Path(__file__).resolve().parents[1]
BASE = {
    "departure": "2026-09-14T10:00:00+10:00",
    "deadline": "2026-09-14T14:00:00+10:00",
}


def score(case):
    req = Request.model_validate(BASE | case["request"])
    result, _ = plan(req)
    expected = case["expect"]
    errors = []
    if result.status not in expected["run"]:
        errors.append(f"run={result.status}, expected {expected['run']}")
    plans = result.itineraries
    if expected.get("no_itinerary") and plans:
        errors.append("expected no itinerary")
    if plans:
        best = plans[0]
        ids = {stop.candidate.id for stop in best.stops}
        if len(best.legs) != len(best.stops) + 1:
            errors.append("full outbound/inter-stop/return chain missing")
        if best.return_at > req.deadline:
            errors.append("return after deadline")
        if len(best.stops) < expected.get("min_stops", 0):
            errors.append("too few stops")
        if len(best.stops) > expected.get("max_stops", 3):
            errors.append("too many stops")
        if not set(expected.get("required", [])) <= ids:
            errors.append("required stop missing")
        if set(expected.get("forbidden", [])) & ids:
            errors.append("forbidden stop present")
        if best.status not in expected.get("itinerary", ["verified", "conditional"]):
            errors.append(f"itinerary={best.status}")
        if any(check.status == "fail" for check in best.checks):
            errors.append("returned itinerary contains failed check")
    if result.tool_calls > 20:
        errors.append("tool budget exceeded")
    return {
        "id": case["id"],
        "split": case["split"],
        "passed": not errors,
        "errors": errors,
        "run_status": result.status,
        "itinerary_status": plans[0].status if plans else None,
        "stops": len(plans[0].stops) if plans else 0,
        "tool_calls": result.tool_calls,
    }


def main():
    cases = json.loads((ROOT / "evals/scenarios.json").read_text())
    rows = [score(case) for case in cases]
    totals = Counter((row["split"], row["passed"]) for row in rows)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "configuration": {
            "strategy": "fixed-v1",
            "fixture": "synthetic-fixtures-v1",
            "tool_budget": 20,
        },
        "summary": {
            split: {
                "passed": totals[(split, True)],
                "total": sum(1 for row in rows if row["split"] == split),
            }
            for split in ("dev", "holdout")
        },
        "results": rows,
    }
    output = ROOT / "reports/latest-eval.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["summary"], ensure_ascii=False))
    failures = [row for row in rows if not row["passed"]]
    for row in failures:
        print(f"FAIL {row['id']}: {'; '.join(row['errors'])}")
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
