"""Turning what the UI reads into plain JSON for the React app. Nothing here reads files."""

from typing import Any

from orchestrator.inbox import Pending
from orchestrator.ui import data
from orchestrator.ui.decisions import describe
from orchestrator.ui.health import Check
from orchestrator.ui.render import markdown_html


def summary(s: data.ProjectSummary) -> dict[str, Any]:
    return {
        "slug": s.slug,
        "idea": s.idea,
        "stage": s.stage.value,
        "status": s.status.value,
        "detail": s.detail,
        "cost_usd": s.cost_usd,
        "cap_usd": s.cap_usd,
        "pending": s.pending,
        "alive": s.alive,
    }


def pending_item(p: Pending) -> dict[str, Any]:
    title, detail = describe(p)
    return {
        "id": p.id,
        "project": p.project,
        "kind": p.kind.value,
        "title": title,
        "detail": detail,
        "created": p.created.isoformat(),
    }


def project(view: data.ProjectView, pending: list[Pending]) -> dict[str, Any]:
    return {
        "starting": False,
        "summary": summary(view.summary),
        "stages": [
            {
                "stage": r.stage.value,
                "label": r.label,
                "state": r.state,
                "spent": r.spent,
                "cap": r.cap,
                "fraction": r.fraction,
            }
            for r in view.stages
        ],
        "warn_fraction": view.warn_fraction,
        "gates": [
            {
                "ts": g.ts.isoformat(),
                "stage": g.stage.value,
                "outcome": g.outcome.value,
                "comments": g.comments,
            }
            for g in view.gates
        ],
        "links": [{"label": link.label, "url": link.url} for link in view.links],
        "artifacts": view.artifacts,
        "pending": [pending_item(p) for p in pending],
    }


def check(c: Check) -> dict[str, str]:
    return {"name": c.name, "status": c.status, "detail": c.detail, "fix": c.fix}


def _evidence(rows: list[tuple[str, str, str]]) -> list[dict[str, str]]:
    return [{"id": i, "type": t, "description": d} for i, t, d in rows]


def workbench(wb: data.Workbench) -> dict[str, Any]:
    plan = wb.plan
    entry = plan.website_entry
    return {
        "fit": [
            {"track": f.track.value, "decision": f.decision.value, "reason": f.reason}
            for f in plan.fit
        ],
        "changes": [
            {
                "action": c.change.action.value,
                "section": c.change.section,
                "entry": c.change.entry,
                "reason": c.change.reason,
                "replaces": c.change.replaces,
                "bullets": [{"text": b.text, "evidence": _evidence(b.evidence)} for b in c.bullets],
                "latex": c.latex,
            }
            for c in wb.changes
        ],
        "website": None
        if entry is None
        else {
            "title": entry.title,
            "slug": entry.slug,
            "hook": entry.hook,
            "metric": entry.metric,
            "scope": entry.scope,
            "problem": entry.problem,
            "approach": entry.approach,
            "result": entry.result,
            "tags": entry.tags,
            "evidence": _evidence(wb.website_evidence),
        },
        "website_reason": plan.website_reason,
        "readme_html": markdown_html(plan.readme_md),
        "case_study_html": markdown_html(plan.case_study_md),
    }


def question(p: Pending, refused: str | None, wb: data.Workbench | None) -> dict[str, Any]:
    summary_text = p.request.get("summary")
    title, detail = describe(p)
    return {
        "status": "open",
        "id": p.id,
        "project": p.project,
        "kind": p.kind.value,
        "title": title,
        "detail": detail,
        "request": p.request,
        "summary_html": markdown_html(summary_text) if isinstance(summary_text, str) else "",
        "refused": refused,
        "workbench": workbench(wb) if wb else None,
    }
