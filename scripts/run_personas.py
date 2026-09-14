"""Run the simulated-user experiments (plan v2.2 §8.2) and write reports/latest-personas.json.

  PYTHONPATH=backend .venv/bin/python scripts/run_personas.py --places <index.sqlite>
  ... --holdout            include the held-out personas (only for a frozen configuration)

Uses RuleModel, not an LLM: the numbers describe the memory and ranking machinery. Personas
that need an inactive dimension are skipped and listed as blocked. The report records the
index digest so a run can be tied to the snapshot it used.
"""

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_personas(path: Path):
    from sidequest.personas import Pattern, Persona

    data = json.loads(path.read_text())
    patterns = {k: Pattern.model_validate(v) for k, v in data["patterns"].items()}
    return [Persona.model_validate({**p, "patterns": [patterns[n] for n in p["patterns"]]})
            for p in data["personas"]]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--places", type=Path, required=True)
    p.add_argument("--holdout", action="store_true")
    p.add_argument("--out", type=Path, default=ROOT / "reports" / "latest-personas.json")
    p.add_argument("--model", choices=["rule", "openai"], default="rule",
                   help="openai: real model calls (costs money, needs OPENAI_API_KEY)")
    p.add_argument("--groups", help="comma-separated subset of groups")
    p.add_argument("--workers", type=int, default=1, help="personas run in parallel")
    p.add_argument("--repeat", type=int, default=1, help="repeat index recorded in the report")
    args = p.parse_args()
    os.environ["SIDEQUEST_PLACES_DB"] = str(args.places)

    from concurrent.futures import ThreadPoolExecutor

    from check_dimensions import active_dimensions, osm_pool
    from sidequest.harness import GROUPS, MAX_ROUNDS, Harness, RuleModel, summarise
    from sidequest.llm import OpenAIModel

    # Personas may only stratify on dimensions this very index passes (plan v2 §8.3).
    active = active_dimensions(osm_pool(args.places), "osm")
    personas = load_personas(ROOT / "evals" / "personas.json")
    blocked = {x.id: x.blocked_by(active) for x in personas if x.blocked_by(active)}
    chosen = [x for x in personas if x.id not in blocked and (args.holdout or not x.holdout)]
    groups = args.groups.split(",") if args.groups else GROUPS
    assert set(groups) <= set(GROUPS), f"unknown group in {groups}"
    if args.model == "openai":
        factory, model_name = (lambda seed: OpenAIModel(limit=8)), f"OpenAI {OpenAIModel(limit=1).model}"
    else:
        factory, model_name = RuleModel, "RuleModel（确定性规则，不是 LLM）"
    jobs = [(persona, group) for persona in chosen for group in groups]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        runs = list(pool.map(lambda job: Harness(job[0], job[1], factory).run(), jobs))
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "开发运行（非冻结实验）" if not args.holdout else "含保留集",
        "model": model_name,
        "repeat": args.repeat,
        "groups": groups,
        "places_index_sha256": hashlib.sha256(args.places.read_bytes()).hexdigest(),
        "active_dimensions": sorted(active),
        "max_rounds": MAX_ROUNDS,
        "personas": [x.id for x in chosen],
        "blocked": blocked,
        "excluded_holdout": [] if args.holdout else [x.id for x in personas
                                                     if x.holdout and x.id not in blocked],
        "summary": summarise(runs, {x.id: x for x in chosen}),
        "runs": [r.model_dump(mode="json") for r in runs],
    }
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for group, row in report["summary"].items():
        print(f"{group:17} acc@1 {row['accept@1']['rate']}  acc@3 {row['accept@3']['rate']}  "
              f"s3+首轮 {row['first_round_accept_session_3_plus']['rate']}  "
              f"情境误用 {row['other_context_misuse']['rate']}  提议精确 "
              f"{row['proposal_precision']['rate']}  召回 {row['memory_recall']['rate']}  "
              f"错记(全同意) {row['false_memory_all_agree']['rate']}")


if __name__ == "__main__":
    main()
