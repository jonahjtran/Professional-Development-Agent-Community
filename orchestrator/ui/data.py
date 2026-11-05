"""What the UI shows, read from the files a run already writes. Nothing here changes anything."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from orchestrator.artifacts import ArtifactStore
from orchestrator.config import Config
from orchestrator.inbox import all_pending, run_is_alive
from orchestrator.latex import render_snippets
from orchestrator.state import GateRecord, ProjectState, Stage, Status, load_state, state_path
from orchestrator.trace import EventType, TraceEvent, trace_path
from schemas import CareerPlan, EvidenceSet, ResumeChange

STAGES = (Stage.PRODUCT_OWNER, Stage.ARCHITECT, Stage.DEVELOPER, Stage.QA, Stage.CAREER_COACH)
LABELS = {
    Stage.PRODUCT_OWNER: "Product Owner",
    Stage.ARCHITECT: "Architect",
    Stage.DEVELOPER: "Developer",
    Stage.QA: "QA",
    Stage.CAREER_COACH: "Career coach",
}


@dataclass(frozen=True)
class ProjectSummary:
    slug: str
    idea: str
    stage: Stage
    status: Status
    detail: str  # what the project is waiting for, if anything
    cost_usd: float
    cap_usd: float
    pending: int
    alive: bool


@dataclass(frozen=True)
class StageRow:
    stage: Stage
    label: str
    state: str  # done, current, waiting, upcoming
    spent: float
    cap: float

    @property
    def fraction(self) -> float:
        return min(self.spent / self.cap, 1.0) if self.cap else 0.0


@dataclass(frozen=True)
class Link:
    label: str
    url: str


@dataclass(frozen=True)
class ProjectView:
    summary: ProjectSummary
    state: ProjectState
    stages: list[StageRow]
    warn_fraction: float
    gates: list[GateRecord]
    links: list[Link] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)


def project_cap(state: ProjectState, config: Config) -> float:
    return config.budget.project_cap_usd + state.allowances.extra_project_usd


def stage_cap(state: ProjectState, config: Config, stage: Stage) -> float:
    base = float(getattr(config.budget.stage_caps_usd, stage.value))
    return base + state.allowances.extra_stage_usd.get(stage, 0.0)


def _summary(state: ProjectState, runs_dir: Path, config: Config) -> ProjectSummary:
    slug = state.project_slug
    return ProjectSummary(
        slug=slug,
        idea=state.idea,
        stage=state.stage,
        status=state.status,
        detail=state.intervention.detail if state.intervention else "",
        cost_usd=state.total_cost_usd,
        cap_usd=project_cap(state, config),
        pending=sum(1 for p in all_pending(runs_dir) if p.project == slug),
        alive=run_is_alive(runs_dir, slug),
    )


def load_project(runs_dir: Path, slug: str) -> ProjectState | None:
    """The project's saved state, or None if there is no such project (or it cannot be read)."""
    if not state_path(runs_dir, slug).exists():
        return None
    try:
        return load_state(runs_dir, slug)
    except (ValidationError, ValueError, OSError):
        return None


def list_projects(runs_dir: Path, config: Config) -> list[ProjectSummary]:
    """Every project that has a saved state, those needing attention first, then newest."""
    found: list[tuple[float, ProjectSummary]] = []
    for path in runs_dir.glob("*/state.json"):
        state = load_project(runs_dir, path.parent.name)
        if state is not None:
            found.append((path.stat().st_mtime, _summary(state, runs_dir, config)))
    found.sort(key=lambda item: (item[1].pending == 0, -item[0]))
    return [summary for _mtime, summary in found]


def _stage_state(state: ProjectState, stage: Stage) -> str:
    if state.status == Status.DONE:
        return "done"
    if state.stage == Stage.INTAKE:
        return "upcoming"
    order = {s: n for n, s in enumerate(STAGES)}
    if state.stage not in order:
        return "upcoming"
    if order[stage] < order[state.stage]:
        return "done"
    if stage == state.stage:
        return "waiting" if state.status == Status.AWAITING_HUMAN else "current"
    return "upcoming"


def _links(state: ProjectState, runs_dir: Path) -> list[Link]:
    store = ArtifactStore(runs_dir, state.project_slug)
    links: list[Link] = []

    def read_json(name: str) -> dict[str, str] | None:
        artifact = state.artifacts.get(name)
        if artifact is None:
            return None
        try:
            data: dict[str, str] = json.loads(store.read(artifact))
        except (ValueError, OSError):
            return None
        return data

    record = read_json("design_record.json")
    if record and record.get("pr_url"):
        links.append(Link("Design record", record["pr_url"]))
    for task_id in state.completed_tasks:
        dev = read_json(f"dev/{task_id}.json")
        if dev and dev.get("pr_url"):
            links.append(Link(f"Task {task_id}", dev["pr_url"]))
    published = read_json("career_published.json") or {}
    links += [Link(target, url) for target, url in published.items() if url.startswith("http")]
    return links


def project_view(runs_dir: Path, config: Config, slug: str) -> ProjectView | None:
    state = load_project(runs_dir, slug)
    if state is None:
        return None
    rows = [
        StageRow(
            stage=s,
            label=LABELS[s],
            state=_stage_state(state, s),
            spent=state.cost_usd.get(s, 0.0),
            cap=stage_cap(state, config, s),
        )
        for s in STAGES
    ]
    return ProjectView(
        summary=_summary(state, runs_dir, config),
        state=state,
        stages=rows,
        warn_fraction=config.budget.warn_at_fraction,
        gates=list(reversed(state.gate_history)),
        links=_links(state, runs_dir),
        artifacts=sorted(state.artifacts),
    )


def read_trace(
    runs_dir: Path, slug: str, *, event: EventType | None = None, limit: int = 300
) -> list[TraceEvent]:
    """The newest trace events first, optionally of one type. Malformed lines are skipped."""
    path = trace_path(runs_dir, slug)
    if not path.exists():
        return []
    events: list[TraceEvent] = []
    for line in path.read_text().splitlines():
        try:
            parsed = TraceEvent.model_validate_json(line)
        except ValidationError:
            continue
        if event is None or parsed.event == event:
            events.append(parsed)
    return list(reversed(events))[:limit]


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class BulletView:
    text: str
    evidence: list[tuple[str, str, str]]  # id, type, what the evidence says


@dataclass(frozen=True)
class ResumeChangeView:
    change: ResumeChange
    bullets: list[BulletView]
    latex: str  # paste-ready for the Overleaf resume


@dataclass(frozen=True)
class Workbench:
    """The career plan laid out for a decision: each claim beside the evidence behind it."""

    plan: CareerPlan
    changes: list[ResumeChangeView]
    website_evidence: list[tuple[str, str, str]]


def career_workbench(runs_dir: Path, state: ProjectState) -> Workbench | None:
    """The plan and its evidence for the career gate, or None if there is no plan to show."""
    plan_record = state.artifacts.get("career_plan.json")
    evidence_record = state.artifacts.get("evidence.json")
    if plan_record is None or evidence_record is None:
        return None
    store = ArtifactStore(runs_dir, state.project_slug)
    try:
        plan = CareerPlan.model_validate_json(store.read(plan_record))
        evidence = EvidenceSet.model_validate_json(store.read(evidence_record))
    except (ValidationError, ValueError, OSError):
        return None

    def cited(ids: list[str]) -> list[tuple[str, str, str]]:
        found = (evidence.get(i) for i in ids)
        return [(e.id, e.type.value, e.description) for e in found if e is not None]

    changes = [
        ResumeChangeView(
            change=c,
            bullets=[BulletView(b.text, cited(b.evidence_ids)) for b in c.bullets],
            latex=render_snippets([c]) if c.bullets else "",
        )
        for c in plan.resume_changes
    ]
    website = cited(plan.website_entry.evidence_ids) if plan.website_entry else []
    return Workbench(plan=plan, changes=changes, website_evidence=website)
