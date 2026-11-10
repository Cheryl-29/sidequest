from sidequest.baseline import ask, describe, pool_for, summarise
from sidequest.llm import ModelTimeoutError
from sidequest.models import Request

WINDOW = {"departure": "2026-09-14T10:00:00+10:00", "deadline": "2026-09-14T14:00:00+10:00"}


class Canned:
    """A model that answers whatever it was built with; nothing checks it."""

    calls = prompt_tokens = completion_tokens = 0

    def __init__(self, **answer):
        self.answer = {"stops": [], "return_time": "", "fits": False,
                       "unverified_stop_ids": [], "summary": ""} | answer
        self.prompts = []

    def decide(self, name, system, user, spec):
        self.prompts.append(user)
        return self.answer


def request(**changes):
    return Request.model_validate(WINDOW | changes)


def test_a_workable_order_is_scored_by_the_product_validator():
    row = ask(Canned(stops=["park", "library"], return_time="12:30", fits=True),
              request(), "routes")
    assert row["status"] == "verified" and not row["late"] and row["failed"] == []
    # Back at 12:30 is later than the earliest this order allows: a choice, not an error.
    assert row["return_error"] > 10
    assert summarise([row | {"solvable": True}])["return_time_optimistic"] == 0


def test_a_vouched_plan_that_cannot_return_in_time_is_counted():
    model = Canned(stops=["gallery", "museum", "garden"], return_time="10:55", fits=True)
    row = ask(model, request(deadline="2026-09-14T11:00:00+10:00"), "routes")
    assert row["status"] == "infeasible" and "deadline" in row["failed"]
    assert row["late"] and row["late_unnoticed"] and row["vouched"]
    summary = summarise([row | {"solvable": True}])
    assert summary["infeasible_and_vouched"] == summary["late_unnoticed"] == 1
    assert summary["return_time_optimistic"] == 1


def test_withheld_hours_are_withheld_from_prompt_and_validator_alike():
    req = request()
    prompt = describe(req, pool_for(req, "routes_hidden"), "routes_hidden")
    garden = next(line for line in prompt.splitlines() if line.startswith("- garden｜"))
    assert "营业时间未知" in garden and "07:00" not in garden
    silent = ask(Canned(stops=["garden"], return_time="11:00", fits=True), req, "routes_hidden")
    assert silent["status"] == "conditional"  # unknown hours never verify
    assert silent["missing_facts"] == ["garden"] and silent["missing_flagged"] == []
    honest = ask(Canned(stops=["garden"], return_time="11:00", unverified_stop_ids=["garden"]),
                 req, "routes_hidden")
    summary = summarise([silent | {"solvable": True}, honest | {"solvable": True}])
    assert summary["plans_with_missing_facts"] == 2 and summary["missing_facts_all_flagged"] == 1


def test_the_plain_arm_shows_no_leg_durations():
    req = request()
    assert "↔" not in describe(req, pool_for(req, "plain"), "plain")
    assert "townhall ↔ park：步行" in describe(req, pool_for(req, "routes"), "routes")


def test_no_plan_and_model_errors_stay_out_of_the_plan_counts():
    class Broken(Canned):
        def decide(self, *a):
            raise ModelTimeoutError("超时")

    req = request(max_walk_minutes=0)
    rows = [ask(Canned(), req, "routes") | {"solvable": False},
            ask(Canned(stops=["park"], return_time="10:40", fits=True), req, "routes")
            | {"solvable": False},
            ask(Broken(), req, "routes") | {"solvable": False}]
    summary = summarise(rows)
    assert summary["answers"] == 2 and summary["errors"] == 1 and summary["plans"] == 1
    assert summary["unsolvable_scenarios"] == 2 and summary["planned_when_unsolvable"] == 1
