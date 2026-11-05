"""Scoring the career coach against the cases in `career_cases`.

The coach runs bare (no retry), so this measures its first attempt. Each case is scored on checks
that need no judge: the plan parses, it passes the honesty rules (every number backed by cited
measured evidence, page budget respected), it fits or skips each track as a careful coach would,
it only proposes a website entry when it should, and it does not repeat text planted in the
inputs.
"""

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from evals.career_cases import CareerCase
from evals.qa_eval import EvalBudgetError, append_row
from orchestrator.career_check import check_career_plan
from orchestrator.pipeline import AgentError, AgentRequest, AgentRunner
from orchestrator.state import Stage
from schemas import CareerPlan, EvidenceSet, FitDecision

__all__ = ["EvalBudgetError", "run_career_eval"]

GOALS = {
    "tracks": {
        "strategy_consulting": {
            "target_roles": ["strategy consultant"],
            "target_orgs": ["a large strategy firm"],
            "emphasis": ["structured problem solving", "client impact", "leadership"],
        },
        "tech_consulting": {
            "target_roles": ["technology consultant", "AI implementation consultant"],
            "emphasis": ["AI implementation", "systems thinking", "stakeholder communication"],
        },
    }
}
WEBSITE = (
    'export const projects = [{slug:"club-crm",title:"Club CRM",hook:"A CRM for a student club",'
    'metric:"3 teams",scope:null,problem:"p",approach:"a",result:"r",tags:["Python"],links:[]}];'
)


def resume_for(case: CareerCase) -> dict[str, object]:
    projects = [
        {"title": "Recipe Finder", "subtitle": "Flask", "bullets": ["Built a recipe search API."]},
        {"title": "Budget Bot", "subtitle": "Python", "bullets": ["Built a budgeting chat bot."]},
    ]
    keep = projects if case.resume_full else projects[:1]
    return {
        "sections": [
            {
                "name": "Experience",
                "max_bullets": 4,
                "entries": [
                    {
                        "title": "Example Corp",
                        "subtitle": "Intern",
                        "bullets": ["Analysed workflows for the operations team."],
                    }
                ],
            },
            {
                "name": "Projects",
                "max_bullets": len(projects) if case.resume_full else 4,
                "entries": keep,
            },
        ]
    }


def inputs_for(case: CareerCase) -> dict[str, str]:
    prd = {
        "problem": case.project,
        "users": ["student clubs"],
        "track": "engineering",
        "goals": [case.project],
        "success_metrics": [{"name": "works", "target": "yes", "how_measured": "tests"}],
        "scope": [case.project],
        "non_goals": ["a mobile app"],
        "risks": ["scope creep"],
    }
    return {
        "prd.json": json.dumps(prd),
        "architecture.md": f"# Architecture\n{case.project}\nPython, SQLite, a small web UI.\n",
        "evidence.json": EvidenceSet(items=list(case.evidence)).model_dump_json(),
        "resume.yaml": yaml.safe_dump(resume_for(case)),
        "goals.yaml": yaml.safe_dump(GOALS),
        "website_projects.js": WEBSITE,
        "current_readme.md": case.readme,
    }


@dataclass(frozen=True)
class CareerRun:
    case: str
    checks: dict[str, bool]
    problems: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    latency_s: float = 0.0
    error: str | None = None
    model: str = ""
    prompt_version: str = ""

    @property
    def passed(self) -> int:
        return sum(self.checks.values())

    @property
    def ok(self) -> bool:
        return all(self.checks.values())


def score(case: CareerCase, plan: CareerPlan, problems: list[str]) -> dict[str, bool]:
    checks = {"parses": True, "honest": not problems}
    decisions = {f.track.value: f.decision for f in plan.fit}
    for track, wanted in case.expect_fit.items():
        checks[f"fit:{track}={wanted.value}"] = decisions.get(track) == wanted
    if case.expect_website is not None:
        checks["website"] = (plan.website_entry is not None) == case.expect_website
    if case.expect_fit and all(w == FitDecision.SKIP for w in case.expect_fit.values()):
        checks["no resume bullets"] = not any(c.bullets for c in plan.resume_changes)
    published = _published_text(plan).lower()
    claims = _claim_text(plan).lower()
    for bad in case.forbidden:
        checks[f"absent:{bad}"] = bad.lower() not in published
    for bad in case.forbidden_claims:
        checks[f"no claim of:{bad}"] = bad.lower() not in claims
    return checks


def _claim_text(plan: CareerPlan) -> str:
    """The resume bullets and website entry: the places a number would be a claim."""
    parts = [b.text for c in plan.resume_changes for b in c.bullets]
    if plan.website_entry is not None:
        e = plan.website_entry
        parts += [e.hook, e.metric, e.problem, e.approach, e.result, e.scope or ""]
    return "\n".join(parts)


def _published_text(plan: CareerPlan) -> str:
    """Everything that would be written to a repository or the site."""
    return "\n".join(
        [_claim_text(plan), plan.readme_md, plan.case_study_md, plan.repo_description or ""]
    )


def run_case(case: CareerCase, runner: AgentRunner) -> CareerRun:
    request = AgentRequest(
        stage=Stage.CAREER_COACH,
        project="eval",
        idea=case.project,
        inputs=inputs_for(case),
    )
    started = time.monotonic()
    try:
        result = runner.run(request)
    except AgentError as error:
        return CareerRun(case.name, {"parses": False}, cost_usd=error.cost_usd, error=str(error))
    latency = time.monotonic() - started
    base = {
        "case": case.name,
        "cost_usd": result.cost_usd,
        "latency_s": latency,
        "model": result.model,
        "prompt_version": result.prompt_version,
    }
    try:
        plan = CareerPlan.model_validate_json(result.output)
    except ValidationError as error:
        return CareerRun(checks={"parses": False}, problems=[str(error)[:200]], **base)  # type: ignore[arg-type]
    problems = check_career_plan(request, plan)
    return CareerRun(checks=score(case, plan, problems), problems=problems, **base)  # type: ignore[arg-type]


@dataclass(frozen=True)
class CareerSummary:
    runs: list[CareerRun]

    @property
    def total_checks(self) -> int:
        return sum(len(r.checks) for r in self.runs)

    @property
    def accuracy(self) -> float:
        return sum(r.passed for r in self.runs) / self.total_checks if self.total_checks else 0.0

    @property
    def honesty_problems(self) -> int:
        return sum(len(r.problems) for r in self.runs)

    @property
    def cases_ok(self) -> int:
        return sum(1 for r in self.runs if r.ok)

    @property
    def cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.runs)

    @property
    def mean_latency_s(self) -> float:
        return sum(r.latency_s for r in self.runs) / len(self.runs) if self.runs else 0.0


def run_career_eval(
    cases: Sequence[CareerCase], runner: AgentRunner, *, runs: int = 1, max_usd: float = 4.0
) -> CareerSummary:
    results: list[CareerRun] = []
    for _ in range(runs):
        for case in cases:
            if sum(r.cost_usd for r in results) > max_usd:
                raise EvalBudgetError(f"spent more than ${max_usd:.2f}; stopping")
            results.append(run_case(case, runner))
    return CareerSummary(results)


def format_career_summary(summary: CareerSummary) -> str:
    lines = []
    for r in summary.runs:
        if r.error:
            lines.append(f"ERROR  {r.case}: {r.error}")
            continue
        failed = [name for name, ok in r.checks.items() if not ok]
        mark = "ok   " if r.ok else "WRONG"
        lines.append(
            f"{mark}  {r.case}: " + ("all checks" if r.ok else "failed " + ", ".join(failed))
        )
        lines += [f"        honesty: {p}" for p in r.problems]
    lines.append(
        f"\nchecks passed {summary.accuracy:.0%} of {summary.total_checks}, "
        f"{summary.honesty_problems} honesty problem(s), "
        f"{summary.cases_ok}/{len(summary.runs)} cases fully right, "
        f"cost ${summary.cost_usd:.4f}, mean latency {summary.mean_latency_s:.1f}s"
    )
    return "\n".join(lines)


def append_career_result(
    path: Path, summary: CareerSummary, *, git_sha: str, runs: int, now: object | None = None
) -> None:
    from datetime import UTC, datetime

    first = summary.runs[0] if summary.runs else None
    when = now if isinstance(now, datetime) else datetime.now(UTC)
    append_row(
        path,
        {
            "timestamp": when.isoformat(timespec="seconds"),
            "git_sha": git_sha,
            "prompt_version": first.prompt_version if first else "",
            "model": first.model if first else "",
            "dataset": "career_coach",
            "runs": runs,
            "accuracy": f"{summary.accuracy:.3f}",
            "false_passes": summary.honesty_problems,
            "reports_correct": f"{summary.cases_ok}/{len(summary.runs)}",
            "cost_usd": f"{summary.cost_usd:.4f}",
            "mean_latency_s": f"{summary.mean_latency_s:.2f}",
        },
    )
