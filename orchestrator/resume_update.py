"""Applying approved resume changes to the structured resume data, and writing it back.

The Overleaf file is still edited by hand; this keeps `resume.yaml` (what the coach checks claims
and the page budget against) in step with what the person accepted.
"""

import yaml

from schemas import ResumeAction, ResumeChange, ResumeData, ResumeEntry, ResumeSection


class ResumeUpdateError(Exception):
    """A change cannot be applied to the resume data; the message says why."""


def apply_changes(resume: ResumeData, changes: list[ResumeChange]) -> tuple[ResumeData, list[str]]:
    """The resume after the changes, and notes for anything that needs a manual look."""
    sections = {s.name: s for s in resume.sections}
    notes: list[str] = []
    for change in changes:
        if change.action == ResumeAction.SKIP:
            continue
        name = next((n for n in sections if n.lower() == change.section.strip().lower()), None)
        if name is None:
            raise ResumeUpdateError(f"the resume has no section called {change.section!r}")
        section = sections[name]
        entries = list(section.entries)
        if change.replaces:
            kept = [e for e in entries if e.title.lower() != change.replaces.strip().lower()]
            if len(kept) == len(entries):
                notes.append(
                    f"'{change.replaces}' is not an entry in {name}; remove what it replaces "
                    "by hand"
                )
            entries = kept
        entries.append(
            ResumeEntry(
                title=change.entry,
                subtitle=change.tech,
                bullets=[b.text for b in change.bullets],
            )
        )
        sections[name] = ResumeSection(name=name, max_bullets=section.max_bullets, entries=entries)
        if sections[name].bullet_count > section.max_bullets:
            raise ResumeUpdateError(
                f"{name} would have {sections[name].bullet_count} bullets, over its "
                f"{section.max_bullets}-bullet page budget"
            )
    return ResumeData(sections=[sections[s.name] for s in resume.sections]), notes


def dump_resume(resume: ResumeData, original_text: str) -> str:
    """The resume as YAML, keeping the comment block at the top of the original file."""
    header = []
    for line in original_text.splitlines():
        if not line.startswith("#"):
            break
        header.append(line)
    body = yaml.safe_dump(
        resume.model_dump(mode="json", exclude_none=True),
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )
    return "\n".join(header) + ("\n" if header else "") + str(body)
