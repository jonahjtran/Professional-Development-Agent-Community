"""The rules that keep career material honest. Code enforces them, never the model.

Every bullet cites evidence, every number in a bullet must appear in the cited evidence, and a
resume section may not grow past its one-page budget without naming what it replaces.
"""

import re
from collections.abc import Iterable

from schemas import CareerPlan, EvidenceSet, ResumeBullet, ResumeData, WebsiteEntry
from schemas.career import NUMERIC_EVIDENCE, ResumeAction

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> set[str]:
    """The numbers in `text` as plain digits: '$45M', '1,200+' and '74%' give 45, 1200, 74."""
    return {m.group().replace(",", "").rstrip(".") for m in _NUMBER.finditer(text)}


def _problems_for_claim(
    label: str, text: str, evidence_ids: Iterable[str], evidence: EvidenceSet
) -> list[str]:
    problems: list[str] = []
    backed: set[str] = set()
    for evidence_id in evidence_ids:
        item = evidence.get(evidence_id)
        if item is None:
            problems.append(f"{label} cites {evidence_id}, which is not in the evidence")
        elif item.type in NUMERIC_EVIDENCE:
            backed |= numbers_in(item.description)
    for number in sorted(numbers_in(text) - backed):
        problems.append(f"{label} states {number}, which no cited measured evidence contains")
    return problems


def _bullet_problems(bullet: ResumeBullet, evidence: EvidenceSet) -> list[str]:
    return _problems_for_claim(
        f"resume bullet {bullet.text[:40]!r}", bullet.text, bullet.evidence_ids, evidence
    )


def _entry_problems(entry: WebsiteEntry, evidence: EvidenceSet) -> list[str]:
    text = " ".join([entry.hook, entry.metric, entry.problem, entry.approach, entry.result])
    return _problems_for_claim(f"website entry {entry.slug}", text, entry.evidence_ids, evidence)


def plan_problems(plan: CareerPlan, evidence: EvidenceSet, resume: ResumeData) -> list[str]:
    """Everything wrong with a plan, in plain words; empty means it may go to the human."""
    problems: list[str] = []
    added: dict[str, int] = {}
    for change in plan.resume_changes:
        for bullet in change.bullets:
            problems += _bullet_problems(bullet, evidence)
        if change.action != ResumeAction.ADD:
            continue
        section = resume.section(change.section)
        if section is None:
            problems.append(f"resume change targets the unknown section {change.section!r}")
            continue
        added[section.name] = added.get(section.name, 0) + len(change.bullets)
        if (
            section.bullet_count + added[section.name] > section.max_bullets
            and not (change.replaces or "").strip()
        ):
            problems.append(
                f"adding to {section.name} passes its {section.max_bullets}-bullet page budget; "
                "say what it replaces"
            )
    if plan.website_entry is not None:
        problems += _entry_problems(plan.website_entry, evidence)
    return problems
