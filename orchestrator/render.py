"""Turns validated documents into Markdown a human reads at a gate."""

from schemas import ADR, PRD, ArchitectOutput


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


def render_architecture(output: ArchitectOutput, unmapped: list[str]) -> str:
    """Markdown for the Architecture gate: the design, the tasks, and any uncovered PRD metrics."""
    architecture = output.architecture
    sections = [
        "# Architecture",
        "## Components\n"
        + "\n".join(
            f"- **{c.name}** ({c.tech}): {c.responsibility}" for c in architecture.components
        ),
        f"## Data flow\n{architecture.data_flow}",
        f"## Interfaces\n{_bullets(architecture.interfaces)}",
        "## Tech choices\n"
        + "\n".join(
            f"- **{t.choice}** (instead of {', '.join(t.alternatives)}): {t.rationale}"
            for t in architecture.tech_choices
        ),
        f"## Risks\n{_bullets(architecture.risks)}",
        "## Tasks\n"
        + "\n".join(
            f"- **{t.id}** [{t.size.value}] {t.title}"
            + (f" (after {', '.join(t.depends_on)})" if t.depends_on else "")
            + "".join(f"\n  - {c}" for c in t.acceptance_criteria)
            for t in output.task_list.tasks
        ),
    ]
    if unmapped:
        sections.append(
            "## Warnings\nThese PRD success metrics are not named in any task's acceptance "
            "criteria:\n" + _bullets(unmapped)
        )
    return "\n\n".join(sections) + "\n"


def render_adr(adr: ADR) -> str:
    return (
        f"# ADR {adr.number:03d}: {adr.title}\n\n"
        f"## Context\n{adr.context}\n\n"
        f"## Decision\n{adr.decision}\n\n"
        f"## Consequences\n{adr.consequences}\n"
    )
