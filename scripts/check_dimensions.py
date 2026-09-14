"""Gate the frozen taste dimensions (plan v2 §4.3) against the real candidate pool.

A dimension is only usable if the candidate pool can actually separate on it.
Two dimensions that always agree -- or always disagree -- are one dimension
wearing two hats: probing either one gives the other for free, and no persona
can be built to tell them apart. This script fails CI when an active dimension
stops meeting that bar, and reports pending/reserved ones without failing.
"""

import argparse
import json
from pathlib import Path

from sidequest.fixtures import ORIGINS, ROWS
from sidequest.models import Request
from sidequest.planner import distance
from sidequest.providers import CuratedVenueProvider
from sidequest.taste import D3_TIERS, FAR_KM, Dimension, dimensions_of

ROOT = Path(__file__).resolve().parents[1]

MIN_COVERAGE = 0.80  # share of the pool on which the dimension has a value
MIN_MINORITY = 0.20  # share of the smaller side, among candidates that have a value
MAX_AGREEMENT = 0.80  # pairwise |agreement| ceiling; anti-correlation is just as degenerate
# FAR_KM / D3_TIERS and the pole definitions live in sidequest.taste -- one source of truth.


def pool():
    """Both candidate pools in one shape. Live venues have no obviousness field yet."""
    rows = [
        {"id": r[0], "source": "replay", "lat": r[4], "lon": r[5], "tags": set(r[6]),
         "cost": r[8], "obviousness": r[11]}
        for r in ROWS
    ] + [
        {
            "id": r["id"],
            "source": "live",
            "lat": r["lat"],
            "lon": r["lon"],
            "tags": set(r["tags"]),
            "cost": 0,
            "obviousness": r.get("obviousness"),
        }
        for r in CuratedVenueProvider.rows
    ]
    for row in rows:
        origins = [ORIGINS[key] for key in ("central", "townhall", "circular")]
        row["mean_km"] = sum(distance(o, row) for o in origins) / len(origins)
    return rows


def osm_pool(path: Path):
    """What discovery would actually hand the agent from each fixed origin (plan v2.1 §4.3.1).

    Distances are from the origin that retrieved the place, not a three-origin mean: with
    open-world retrieval, D2 only means something relative to where the user starts.
    """
    from sidequest.places import KIND_BY_CODE, PlaceIndex, radius_for, select

    index = PlaceIndex(path)
    rows = []
    for key in ("central", "townhall", "circular"):
        origin = ORIGINS[key]
        request = Request.model_validate({"origin_id": key, "catalog": "osm",
                                          "departure": "2026-09-14T10:00:00+10:00",
                                          "deadline": "2026-09-14T13:00:00+10:00"})
        radius = radius_for(request)
        for r in select(index.near(origin["lat"], origin["lon"], radius), radius):
            rows.append({"id": f"{key}:{r['name'][:14]}", "source": "osm", "lat": r["lat"],
                         "lon": r["lon"], "tags": set(KIND_BY_CODE[r["kind"]].tags), "cost": None,
                         "obviousness": r["obviousness"], "mean_km": r["km"],
                         "name": r["name"], "everyday": KIND_BY_CODE[r["kind"]].everyday})
    return rows


def axis(dimension):
    """Read one dimension off a pool row, via the canonical definition in sidequest.taste."""
    return lambda c: dimensions_of(c["tags"], c["mean_km"], c.get("obviousness"))[dimension]


def mood(c):
    """D4 mood, reserved until every candidate is annotated."""
    return 1 if "热闹" in c["tags"] else (0 if "安静" in c["tags"] else None)


# status: active (gates enforced) | pending (blocked, reported) | reserved (reported only).
# A status can depend on the pool: D2 is a property of retrieval around an origin, so the
# static CBD pool can never show it (plan v2 §4.3.1) while an OSM pool can (plan v2.2).
DIMENSIONS = [
    ("D1", "form", "室内静态 ↔ 户外走动", "active", "", axis(Dimension.FORM)),
    ("D2", "travel", "少折腾 ↔ 愿意走远",
     {"fixed": "pending", "osm": "active"},
     "static CBD pool has no geographic spread; enforced on OSM retrieval pools",
     axis(Dimension.TRAVEL)),
    ("D3", "obviousness", "大众答案 ↔ 冷门", "reserved",
     "retired as a global preference; research-only local reroll signal",
     axis(Dimension.OBVIOUSNESS)),
    ("D4", "mood", "安静 ↔ 热闹", "reserved", "needs full annotation", mood),
]
# Candidates a dimension deliberately says nothing about do not count against its coverage:
# D3 is silent on errands (plan v2.1 §4.3), which is a design choice, not missing data.
APPLIES = {"D3": lambda c: not c.get("everyday")}


def status_of(status, pool_kind):
    return status[pool_kind] if isinstance(status, dict) else status


def active_dimensions(candidates, pool_kind) -> set[str]:
    """Dimensions that are active for this pool AND pass its gates -- what personas may use."""
    return {code for code, problems, enforced in gate(candidates, pool_kind)
            if enforced and not problems}


def gate(candidates, pool_kind):
    columns = {code: [fn(c) for c in candidates] for code, *_, fn in DIMENSIONS}
    out = []
    for code, _, _, status, _, _ in DIMENSIONS:
        applies = APPLIES.get(code, lambda c: True)
        values = [v for v, c in zip(columns[code], candidates) if applies(c)]
        stats = measure(values, len(values))
        problems = []
        if stats["coverage"] < MIN_COVERAGE:
            problems.append(f"coverage {stats['coverage']:.2f} < {MIN_COVERAGE}")
        if stats["minority"] < MIN_MINORITY:
            problems.append(f"minority side {stats['minority']:.2f} < {MIN_MINORITY}")
        for other, _, _, other_status, _, _ in DIMENSIONS:
            if other == code or status_of(other_status, pool_kind) == "reserved":
                continue
            rate, overlap = agreement(columns[code], columns[other])
            if rate is not None and rate >= MAX_AGREEMENT:
                problems.append(f"{rate:.0%} identical to {other} over {overlap} candidates")
        out.append((code, problems, status_of(status, pool_kind) == "active"))
    return out


def measure(values, total):
    values = binarize(values)
    known = [v for v in values if v is not None]
    coverage = len(known) / total if total else 0.0
    minority = min(known.count(0), known.count(1)) / len(known) if known else 0.0
    return {"coverage": round(coverage, 3), "minority": round(minority, 3), "known": len(known)}


def binarize(values):
    """An ordinal column (D3 tiers) at whichever cut separates it best; binary ones as-is.
    Probing can act on either boundary, so separable at one cut is separable."""
    known = [v for v in values if v is not None]
    if not known or max(known) <= 1:
        return values
    cut = max(range(1, max(known) + 1),
              key=lambda c: min(sum(v >= c for v in known), sum(v < c for v in known)))
    return [None if v is None else int(v >= cut) for v in values]


def agreement(a, b):
    """max(agree, disagree) over candidates both dimensions cover. None when too few."""
    a, b = binarize(a), binarize(b)
    both = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if len(both) < 3:
        return None, len(both)
    same = sum(1 for x, y in both if x == y)
    return max(same, len(both) - same) / len(both), len(both)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--places", type=Path,
                        help="gate an OSM place index instead of the fixed pools (report-only)")
    args = parser.parse_args()
    candidates = osm_pool(args.places) if args.places else pool()
    pool_kind = "osm" if args.places else "fixed"
    total = len(candidates)
    columns = {code: [fn(c) for c in candidates] for code, *_, fn in DIMENSIONS}

    print(f"candidate pool: {total} "
          f"({sum(c['source'] == 'replay' for c in candidates)} replay, "
          f"{sum(c['source'] == 'live' for c in candidates)} live, "
          f"{sum(c['source'] == 'osm' for c in candidates)} osm)")
    print(f"{'id':24} {'km':>5} " + " ".join(f"{code:>4}" for code, *_ in DIMENSIONS))
    for i, c in enumerate(candidates):
        cells = " ".join(f"{str(columns[code][i]):>4}" for code, *_ in DIMENSIONS)
        print(f"{c['id']:24} {c['mean_km']:5.2f} {cells}")

    failures, report = [], {}
    print()
    verdicts = {code: (problems, enforced) for code, problems, enforced in gate(candidates, pool_kind)}
    for code, name, poles, status, blocker, _ in DIMENSIONS:
        status = status_of(status, pool_kind)
        applies = APPLIES.get(code, lambda c: True)
        applicable = [v for v, c in zip(columns[code], candidates) if applies(c)]
        stats = measure(applicable, len(applicable))
        report[code] = {"name": name, "poles": poles, "status": status, "blocker": blocker,
                        **stats, "applicable": len(applicable), "agreement": {}}
        for other, _, _, other_status, _, _ in DIMENSIONS:
            if other == code or status_of(other_status, pool_kind) == "reserved":
                continue
            rate, overlap = agreement(columns[code], columns[other])
            if rate is not None:
                report[code]["agreement"][other] = {"rate": round(rate, 3), "overlap": overlap}
        problems, enforced = verdicts[code]
        mark = "FAIL" if (problems and enforced) else ("----" if problems else "ok  ")
        note = "; ".join(problems) if problems else "separable"
        suffix = "" if enforced else f" [{status}: {blocker}]"
        if len(applicable) != total:
            suffix += f" (over {len(applicable)} candidates it applies to)"
        print(f"{mark} {code} {name:12} coverage {stats['coverage']:.0%} "
              f"minority {stats['minority']:.0%} · {note}{suffix}")
        if problems and enforced:
            failures.append(code)

    # CI gates the fixed pools; an OSM run writes its own report and never replaces that one.
    output = ROOT / ("reports/latest-dimensions-osm.json" if args.places
                     else "reports/latest-dimensions.json")
    output.write_text(
        json.dumps(
            {
                "pool_size": total,
                "pool": str(args.places) if args.places else "fixed",
                "thresholds": {
                    "min_coverage": MIN_COVERAGE,
                    "min_minority": MIN_MINORITY,
                    "max_agreement": MAX_AGREEMENT,
                    "far_km": FAR_KM,
                    "d3_tiers": list(D3_TIERS),
                },
                "dimensions": report,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    pending = [(c, b) for c, _, _, s, b, _ in DIMENSIONS if status_of(s, pool_kind) == "pending"]
    active = sorted(active_dimensions(candidates, pool_kind))
    if pending:
        print("\npending -- gates start enforcing once the blocker is cleared:")
        for code, blocker in pending:
            print(f"  {code}: {blocker}")
    print(f"\n{len(active)} active and passing dimension(s): {', '.join(active) or 'none'}. "
          "Persona design (plan v2 §8.3) may only stratify on these.")
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
