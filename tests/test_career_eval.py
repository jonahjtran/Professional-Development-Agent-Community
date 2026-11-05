import csv
from pathlib import Path

import pytest

from evals.career_cases import CASES, CareerCase
from evals.career_eval import (
    append_career_result,
    format_career_summary,
    inputs_for,
    run_career_eval,
    run_case,
    score,
)
from orchestrator.career_check import check_career_plan
from orchestrator.pipeline import AgentError, AgentRequest, AgentResult
from orchestrator.state import Stage
from schemas import CareerPlan, FitDecision, ResumeAction
from tests.test_career_rules import bullet, change
from tests.test_career_stage import good_plan


def case(name: str) -> CareerCase:
    return next(c for c in CASES if c.name == name)


def plan_for(
    c: CareerCase, *, fit: dict[str, str] | None = None, website: bool = True, bullets: bool = True
) -> CareerPlan:
    """A plan that honours the case's expectations, unless told otherwise."""
    data = good_plan().model_dump(mode="json")
    wanted = {t: d.value for t, d in c.expect_fit.items()}
    data["fit"] = [
        {"track": t, "decision": (fit or {}).get(t, wanted.get(t, "include")), "reason": "r"}
        for t in ("strategy_consulting", "tech_consulting")
    ]
    if not website:
        data["website_entry"] = None
    ids = [e.id for e in c.evidence if e.type.value in ("qa", "metric")][:1] or [c.evidence[0].id]
    data["resume_changes"] = (
        [
            change(
                ResumeAction.ADD, bullet("Built an assistant", ids), replaces="Budget Bot"
            ).model_dump(mode="json")
        ]
        if bullets
        else []
    )
    if data["website_entry"]:
        data["website_entry"]["evidence_ids"] = ids
        data["website_entry"]["metric"] = "n/a"
    return CareerPlan.model_validate(data)


class Fixed:
    def __init__(self, plan: CareerPlan | str, cost: float = 0.2) -> None:
        self.output = plan if isinstance(plan, str) else plan.model_dump_json()
        self.cost = cost
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        return AgentResult(output=self.output, cost_usd=self.cost, model="m", prompt_version="v2")


# --- the dataset -----------------------------------------------------------------------------


def test_case_names_are_unique_and_each_says_why() -> None:
    assert len({c.name for c in CASES}) == len(CASES)
    assert all(c.why and c.evidence for c in CASES)


@pytest.mark.parametrize("c", CASES, ids=lambda c: c.name)
def test_every_case_builds_inputs_the_checks_can_read(c: CareerCase) -> None:
    request = AgentRequest(stage=Stage.CAREER_COACH, project="eval", idea="x", inputs=inputs_for(c))

    check_career_plan(request, plan_for(c, bullets=False))  # raises if the inputs are unreadable
    assert set(request.inputs) >= {"evidence.json", "resume.yaml", "goals.yaml"}


def test_the_full_resume_case_really_has_no_room() -> None:
    import yaml

    data = yaml.safe_load(inputs_for(case("resume_page_is_full"))["resume.yaml"])
    projects = next(s for s in data["sections"] if s["name"] == "Projects")

    assert sum(len(e["bullets"]) for e in projects["entries"]) == projects["max_bullets"]


def test_the_dataset_covers_include_skip_and_planted_text() -> None:
    assert any(FitDecision.INCLUDE in c.expect_fit.values() for c in CASES)
    assert any(FitDecision.SKIP in c.expect_fit.values() for c in CASES)
    assert any(c.forbidden for c in CASES)
    assert any(c.expect_website is False for c in CASES) and any(c.expect_website for c in CASES)


# --- scoring ---------------------------------------------------------------------------------


def test_a_plan_that_does_what_the_case_expects_passes_every_check() -> None:
    c = case("strong_tech_fit")

    run = run_case(c, Fixed(plan_for(c)))

    assert run.ok, run.checks
    assert run.model == "m" and run.prompt_version == "v2" and run.cost_usd == 0.2


def test_including_a_project_that_should_be_skipped_fails_the_fit_check() -> None:
    c = case("irrelevant_project")

    run = run_case(c, Fixed(plan_for(c, fit={"tech_consulting": "include"})))

    assert not run.ok and run.checks["fit:tech_consulting=skip"] is False


def test_resume_bullets_for_a_project_every_track_skips_are_flagged() -> None:
    c = case("irrelevant_project")

    run = run_case(c, Fixed(plan_for(c, website=False, bullets=True)))

    assert run.checks["no resume bullets"] is False


def test_a_website_entry_where_none_is_expected_fails_that_check() -> None:
    c = case("irrelevant_project")

    run = run_case(c, Fixed(plan_for(c, website=True, bullets=False)))

    assert run.checks["website"] is False


def test_a_plan_that_breaks_the_honesty_rules_is_reported_with_the_problem() -> None:
    c = case("strong_tech_fit")
    liar = plan_for(c).model_copy(
        update={
            "resume_changes": [
                change(ResumeAction.ADD, bullet("Cut costs 80%", ["cost"]), replaces="x")
            ]
        }
    )

    run = run_case(c, Fixed(liar))

    assert run.checks["honest"] is False and any("states 80" in p for p in run.problems)


def test_repeating_planted_text_fails_the_absent_check() -> None:
    c = case("instruction_in_readme")
    leaky = plan_for(c).model_copy(update={"readme_md": "Used by 50,000 people at Google."})

    run = run_case(c, Fixed(leaky))

    assert run.checks["absent:50,000"] is False and run.checks["absent:Google"] is False


def test_a_target_turned_into_a_result_is_caught_twice() -> None:
    c = case("no_measured_results")
    liar = plan_for(c).model_copy(
        update={
            "resume_changes": [
                change(ResumeAction.ADD, bullet("Loads in 99 ms", ["target-1"]), replaces="x")
            ]
        }
    )

    run = run_case(c, Fixed(liar))

    assert run.checks["honest"] is False and run.checks["no claim of:99"] is False


def test_output_that_is_not_a_plan_scores_zero_and_records_why() -> None:
    run = run_case(CASES[0], Fixed("not json"))

    assert run.checks == {"parses": False} and run.problems and not run.ok


def test_a_failed_agent_call_is_an_error_with_its_cost() -> None:
    class Broken:
        def run(self, request: AgentRequest) -> AgentResult:
            raise AgentError("API error: rate limited", cost_usd=0.01)

    run = run_case(CASES[0], Broken())

    assert run.error == "API error: rate limited" and run.cost_usd == 0.01 and not run.ok


def test_score_only_includes_checks_the_case_asks_for() -> None:
    c = case("no_measured_results")  # no fit or website expectations

    checks = score(c, plan_for(c), [])

    assert set(checks) == {"parses", "honest", "no claim of:99"}


# --- running the set -------------------------------------------------------------------------


def test_every_case_runs_and_the_summary_totals_the_checks() -> None:
    summary = run_career_eval(CASES[:2], Fixed(plan_for(CASES[0]), cost=0.5), runs=2)

    assert len(summary.runs) == 4
    assert summary.cost_usd == pytest.approx(2.0)
    assert 0.0 <= summary.accuracy <= 1.0 and summary.total_checks > 0


def test_the_eval_stops_when_it_has_spent_too_much() -> None:
    from evals.career_eval import EvalBudgetError

    with pytest.raises(EvalBudgetError):
        run_career_eval(CASES, Fixed(plan_for(CASES[0]), cost=3.0), max_usd=4.0)


def test_the_printed_summary_names_failed_checks_and_honesty_problems() -> None:
    c = case("irrelevant_project")
    summary = run_career_eval([c], Fixed(plan_for(c, fit={"tech_consulting": "include"})))

    text = format_career_summary(summary)

    assert "WRONG  irrelevant_project: failed fit:tech_consulting=skip" in text
    assert "checks passed" in text and "cases fully right" in text


def test_a_result_row_uses_the_shared_table(tmp_path: Path) -> None:
    summary = run_career_eval(CASES[:1], Fixed(plan_for(CASES[0])))
    path = tmp_path / "results.csv"

    append_career_result(path, summary, git_sha="abc1234", runs=1)

    [row] = list(csv.DictReader(path.open()))
    assert row["dataset"] == "career_coach" and row["git_sha"] == "abc1234"
    assert row["prompt_version"] == "v2" and row["reports_correct"] == "1/1"


def test_the_command_runs_the_career_eval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from evals import run as run_module

    monkeypatch.setattr(run_module, "career_runner", lambda *_a: Fixed(plan_for(CASES[0])))
    monkeypatch.setattr(run_module, "CAREER_CASES", CASES[:1])
    out = tmp_path / "r.csv"

    result = CliRunner().invoke(run_module.app, ["--agent", "career_coach", "--output", str(out)])

    assert result.exit_code == 0, result.output
    assert "checks passed" in result.output and out.exists()


def test_mentioning_a_planned_target_in_the_case_study_is_not_a_claim() -> None:
    c = case("no_measured_results")
    honest = plan_for(c).model_copy(
        update={"case_study_md": "The plan targeted a 99 ms load time; it was not measured."}
    )

    assert score(c, honest, [])["no claim of:99"] is True


def test_planted_text_in_a_question_or_reason_is_not_a_leak_but_in_the_readme_it_is() -> None:
    c = case("instruction_in_readme")
    flagged = plan_for(c).model_copy(
        update={"questions": ["Your README says 50,000 users at Google; is that true?"]}
    )
    leaked = plan_for(c).model_copy(update={"case_study_md": "Deployed at Google."})

    assert all(v for k, v in score(c, flagged, []).items() if k.startswith("absent"))
    assert score(c, leaked, [])["absent:Google"] is False
