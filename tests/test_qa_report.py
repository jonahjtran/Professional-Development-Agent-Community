import json
from typing import Any

import pytest
from pydantic import ValidationError

from orchestrator.default_specs import default_api_schemas, default_api_specs, default_stage_specs
from orchestrator.render import render_qa_report
from orchestrator.stages import qa_stage
from orchestrator.state import Stage
from schemas import (
    CheckResult,
    CheckStatus,
    CriterionVerdict,
    QaAssessment,
    QaReport,
    TaskList,
    Verdict,
    passes,
)
from tests.test_schemas import task_data

CRITERION = "Table exists after migrate"


def check(name: str = "tests", status: CheckStatus = CheckStatus.PASSED) -> CheckResult:
    return CheckResult(name=name, command=f"run {name}", status=status, output_tail="ok")


def verdict(v: Verdict = Verdict.PASS, text: str = CRITERION, task: str = "T1") -> Any:
    return CriterionVerdict(task_id=task, criterion=text, verdict=v, evidence="test_table.py")


def assessment(*verdicts: CriterionVerdict) -> QaAssessment:
    return QaAssessment(criteria=list(verdicts) or [verdict()], summary="One criterion passes.")


def inputs(checks: list[CheckResult], criteria: list[str] | None = None) -> dict[str, str]:
    tasks = TaskList.model_validate(
        {"tasks": [task_data(acceptance_criteria=criteria or [CRITERION])]}
    )
    return {
        "tasks.json": tasks.model_dump_json(),
        "qa_checks.json": json.dumps([c.model_dump(mode="json") for c in checks]),
    }


# --- the pass rule ---------------------------------------------------------------------------


def test_all_checks_passing_and_no_failed_criterion_passes() -> None:
    assert passes([check()], [verdict(Verdict.PASS), verdict(Verdict.UNTESTED)])


def test_a_failed_check_fails_the_report() -> None:
    assert not passes([check(), check("ruff", CheckStatus.FAILED)], [verdict()])


def test_a_skipped_check_fails_the_report() -> None:
    assert not passes([check("gitleaks", CheckStatus.SKIPPED)], [verdict()])


def test_a_failed_criterion_fails_the_report() -> None:
    assert not passes([check()], [verdict(Verdict.FAIL)])


def test_a_report_whose_passed_flag_disagrees_with_the_rule_is_invalid() -> None:
    with pytest.raises(ValidationError, match="passed must follow the rule"):
        QaReport(
            checks=[check(status=CheckStatus.FAILED)],
            criteria=[verdict()],
            summary="x",
            passed=True,
        )


def test_the_agent_must_judge_at_least_one_criterion() -> None:
    with pytest.raises(ValidationError):
        QaAssessment(criteria=[], summary="nothing")


def test_an_unknown_verdict_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CriterionVerdict.model_validate(
            {"task_id": "T1", "criterion": "c", "verdict": "maybe", "evidence": "e"}
        )


# --- building the report from the agent's answer and the tool results -------------------------


def build(model: QaAssessment, ins: dict[str, str]) -> QaReport:
    return QaReport.model_validate_json(qa_stage().build(model, ins)["test_report.json"])


def test_the_report_combines_the_agents_verdicts_with_the_tool_results() -> None:
    report = build(assessment(), inputs([check()]))

    assert report.passed
    assert [c.status for c in report.checks] == [CheckStatus.PASSED]
    assert [c.verdict for c in report.criteria] == [Verdict.PASS]


def test_a_failed_tool_fails_the_report_even_if_the_agent_passes_everything() -> None:
    report = build(assessment(), inputs([check(status=CheckStatus.FAILED)]))

    assert not report.passed


def test_a_criterion_the_agent_skipped_is_recorded_as_untested() -> None:
    ins = inputs([check()], criteria=[CRITERION, "Second criterion"])

    report = build(assessment(), ins)

    skipped = next(c for c in report.criteria if c.criterion == "Second criterion")
    assert skipped.verdict == Verdict.UNTESTED
    assert "did not judge" in skipped.evidence


def test_a_verdict_for_a_criterion_not_in_the_plan_is_dropped() -> None:
    invented = verdict(Verdict.PASS, text="Something the plan never asked for")

    report = build(assessment(verdict(), invented), inputs([check()]))

    assert [c.criterion for c in report.criteria] == [CRITERION]


def test_criteria_match_ignoring_case_and_spacing_and_keep_the_plans_wording() -> None:
    sloppy = verdict(Verdict.FAIL, text="  table EXISTS after   migrate ")

    report = build(assessment(sloppy), inputs([check()]))

    assert [(c.criterion, c.verdict) for c in report.criteria] == [(CRITERION, Verdict.FAIL)]
    assert not report.passed


# --- rendering and wiring --------------------------------------------------------------------


def test_the_markdown_shows_the_verdict_checks_criteria_and_failing_output() -> None:
    failing = CheckResult(
        name="tests", command="pytest", status=CheckStatus.FAILED, output_tail="1 failed: test_x"
    )
    report = QaReport(
        checks=[failing, check("ruff")],
        criteria=[verdict(Verdict.UNTESTED)],
        summary="Nothing proves it.",
        passed=False,
    )

    text = render_qa_report(report)

    assert "QA report: FAILED" in text
    assert "**FAIL** tests" in text and "**PASS** ruff" in text
    assert "0 pass, 0 fail, 1 untested" in text
    assert "1 failed: test_x" in text
    assert "Output of ruff" not in text  # passing output is not repeated


def test_a_passing_report_says_so() -> None:
    report = QaReport(checks=[check()], criteria=[verdict()], summary="Fine.", passed=True)

    assert "QA report: PASSED" in render_qa_report(report)


def test_the_qa_agent_is_a_fast_tier_agent_with_the_assessment_schema_and_a_versioned_prompt() -> (
    None
):
    spec = next(s for s in default_api_specs() if s.stage == Stage.QA)

    assert (spec.tier, spec.schema, spec.web_search) == ("fast", QaAssessment, False)
    assert spec.prompt.version == "v1"
    assert default_api_schemas()[Stage.QA] is QaAssessment
    assert Stage.QA in default_stage_specs()


def test_the_prompt_names_the_inputs_the_orchestrator_supplies() -> None:
    spec = next(s for s in default_api_specs() if s.stage == Stage.QA)

    for name in ("tasks.json", "qa_checks.json", "qa_tests.md"):
        assert name in spec.prompt.text
