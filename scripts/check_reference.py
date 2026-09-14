"""Measure the fixed planner's search recall against the brute-force reference (M5′).

Runs every frozen scenario in evals/scenarios.json through both and writes
reports/latest-reference.json. Exits non-zero only if the two disagree about feasibility
(the planner returned a combination the reference rejects) -- low recall is a finding to
report, not a build failure.

  PYTHONPATH=backend .venv/bin/python scripts/check_reference.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from sidequest.models import Request
from sidequest.reference import compare

ROOT = Path(__file__).resolve().parents[1]
BASE = {"departure": "2026-09-14T10:00:00+10:00", "deadline": "2026-09-14T14:00:00+10:00"}


def main():
    cases = json.loads((ROOT / "evals" / "scenarios.json").read_text())
    rows = []
    for case in cases:
        request = Request.model_validate(BASE | case["request"])
        if request.mode != "replay":
            continue
        rows.append({"id": case["id"], "split": case["split"], **compare(request)})
    exists = [r for r in rows if r["exists"]]
    verified = [r for r in rows if r["verified_exists"]]
    summary = {
        "scenarios": len(rows),
        "feasible_exists": len(exists),
        "planner_hit": sum(r["planner_found"] for r in exists),
        "verified_exists": len(verified),
        "planner_verified_hit": sum(r["planner_verified"] for r in verified),
        "shorter_than_possible": sum(r["planner_max_stops"] < r["reference_max_stops"]
                                     for r in exists if r["planner_found"]),
        "false_negatives": [r["id"] for r in exists if not r["planner_found"]],
        "disagreements": [r["id"] for r in rows if r["unexplained"]],
        "truncated": [r["id"] for r in rows if r["truncated"]],
    }
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "summary": summary,
              "cases": rows}
    out = ROOT / "reports" / "latest-reference.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(1 if summary["disagreements"] else 0)


if __name__ == "__main__":
    main()
