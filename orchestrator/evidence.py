"""Building the evidence a career plan may rest on, from a project's real artifacts.

This is code, not a model: merged pull requests, the QA report, the cost report and the gate
history become short factual statements with stable ids. Nothing here is estimated or rounded up.
"""

import csv
from pathlib import Path

from orchestrator.artifacts import ArtifactStore
from orchestrator.state import GateOutcome, ProjectState, Stage
from schemas import (
    PRD,
    DevResult,
    Evidence,
    EvidenceSet,
    EvidenceType,
    QaReport,
    TaskList,
    Verdict,
)


def build_evidence(
    state: ProjectState, runs_dir: Path, *, eval_results: Path | None = None
) -> EvidenceSet:
    """The facts about one finished project, in a fixed order."""
    store = ArtifactStore(runs_dir, state.project_slug)
    items: list[Evidence] = []

    tasks = _tasks(state, store)
    for task_id in state.completed_tasks:
        artifact = state.artifacts.get(f"dev/{task_id}.json")
        if artifact is None:
            continue
        result = DevResult.model_validate_json(store.read(artifact))
        if result.pr_url:
            title = tasks.get(task_id, task_id)
            items.append(
                Evidence(
                    id=f"pr-{task_id}",
                    type=EvidenceType.PR,
                    ref=result.pr_url,
                    description=f"Task {task_id} ({title}): {result.summary}",
                )
            )
    if state.completed_tasks:
        items.append(
            Evidence(
                id="tasks",
                type=EvidenceType.METRIC,
                ref="runs/tasks.json",
                description=f"{len(state.completed_tasks)} tasks built and approved, "
                "each as its own pull request",
            )
        )

    report = state.artifacts.get("test_report.json")
    if report is not None:
        items += _qa_evidence(QaReport.model_validate_json(store.read(report)))

    items.append(
        Evidence(
            id="cost",
            type=EvidenceType.METRIC,
            ref="runs/cost.json",
            description=f"Agent spend for the whole project: ${state.total_cost_usd:.2f}",
        )
    )
    items += _gate_evidence(state)

    prd = state.artifacts.get("prd.json")
    if prd is not None:
        for number, metric in enumerate(
            PRD.model_validate_json(store.read(prd)).success_metrics, 1
        ):
            items.append(
                Evidence(
                    id=f"target-{number}",
                    type=EvidenceType.DOC,
                    ref="docs/prd.md",
                    description=f"Planned target, not a measured result: {metric.name} "
                    f"({metric.target}), measured by {metric.how_measured}",
                )
            )
    if eval_results is not None:
        items += _eval_evidence(eval_results)
    return EvidenceSet(items=items)


def _tasks(state: ProjectState, store: ArtifactStore) -> dict[str, str]:
    artifact = state.artifacts.get("tasks.json")
    if artifact is None:
        return {}
    return {t.id: t.title for t in TaskList.model_validate_json(store.read(artifact)).tasks}


def _qa_evidence(report: QaReport) -> list[Evidence]:
    count = {v: sum(1 for c in report.criteria if c.verdict == v) for v in Verdict}
    checks = [c for c in report.checks if c.status.value == "passed"]
    outcome = "passed" if report.passed else "failed"
    return [
        Evidence(
            id="qa",
            type=EvidenceType.QA,
            ref="runs/test_report.json",
            description=(
                f"Independent QA {outcome}: {len(checks)} of {len(report.checks)} automated "
                f"checks passed ({', '.join(c.name for c in report.checks)}); of "
                f"{len(report.criteria)} acceptance criteria, {count[Verdict.PASS]} proven by "
                f"a test, {count[Verdict.FAIL]} failing, {count[Verdict.UNTESTED]} untested"
            ),
        )
    ]


def _gate_evidence(state: ProjectState) -> list[Evidence]:
    stages = {Stage.PRODUCT_OWNER, Stage.ARCHITECT, Stage.DEVELOPER, Stage.QA}
    gates = [g for g in state.gate_history if g.stage in stages]
    if not gates:
        return []
    count = {o: sum(1 for g in gates if g.outcome == o) for o in GateOutcome}
    return [
        Evidence(
            id="gates",
            type=EvidenceType.METRIC,
            ref="runs/trace.jsonl",
            description=(
                f"{len(gates)} human approval decisions: {count[GateOutcome.APPROVE]} approved, "
                f"{count[GateOutcome.REQUEST_CHANGES]} changes requested, "
                f"{count[GateOutcome.REJECT]} rejected"
            ),
        )
    ]


def _eval_evidence(path: Path) -> list[Evidence]:
    """The newest row of each dataset in an eval results table."""
    if not path.exists():
        return []
    latest: dict[str, dict[str, str]] = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            latest[row["dataset"]] = row
    return [
        Evidence(
            id=f"eval-{dataset}",
            type=EvidenceType.EVAL_RESULT,
            ref=str(path),
            description=(
                f"Eval '{dataset}' (prompt {row['prompt_version']}, model {row['model']}): "
                f"accuracy {float(row['accuracy']) * 100:.0f}% over {row['runs']} run(s), "
                f"{row['false_passes']} false passes, reports correct {row['reports_correct']}"
            ),
        )
        for dataset, row in latest.items()
    ]
