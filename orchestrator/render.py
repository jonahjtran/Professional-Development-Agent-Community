"""Turns validated documents into Markdown a human reads at a gate."""

from schemas import PRD


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def render_prd(prd: PRD) -> str:
    sections = [
        "# Product requirements",
        f"Track: {prd.track.value}",
        f"## Problem\n{prd.problem}",
        f"## Users\n{_bullets(prd.users)}",
        f"## Goals\n{_bullets(prd.goals)}",
        "## Success metrics\n"
        + "\n".join(
            f"- **{m.name}**: target {m.target}; measured by {m.how_measured}"
            for m in prd.success_metrics
        ),
        f"## Scope\n{_bullets(prd.scope)}",
        f"## Non-goals\n{_bullets(prd.non_goals)}",
        f"## Risks\n{_bullets(prd.risks)}",
    ]
    if prd.assumptions:
        sections.append(f"## Assumptions\n{_bullets(prd.assumptions)}")
    if prd.stakeholders:
        sections.append(f"## Stakeholders\n{_bullets(prd.stakeholders)}")
    if prd.adoption_risks:
        sections.append(f"## Adoption risks\n{_bullets(prd.adoption_risks)}")
    return "\n\n".join(sections) + "\n"
