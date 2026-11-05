"""How a stage's validated output becomes stored artifacts."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orchestrator.render import render_adr, render_architecture, render_prd, render_qa_report
from schemas import (
    ADR,
    PRD,
    ArchitectOutput,
    CheckResult,
    CriterionVerdict,
    QaAssessment,
    QaReport,
    TaskList,
    Verdict,
    passes,
    unmapped_metrics,
)
from schemas._base import Schema


@dataclass(frozen=True)
class StageSpec:
    """What the pipeline does with a stage's output: validate, then store these artifacts.

    `json_name` is the artifact a revision starts from; `md_name` is what the human reads.
    `build` turns the validated document (and the approved inputs the stage was given) into
    {artifact name: text}. A new draft removes earlier artifacts whose names start with one of
    `replaces`, so stale files from a previous draft do not linger.
    """

    schema: type[Schema]
    json_name: str
    md_name: str
    build: Callable[[Any, dict[str, str]], dict[str, str]]
    replaces: tuple[str, ...] = ()


def prd_stage() -> StageSpec:
    return StageSpec(
        schema=PRD,
        json_name="prd.json",
        md_name="prd.md",
        build=lambda prd, _inputs: {
            "prd.json": prd.model_dump_json(indent=2),
            "prd.md": render_prd(prd),
        },
    )


def _build_architect(output: ArchitectOutput, inputs: dict[str, str]) -> dict[str, str]:
    prd = PRD.model_validate_json(inputs["prd.json"])
    unmapped = unmapped_metrics(prd, output.task_list)
    artifacts = {
        "architecture.json": output.model_dump_json(indent=2),
        "architecture.md": render_architecture(output, unmapped),
        "tasks.json": output.task_list.model_dump_json(indent=2),
    }
    for number, choice in enumerate(output.architecture.tech_choices, start=1):
        adr = ADR(
            number=number,
            title=f"Use {choice.choice}",
            context=f"Alternatives considered: {', '.join(choice.alternatives)}.",
            decision=choice.choice,
            consequences=f"Rationale: {choice.rationale}",
        )
        artifacts[f"adr/{adr.filename}"] = render_adr(adr)
    return artifacts


def architect_stage() -> StageSpec:
    return StageSpec(
        schema=ArchitectOutput,
        json_name="architecture.json",
        md_name="architecture.md",
        build=_build_architect,
        replaces=("adr/",),
    )


def _build_qa(assessment: QaAssessment, inputs: dict[str, str]) -> dict[str, str]:
    """Combine the agent's verdicts with the orchestrator's check results into the QA report.

    The task list is the source of truth for which criteria exist: a criterion the agent skipped
    is recorded as untested, and a verdict for a criterion that is not in the plan is dropped.
    """
    plan = TaskList.model_validate_json(inputs["tasks.json"])
    checks = [CheckResult.model_validate(c) for c in json.loads(inputs["qa_checks.json"])]
    given = {(v.task_id, _key(v.criterion)): v for v in assessment.criteria}
    criteria = [
        _with_plan_wording(given[(task.id, _key(text))], text)
        if (task.id, _key(text)) in given
        else CriterionVerdict(
            task_id=task.id,
            criterion=text,
            verdict=Verdict.UNTESTED,
            evidence="The QA agent did not judge this criterion.",
        )
        for task in plan.tasks
        for text in task.acceptance_criteria
    ]
    report = QaReport(
        checks=checks,
        criteria=criteria,
        summary=assessment.summary,
        passed=passes(checks, criteria),
    )
    return {
        "test_report.json": report.model_dump_json(indent=2),
        "test_report.md": render_qa_report(report),
    }


def _with_plan_wording(found: CriterionVerdict, text: str) -> CriterionVerdict:
    return found.model_copy(update={"criterion": text})


def _key(text: str) -> str:
    return " ".join(text.lower().split())


def qa_stage() -> StageSpec:
    return StageSpec(
        schema=QaAssessment,
        json_name="test_report.json",
        md_name="test_report.md",
        build=_build_qa,
    )
