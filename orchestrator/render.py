"""Turns validated documents into Markdown a human reads at a gate."""

from schemas import ADR, PRD, ArchitectOutput, CareerPlan, QaReport


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


_CHECK_MARK = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIPPED"}
_VERDICT_MARK = {"pass": "PASS", "fail": "FAIL", "untested": "UNTESTED"}


def render_qa_report(report: QaReport) -> str:
    """Markdown for the QA gate: the verdict, the tool results, then each criterion."""
    counts = {v: sum(1 for c in report.criteria if c.verdict.value == v) for v in _VERDICT_MARK}
    sections = [
        f"# QA report: {'PASSED' if report.passed else 'FAILED'}",
        report.summary,
        "## Checks\n"
        + "\n".join(
            f"- **{_CHECK_MARK[c.status.value]}** {c.name} (`{c.command}`)" for c in report.checks
        ),
        f"## Acceptance criteria\n{counts['pass']} pass, {counts['fail']} fail, "
        f"{counts['untested']} untested\n\n"
        + "\n".join(
            f"- **{_VERDICT_MARK[c.verdict.value]}** {c.task_id}: {c.criterion}\n  - {c.evidence}"
            for c in report.criteria
        ),
    ]
    problems = [c for c in report.checks if c.status.value != "passed"]
    for check in problems:
        sections.append(f"## Output of {check.name}\n```\n{check.output_tail}\n```")
    return "\n\n".join(sections) + "\n"


def render_career_plan(plan: CareerPlan) -> str:
    """Markdown for the career gate: every item the person can approve or skip, in full."""
    sections = ["# Career plan"]
    sections.append(
        "## Fit with your goals\n"
        + "\n".join(f"- **{f.track.value}**: {f.decision.value}. {f.reason}" for f in plan.fit)
    )
    resume = []
    for change in plan.resume_changes:
        head = f"### {change.action.value.upper()}: {change.entry} ({change.section})"
        lines = [head, change.reason]
        if change.replaces:
            lines.append(f"Replaces: {change.replaces}")
        lines += [f"- {b.text}  [{', '.join(b.evidence_ids)}]" for b in change.bullets]
        resume.append("\n".join(lines))
    sections.append(
        "## Resume\n" + ("\n\n".join(resume) if resume else "No resume change is proposed.")
    )
    if plan.website_entry is None:
        website = f"No website entry. {plan.website_reason}"
    else:
        entry = plan.website_entry
        website = (
            f"{plan.website_reason}\n\n**{entry.title}** ({entry.slug}): {entry.hook}\n"
            f"- Metric: {entry.metric}\n- Problem: {entry.problem}\n"
            f"- Approach: {entry.approach}\n- Result: {entry.result}\n"
            f"- Tags: {', '.join(entry.tags)}  [{', '.join(entry.evidence_ids)}]"
        )
    sections.append(f"## Website\n{website}")
    if plan.questions:
        sections.append("## Questions for you\n" + _bullets(plan.questions))
    sections += [f"## README\n{plan.readme_md}", f"## Case study\n{plan.case_study_md}"]
    return "\n\n".join(sections) + "\n"
