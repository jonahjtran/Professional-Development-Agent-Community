"""Scoring the QA agent against the cases in `qa_cases`.

Each run gives the agent what the pipeline would: the real pytest result on the case's files, the
test files, and the task list. The agent's verdicts go through the same report builder the
pipeline uses, then are compared with the verdicts a careful reviewer would give. Scoring is
programmatic, so there is no judge model to trust.
"""

import csv
import shlex
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from evals.qa_cases import QaCase
from orchestrator.config import CheckCommand, QaConfig
from orchestrator.pipeline import AgentError, AgentRequest, AgentRunner
from orchestrator.qa_checks import collect_tests, run_checks
from orchestrator.stages import qa_stage
from orchestrator.state import Stage
from schemas import QaAssessment, QaReport, TaskList, Verdict

PYTEST = f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider"
CSV_COLUMNS = [
    "timestamp",
    "git_sha",
    "prompt_version",
    "model",
    "dataset",
    "runs",
    "accuracy",
    "false_passes",
    "reports_correct",
    "cost_usd",
    "mean_latency_s",
]


class EvalBudgetError(Exception):
    """The eval spent more than its limit and stopped."""


@dataclass(frozen=True)
class CaseRun:
    case: str
    expected: tuple[Verdict, ...]
    got: tuple[Verdict, ...]  # empty if the agent call failed
    expected_passed: bool
    got_passed: bool | None
    cost_usd: float
    latency_s: float
    error: str | None = None
    model: str = ""
    prompt_version: str = ""

    @property
    def correct(self) -> int:
        return sum(1 for e, g in zip(self.expected, self.got, strict=False) if e == g)

    @property
    def false_passes(self) -> int:
        """Criteria the agent called pass although a careful reviewer would not."""
        return sum(
            1
            for e, g in zip(self.expected, self.got, strict=False)
            if g == Verdict.PASS and e != Verdict.PASS
        )

    @property
    def report_correct(self) -> bool:
        return self.got_passed == self.expected_passed


@dataclass(frozen=True)
class Summary:
    runs: list[CaseRun]

    @property
    def criteria(self) -> int:
        return sum(len(r.expected) for r in self.runs)

    @property
    def accuracy(self) -> float:
        return sum(r.correct for r in self.runs) / self.criteria if self.criteria else 0.0

    @property
    def false_passes(self) -> int:
        return sum(r.false_passes for r in self.runs)

    @property
    def reports_correct(self) -> int:
        return sum(1 for r in self.runs if r.report_correct)

    @property
    def cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.runs)

    @property
    def mean_latency_s(self) -> float:
        return sum(r.latency_s for r in self.runs) / len(self.runs) if self.runs else 0.0


def write_project(root: Path, case: QaCase) -> None:
    for name, text in case.files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def run_case(case: QaCase, runner: AgentRunner) -> CaseRun:
    """Run the real tests on the case, ask the QA agent, and compare its verdicts."""
    expected = case.expected
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_project(root, case)
        checks = run_checks(
            QaConfig(
                timeout_seconds=120,
                tail_lines=30,
                checks=[CheckCommand(name="tests", command=PYTEST)],
            ),
            root,
        )
        tests_md = collect_tests(root)
    plan = TaskList.model_validate(
        {
            "tasks": [
                {
                    "id": "T1",
                    "title": "Greeting and doubling",
                    "description": "Two small functions.",
                    "acceptance_criteria": list(case.criteria),
                    "depends_on": [],
                    "size": "S",
                }
            ]
        }
    )
    inputs = {
        "tasks.json": plan.model_dump_json(),
        "qa_checks.json": "[" + ",".join(c.model_dump_json() for c in checks) + "]",
        "qa_tests.md": tests_md,
    }
    request = AgentRequest(
        stage=Stage.QA, project="eval", idea="Greeting and doubling functions", inputs=inputs
    )
    started = time.monotonic()
    try:
        result = runner.run(request)
    except AgentError as error:
        return CaseRun(
            case=case.name,
            expected=expected,
            got=(),
            expected_passed=case.passed,
            got_passed=None,
            cost_usd=error.cost_usd,
            latency_s=time.monotonic() - started,
            error=str(error),
        )
    latency = time.monotonic() - started
    assessment = QaAssessment.model_validate_json(result.output)
    report = QaReport.model_validate_json(qa_stage().build(assessment, inputs)["test_report.json"])
    return CaseRun(
        case=case.name,
        expected=expected,
        got=tuple(c.verdict for c in report.criteria),
        expected_passed=case.passed,
        got_passed=report.passed,
        cost_usd=result.cost_usd,
        latency_s=latency,
        model=result.model,
        prompt_version=result.prompt_version,
    )


def run_eval(
    cases: Sequence[QaCase], runner: AgentRunner, *, runs: int = 1, max_usd: float = 2.0
) -> Summary:
    """Run every case `runs` times, stopping early if the spend passes `max_usd`."""
    results: list[CaseRun] = []
    for _ in range(runs):
        for case in cases:
            if sum(r.cost_usd for r in results) > max_usd:
                raise EvalBudgetError(f"spent more than ${max_usd:.2f}; stopping")
            results.append(run_case(case, runner))
    return Summary(results)


def format_summary(summary: Summary) -> str:
    lines = []
    for r in summary.runs:
        if r.error:
            lines.append(f"ERROR  {r.case}: {r.error}")
            continue
        mark = "ok   " if r.got == r.expected and r.report_correct else "WRONG"
        shown = " ".join(
            f"{g.value}" if g == e else f"{g.value}(want {e.value})"
            for g, e in zip(r.got, r.expected, strict=True)
        )
        lines.append(f"{mark}  {r.case}: {shown}; report passed={r.got_passed}")
    lines.append(
        f"\naccuracy {summary.accuracy:.0%} of {summary.criteria} criteria, "
        f"{summary.false_passes} false pass(es), "
        f"{summary.reports_correct}/{len(summary.runs)} reports correct, "
        f"cost ${summary.cost_usd:.4f}, mean latency {summary.mean_latency_s:.1f}s"
    )
    return "\n".join(lines)


def append_result(
    path: Path,
    summary: Summary,
    *,
    git_sha: str,
    dataset: str,
    runs: int,
    now: datetime | None = None,
) -> None:
    """Add one row per eval run to the results table, writing the header the first time."""
    first = summary.runs[0] if summary.runs else None
    row = {
        "timestamp": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "git_sha": git_sha,
        "prompt_version": first.prompt_version if first else "",
        "model": first.model if first else "",
        "dataset": dataset,
        "runs": runs,
        "accuracy": f"{summary.accuracy:.3f}",
        "false_passes": summary.false_passes,
        "reports_correct": f"{summary.reports_correct}/{len(summary.runs)}",
        "cost_usd": f"{summary.cost_usd:.4f}",
        "mean_latency_s": f"{summary.mean_latency_s:.2f}",
    }
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if new:
            writer.writeheader()
        writer.writerow(row)
