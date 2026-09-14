"""Brute-force reference planner (plan v2 §10, M5′): the yardstick for `planner.plan`.

`plan()` is a fixed policy that tries at most six combinations. To say how much that
policy leaves on the table, this module tries every ordered combination of every
eligible candidate up to `max_stops`, through the same `assemble` + validator and the
same replay route source, with no call budget. It is slow on purpose and never used to
answer a user.

It shares the validator, so it cannot catch a validator bug; it measures search recall
only. Replay mode only: exhaustively querying TfNSW would be abuse of a live API.
"""

import itertools

from pydantic import BaseModel

from .models import Request, Status
from .planner import NoRouteError, ReplayTools, assemble, plan

MAX_ASSEMBLIES = 5000  # a guard against an accidentally huge catalog, not a search budget


class Reference(BaseModel):
    feasible: list[list[str]]  # every feasible ordered combination, as candidate ids
    verified: list[list[str]]
    assemblies: int
    truncated: bool = False

    @property
    def sets(self) -> set[frozenset[str]]:
        return {frozenset(ids) for ids in self.feasible}


def enumerate_feasible(request: Request) -> Reference:
    if request.mode != "replay":
        raise ValueError("暴力参考规划器只在回放模式下运行")
    tools = ReplayTools(request, limit=10**9)
    found = tools.retrieve()
    locked = [c for c in found if c.id in request.locked_ids]
    pool = [c for c in found
            if c.id not in request.excluded_ids and not c.cancelled and c not in locked]
    feasible, verified, count, truncated = [], [], 0, False
    for size in range(max(1, len(locked)), request.max_stops + 1):
        for extra in itertools.combinations(pool, size - len(locked)):
            for order in itertools.permutations([*locked, *extra]):
                if count >= MAX_ASSEMBLIES:
                    truncated = True
                    break
                count += 1
                try:
                    itinerary = assemble(request, order, tools)
                except NoRouteError:
                    continue
                if itinerary.status == Status.INFEASIBLE:
                    continue
                ids = [c.id for c in order]
                feasible.append(ids)
                if itinerary.status == Status.VERIFIED:
                    verified.append(ids)
    return Reference(feasible=feasible, verified=verified, assemblies=count, truncated=truncated)


def compare(request: Request) -> dict:
    """What the fixed policy found against what exists."""
    reference = enumerate_feasible(request)
    result, _ = plan(request)
    found = {frozenset(s.candidate.id for s in i.stops) for i in result.itineraries}
    best_size = max((len(ids) for ids in reference.feasible), default=0)
    return {
        "exists": bool(reference.feasible),
        "verified_exists": bool(reference.verified),
        "planner_found": bool(found),
        "planner_verified": any(i.status == Status.VERIFIED for i in result.itineraries),
        # Anything the planner returns must be in the reference; otherwise the two disagree
        # about feasibility and one of them is wrong.
        "unexplained": sorted(sorted(s) for s in found - reference.sets),
        "combinations": len(reference.sets),
        "planner_combinations": len(found),
        "reference_max_stops": best_size,
        "planner_max_stops": max((len(s) for s in found), default=0),
        "assemblies": reference.assemblies,
        "truncated": reference.truncated,
    }
