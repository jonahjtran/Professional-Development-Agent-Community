"""How a stage's validated output becomes stored artifacts."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orchestrator.render import render_prd
from schemas import PRD
from schemas._base import Schema


@dataclass(frozen=True)
class StageSpec:
    """What the pipeline does with a stage's output: validate, then store these artifacts.

    `json_name` is the artifact a revision starts from; `md_name` is what the human reads.
    """

    schema: type[Schema]
    json_name: str
    md_name: str
    build: Callable[[Any], dict[str, str]]  # validated document -> {artifact name: text}


def prd_stage() -> StageSpec:
    return StageSpec(
        schema=PRD,
        json_name="prd.json",
        md_name="prd.md",
        build=lambda prd: {
            "prd.json": prd.model_dump_json(indent=2),
            "prd.md": render_prd(prd),
        },
    )
