from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from schemas._base import Schema, Text


class CareerTrack(StrEnum):
    STRATEGY_CONSULTING = "strategy_consulting"  # MBB and strategy firms
    TECH_CONSULTING = "tech_consulting"  # technology and digital consulting


class EvidenceType(StrEnum):
    PR = "pr"  # a merged pull request
    QA = "qa"  # a result from the QA report
    METRIC = "metric"  # a measured number (cost, task count, gate outcomes)
    EVAL_RESULT = "eval_result"  # a row of an eval run
    DOC = "doc"  # a document, such as a PRD target; its numbers are plans, not results
    HUMAN = "human"  # something the person told the coach


# Evidence of these types may back a number in a bullet. A PRD target (`doc`) may not.
NUMERIC_EVIDENCE = frozenset(
    {EvidenceType.QA, EvidenceType.METRIC, EvidenceType.EVAL_RESULT, EvidenceType.HUMAN}
)


class Evidence(Schema):
    """One checkable fact. Claims in career material may only rest on these."""

    id: Text
    type: EvidenceType
    ref: Text  # a URL, a file path, or "you" for what the person said
    description: Text


class EvidenceSet(Schema):
    items: list[Evidence]

    @model_validator(mode="after")
    def _ids_are_unique(self) -> Self:
        ids = [e.id for e in self.items]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate evidence ids: {', '.join(duplicates)}")
        return self

    def get(self, evidence_id: str) -> Evidence | None:
        return next((e for e in self.items if e.id == evidence_id), None)


class ResumeBullet(Schema):
    text: Text
    track: CareerTrack
    evidence_ids: Annotated[list[Text], Field(min_length=1)]  # no evidence, no bullet


class ResumeAction(StrEnum):
    ADD = "add"
    REPLACE = "replace"
    SKIP = "skip"


class ResumeChange(Schema):
    """A proposed resume change. `skip` is a real answer: the project does not belong there."""

    action: ResumeAction
    section: Text  # for example "Projects"
    entry: Text  # the project or role the bullets belong to
    bullets: list[ResumeBullet]
    replaces: str | None = None  # the entry (or bullet) this one pushes out, for one-page fit
    tech: str | None = (
        None  # for a project entry: the tech stack line, for example "FastAPI, Redis"
    )
    reason: Text

    @model_validator(mode="after")
    def _action_matches_content(self) -> Self:
        if self.action == ResumeAction.SKIP:
            if self.bullets:
                raise ValueError("a skipped change cannot carry bullets")
            return self
        if not self.bullets:
            raise ValueError(f"a change that does {self.action.value} needs at least one bullet")
        if self.action == ResumeAction.REPLACE and not (self.replaces or "").strip():
            raise ValueError("a replace change must say what it replaces")
        return self


class Link(Schema):
    label: Text
    href: Text


class WebsiteEntry(Schema):
    """A project entry for the portfolio site, in the shape its `projects.js` already uses."""

    slug: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]
    title: Text
    hook: Text
    metric: Text
    scope: str | None = None
    problem: Text
    approach: Text
    result: Text
    tags: Annotated[list[Text], Field(min_length=1)]
    links: list[Link] = Field(default_factory=list)
    evidence_ids: Annotated[list[Text], Field(min_length=1)]


class FitDecision(StrEnum):
    INCLUDE = "include"
    SKIP = "skip"


class TrackFit(Schema):
    track: CareerTrack
    decision: FitDecision
    reason: Text


class CareerPlan(Schema):
    """What the career coach returns for one finished project."""

    fit: Annotated[list[TrackFit], Field(min_length=1)]
    readme_md: Text
    case_study_md: Text
    resume_changes: list[ResumeChange]
    website_entry: WebsiteEntry | None = None
    website_reason: Text  # why the project is, or is not, worth a place on the website
    repo_description: Annotated[str, Field(max_length=350)] | None = None  # GitHub description
    repo_topics: list[Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]] = Field(
        default_factory=list
    )  # GitHub topics: lowercase words and hyphens
    questions: list[Text] = Field(default_factory=list)  # facts it needs from the person
