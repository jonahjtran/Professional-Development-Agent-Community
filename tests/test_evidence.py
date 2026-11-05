import csv
from datetime import UTC, datetime
from pathlib import Path

from orchestrator.artifacts import ArtifactStore, record_artifacts
from orchestrator.evidence import build_evidence
from orchestrator.state import GateOutcome, GateRecord, ProjectState, Stage
from schemas import (
    CheckResult,
    CheckStatus,
    CriterionVerdict,
    DevResult,
    EvidenceType,
    QaReport,
    TaskList,
    Verdict,
)
from tests.factories import new_state
from tests.test_product_owner_stage import prd_json
from tests.test_schemas import task_data

NOW = datetime(2026, 11, 5, tzinfo=UTC)


def finished_project(tmp_path: Path) -> ProjectState:
    store = ArtifactStore(tmp_path, "demo")
    tasks = TaskList.model_validate(
        {"tasks": [task_data(id="T1", title="Create the table"), task_data(id="T2", title="API")]}
    )
    files = {"tasks.json": tasks.model_dump_json(), "prd.json": prd_json()}
    for n, task in enumerate(tasks.tasks, start=1):
        dev = DevResult(
            task_id=task.id,
            branch=f"agent/{task.id}",
            pr_url=f"https://github.com/me/p/pull/{n}",
            summary=f"Built {task.id}",
            deviations_from_plan=[],
            known_gaps=[],
        )
        files[f"dev/{task.id}.json"] = dev.model_dump_json()
    verdicts = [Verdict.PASS, Verdict.PASS, Verdict.UNTESTED]
    report = QaReport(
        checks=[
            CheckResult(name="tests", command="t", status=CheckStatus.PASSED, output_tail=""),
            CheckResult(name="lint", command="l", status=CheckStatus.PASSED, output_tail=""),
        ],
        criteria=[
            CriterionVerdict(task_id="T1", criterion=f"c{i}", verdict=v, evidence="e")
            for i, v in enumerate(verdicts)
        ],
        summary="Fine.",
        passed=True,
    )
    files["test_report.json"] = report.model_dump_json()
    state = record_artifacts(
        new_state(), {n: store.write(n, text, revision=1) for n, text in files.items()}
    )
    gates = [
        GateRecord(stage=Stage.PRODUCT_OWNER, outcome=GateOutcome.APPROVE, ts=NOW),
        GateRecord(
            stage=Stage.ARCHITECT, outcome=GateOutcome.REQUEST_CHANGES, comments="x", ts=NOW
        ),
        GateRecord(stage=Stage.ARCHITECT, outcome=GateOutcome.APPROVE, ts=NOW),
    ]
    return state.evolve(
        completed_tasks=["T1", "T2"],
        cost_usd={Stage.ARCHITECT: 0.5, Stage.QA: 0.25},
        gate_history=gates,
    )


def by_id(tmp_path: Path) -> dict[str, tuple[EvidenceType, str, str]]:
    evidence = build_evidence(finished_project(tmp_path), tmp_path)
    return {e.id: (e.type, e.ref, e.description) for e in evidence.items}


def test_each_merged_task_becomes_pr_evidence_with_its_url(tmp_path: Path) -> None:
    items = by_id(tmp_path)

    kind, ref, text = items["pr-T1"]
    assert kind == EvidenceType.PR and ref == "https://github.com/me/p/pull/1"
    assert "Create the table" in text and "Built T1" in text
    assert "2 tasks built and approved" in items["tasks"][2]


def test_the_qa_report_is_summarised_with_real_counts(tmp_path: Path) -> None:
    kind, _ref, text = by_id(tmp_path)["qa"]

    assert kind == EvidenceType.QA
    assert "2 of 2 automated checks passed" in text
    assert "3 acceptance criteria, 2 proven by a test, 0 failing, 1 untested" in text


def test_the_spend_is_the_exact_total(tmp_path: Path) -> None:
    kind, _ref, text = by_id(tmp_path)["cost"]

    assert kind == EvidenceType.METRIC and text == "Agent spend for the whole project: $0.75"


def test_human_decisions_at_the_build_gates_are_counted(tmp_path: Path) -> None:
    _kind, _ref, text = by_id(tmp_path)["gates"]

    assert text == "3 human approval decisions: 2 approved, 1 changes requested, 0 rejected"


def test_prd_targets_are_documents_and_say_they_are_not_results(tmp_path: Path) -> None:
    kind, _ref, text = by_id(tmp_path)["target-1"]

    assert kind == EvidenceType.DOC
    assert "Planned target, not a measured result" in text


def test_a_project_that_never_reached_qa_has_no_qa_evidence(tmp_path: Path) -> None:
    state = finished_project(tmp_path)
    without = state.evolve(
        artifacts={k: v for k, v in state.artifacts.items() if k != "test_report.json"}
    )

    ids = {e.id for e in build_evidence(without, tmp_path).items}

    assert "qa" not in ids and "cost" in ids


def test_eval_results_use_the_newest_row_of_each_dataset(tmp_path: Path) -> None:
    results = tmp_path / "results.csv"
    columns = [
        "dataset",
        "prompt_version",
        "model",
        "runs",
        "accuracy",
        "false_passes",
        "reports_correct",
    ]
    with results.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerow(dict(zip(columns, ["qa", "v1", "m", "1", "0.5", "3", "6/12"], strict=True)))
        writer.writerow(
            dict(zip(columns, ["qa", "v2", "m", "3", "0.917", "0", "36/36"], strict=True))
        )

    evidence = build_evidence(finished_project(tmp_path), tmp_path, eval_results=results)

    item = next(e for e in evidence.items if e.id == "eval-qa")
    assert item.type == EvidenceType.EVAL_RESULT
    assert "prompt v2" in item.description and "accuracy 92% over 3 run(s)" in item.description
    assert "reports correct 36/36" in item.description


def test_a_missing_eval_table_adds_nothing(tmp_path: Path) -> None:
    evidence = build_evidence(
        finished_project(tmp_path), tmp_path, eval_results=tmp_path / "none.csv"
    )

    assert not any(e.id.startswith("eval-") for e in evidence.items)
