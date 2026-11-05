import csv
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evals.qa_cases import CASES, DOUBLE, GREET, QaCase
from evals.qa_eval import (
    CSV_COLUMNS,
    PYTEST,
    EvalBudgetError,
    Summary,
    append_result,
    format_summary,
    run_case,
    run_eval,
    write_project,
)
from orchestrator.config import CheckCommand, QaConfig
from orchestrator.pipeline import AgentError, AgentRequest, AgentResult
from orchestrator.qa_checks import run_checks
from schemas import CheckStatus, CriterionVerdict, QaAssessment, TaskList, Verdict


class Oracle:
    """A fake QA agent that records its requests and builds answers from given verdicts."""

    def __init__(self, cost: float = 0.01) -> None:
        self.cost = cost
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        return self.answer(request, (Verdict.PASS, Verdict.PASS))

    def answer(self, request: AgentRequest, verdicts: tuple[Verdict, ...]) -> AgentResult:
        plan = TaskList.model_validate_json(request.inputs["tasks.json"])
        criteria = plan.tasks[0].acceptance_criteria
        assessment = QaAssessment(
            criteria=[
                CriterionVerdict(task_id="T1", criterion=c, verdict=v, evidence="e")
                for c, v in zip(criteria, verdicts, strict=True)
            ],
            summary="s",
        )
        return AgentResult(
            output=assessment.model_dump_json(),
            cost_usd=self.cost,
            model="fast-model-id",
            prompt_version="v1",
        )


class Scripted:
    """Answers every request with fixed verdicts, whatever the case."""

    def __init__(self, *verdicts: Verdict, cost: float = 0.01) -> None:
        self.oracle = Oracle(cost=cost)
        self.verdicts = verdicts

    def run(self, request: AgentRequest) -> AgentResult:
        return self.oracle.answer(request, self.verdicts)


# --- the dataset itself ----------------------------------------------------------------------


def test_case_names_are_unique_and_each_says_what_it_checks() -> None:
    assert len({c.name for c in CASES}) == len(CASES)
    assert all(c.why for c in CASES)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_the_real_pytest_result_matches_what_the_case_says(case: QaCase) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_project(root, case)
        cfg = QaConfig(
            timeout_seconds=60, tail_lines=20, checks=[CheckCommand(name="tests", command=PYTEST)]
        )

        [result] = run_checks(cfg, root)

    assert result.status == case.tests_status, result.output_tail


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_the_expected_report_outcome_follows_from_the_tool_result_and_the_verdicts(
    case: QaCase,
) -> None:
    tools_ok = case.tests_status == CheckStatus.PASSED
    no_fail = Verdict.FAIL not in case.expected

    assert case.passed == (tools_ok and no_fail)


def test_the_dataset_has_cases_of_every_kind() -> None:
    kinds = {v for c in CASES for v in c.expected}

    assert kinds == {Verdict.PASS, Verdict.FAIL, Verdict.UNTESTED}
    assert any(not c.passed for c in CASES) and any(c.passed for c in CASES)


# --- running and scoring a case --------------------------------------------------------------


def test_the_agent_is_given_the_same_inputs_the_pipeline_gives_it() -> None:
    oracle = Oracle()
    case = CASES[1]  # bug_caught

    run_case(case, oracle)

    [request] = oracle.requests
    assert set(request.inputs) == {"tasks.json", "qa_checks.json", "qa_tests.md"}
    assert GREET in request.inputs["tasks.json"] and DOUBLE in request.inputs["tasks.json"]
    assert '"status":"failed"' in request.inputs["qa_checks.json"].replace(" ", "")
    assert "test_double" in request.inputs["qa_tests.md"]


def test_a_perfect_answer_scores_fully() -> None:
    for case in CASES:
        run = run_case(case, Scripted(*case.expected))

        assert run.correct == 2 and run.false_passes == 0 and run.report_correct, case.name


def test_calling_everything_pass_is_counted_as_false_passes() -> None:
    case = next(c for c in CASES if c.name == "uncovered")  # expects pass, untested

    run = run_case(case, Scripted(Verdict.PASS, Verdict.PASS))

    assert (run.correct, run.false_passes) == (1, 1)


def test_the_report_outcome_comes_from_the_rule_not_the_agents_verdicts() -> None:
    case = next(c for c in CASES if c.name == "bug_caught")  # pytest fails

    run = run_case(case, Scripted(Verdict.PASS, Verdict.PASS))  # the agent misses the bug

    assert run.got_passed is False  # the failed tool still fails the report
    assert run.report_correct
    assert run.correct == 1


def test_a_missing_verdict_is_scored_as_untested() -> None:
    case = next(c for c in CASES if c.name == "uncovered")

    class Skips:
        def run(self, request: AgentRequest) -> AgentResult:
            partial = QaAssessment(
                criteria=[
                    CriterionVerdict(
                        task_id="T1", criterion=GREET, verdict=Verdict.PASS, evidence="e"
                    )
                ],
                summary="s",
            )
            return AgentResult(output=partial.model_dump_json(), cost_usd=0.0)

    run = run_case(case, Skips())

    assert run.got == (Verdict.PASS, Verdict.UNTESTED)


def test_a_failed_agent_call_is_recorded_as_an_error_with_its_cost() -> None:
    class Broken:
        def run(self, request: AgentRequest) -> AgentResult:
            raise AgentError("API error: rate limited", cost_usd=0.002)

    run = run_case(CASES[0], Broken())

    assert run.error == "API error: rate limited" and run.cost_usd == 0.002
    assert run.correct == 0 and not run.report_correct


# --- running the whole set -------------------------------------------------------------------


def test_every_case_runs_the_requested_number_of_times() -> None:
    summary = run_eval(CASES[:2], Scripted(Verdict.PASS, Verdict.PASS), runs=3)

    assert len(summary.runs) == 6
    assert [r.case for r in summary.runs[:2]] == [CASES[0].name, CASES[1].name]


def test_the_summary_totals_accuracy_false_passes_cost_and_correct_reports() -> None:
    summary = run_eval(CASES, Scripted(Verdict.PASS, Verdict.PASS))

    assert summary.criteria == 2 * len(CASES)
    expected_correct = sum(1 for c in CASES for v in c.expected if v == Verdict.PASS)
    assert summary.accuracy == pytest.approx(expected_correct / (2 * len(CASES)))
    assert summary.false_passes == sum(1 for c in CASES for v in c.expected if v != Verdict.PASS)
    assert summary.cost_usd == pytest.approx(0.01 * len(CASES))


def test_the_eval_stops_when_it_has_spent_more_than_its_limit() -> None:
    with pytest.raises(EvalBudgetError):
        run_eval(CASES, Scripted(Verdict.PASS, Verdict.PASS, cost=1.0), max_usd=1.5)


def test_an_empty_summary_does_not_divide_by_zero() -> None:
    summary = Summary([])

    assert summary.accuracy == 0.0 and summary.mean_latency_s == 0.0


def test_the_printed_summary_marks_wrong_cases_and_shows_what_was_wanted() -> None:
    summary = run_eval(CASES[:4], Scripted(Verdict.PASS, Verdict.PASS))

    text = format_summary(summary)

    assert "ok     clean" in text
    assert "WRONG  uncovered: pass pass(want untested)" in text
    assert "false pass(es)" in text and "reports correct" in text


# --- the results table -----------------------------------------------------------------------


def test_each_run_appends_one_row_and_the_header_is_written_once(tmp_path: Path) -> None:
    path = tmp_path / "results" / "results.csv"
    summary = run_eval(CASES[:2], Scripted(Verdict.PASS, Verdict.PASS))
    when = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)

    append_result(path, summary, git_sha="abc1234", dataset="qa", runs=1, now=when)
    append_result(path, summary, git_sha="def5678", dataset="qa", runs=1, now=when)

    rows = list(csv.DictReader(path.open()))
    assert list(rows[0]) == CSV_COLUMNS
    assert [r["git_sha"] for r in rows] == ["abc1234", "def5678"]
    assert rows[0]["prompt_version"] == "v1" and rows[0]["model"] == "fast-model-id"
    assert rows[0]["timestamp"] == "2026-11-05T12:00:00+00:00"
    assert rows[0]["reports_correct"] == "2/2"


def test_a_case_can_set_its_own_criteria() -> None:
    oracle = Oracle()
    case = next(c for c in CASES if c.name == "edge_not_covered")

    run_case(case, oracle)

    assert "including negative numbers" in oracle.requests[0].inputs["tasks.json"]
