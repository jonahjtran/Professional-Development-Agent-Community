"""Pydantic models for every artifact an agent hands to the orchestrator."""

from schemas.architect_output import ArchitectOutput
from schemas.architecture import ADR, Architecture, Component, TechChoice
from schemas.career import (
    CareerPlan,
    CareerTrack,
    Evidence,
    EvidenceSet,
    EvidenceType,
    FitDecision,
    Link,
    ResumeAction,
    ResumeBullet,
    ResumeChange,
    TrackFit,
    WebsiteEntry,
)
from schemas.dev_result import DevReport, DevResult
from schemas.fix_plan import FixPlan
from schemas.next_step import NextStep
from schemas.prd import PRD, SuccessMetric, Track
from schemas.profile import Goals, ResumeData, ResumeEntry, ResumeSection, TrackGoal
from schemas.route import Route
from schemas.tasks import Size, Task, TaskList, unmapped_metrics
from schemas.test_report import (
    CheckResult,
    CheckStatus,
    CriterionVerdict,
    QaAssessment,
    QaReport,
    Verdict,
    passes,
)

__all__ = [
    "NextStep",
    "ADR",
    "PRD",
    "Route",
    "ArchitectOutput",
    "Architecture",
    "CareerPlan",
    "CareerTrack",
    "CheckResult",
    "CheckStatus",
    "Component",
    "CriterionVerdict",
    "DevReport",
    "DevResult",
    "FixPlan",
    "Evidence",
    "EvidenceSet",
    "EvidenceType",
    "FitDecision",
    "Goals",
    "Link",
    "QaAssessment",
    "QaReport",
    "ResumeAction",
    "ResumeBullet",
    "ResumeChange",
    "ResumeData",
    "ResumeEntry",
    "ResumeSection",
    "Size",
    "SuccessMetric",
    "Task",
    "TaskList",
    "TechChoice",
    "Track",
    "TrackFit",
    "TrackGoal",
    "Verdict",
    "WebsiteEntry",
    "passes",
    "unmapped_metrics",
]
