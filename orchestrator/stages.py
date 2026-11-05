"""How a stage's validated output becomes stored artifacts."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orchestrator.render import render_adr, render_architecture, render_prd
from schemas import ADR, PRD, ArchitectOutput, unmapped_metrics
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
