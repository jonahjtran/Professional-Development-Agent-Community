"""The web UI's server: a JSON API over a run directory, plus the React app that uses it.

It only listens on this machine (the server binds to 127.0.0.1 and the app rejects any other Host
header, which also blocks DNS-rebinding tricks). Reading is open to the local page; every change
needs the session token in a header, a JSON body and a matching Origin, so no other website the
browser has open can make it do anything. Agent-written text is escaped before it is sent.
"""

import json
import re
import secrets
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from orchestrator.activity import project_activity
from orchestrator.artifacts import ArtifactStore
from orchestrator.assistant_cost import assistant_totals
from orchestrator.config import Config, LocalConfig
from orchestrator.developer_backend import (
    LocalStatus,
    check_local,
    developer_model,
    get_default_backend,
    get_developer_backend,
    set_global_backend,
    set_project_backend,
)
from orchestrator.explain import Explainer, ExplainInput, ExplainResult
from orchestrator.halt import request_halt
from orchestrator.inbox import (
    AnswerRejected,
    PendingKind,
    all_pending,
    answer,
    answered,
    pending_for,
    rejection_reason,
    run_is_alive,
)
from orchestrator.models_view import agent_models
from orchestrator.newproject import (
    NewProject,
    NewProjectError,
    create_project,
    default_projects_dir,
)
from orchestrator.notes import add_note
from orchestrator.permissions import PolicyContext
from orchestrator.project_settings import AUTONOMY_LEVELS, get_autonomy, set_autonomy
from orchestrator.proposals import Proposal, get_proposal, list_proposals, set_status
from orchestrator.rule_options import RuleOptions, rule_options
from orchestrator.rules import (
    Rule,
    describe,
    global_store,
    project_store,
)
from orchestrator.state import ProjectState, Status, state_path
from orchestrator.todos import Todo, all_open_todos, mark_done, open_todos
from orchestrator.trace import EventType
from orchestrator.ui import data, serialize
from orchestrator.ui import health as health_checks
from orchestrator.ui.chat import ChatService
from orchestrator.ui.decisions import FormError, payload_from_form
from orchestrator.ui.render import artifact_kind, markdown_html, pretty_json
from schemas import Track

HERE = Path(__file__).parent
FRONTEND = HERE / "static" / "app"  # the built React app (npm run build in frontend/)
LOCAL_HOSTS = ["127.0.0.1", "localhost", "testserver"]
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")  # the same rule as a project name in state.py


Launcher = Callable[[list[str], Path, Path], None]
"""Starts a detached process: (command, working folder, log file)."""


def launch_detached(command: list[str], cwd: Path, log: Path) -> None:
    """Start a run that keeps going after the UI is closed."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as out:
        subprocess.Popen(  # noqa: S603
            command,
            cwd=cwd,
            stdout=out,
            stderr=out,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )


def resume_command(state: ProjectState, config_path: Path, runs_dir: Path) -> list[str] | None:
    """The command that resumes a project, or None if its agent kind was never recorded."""
    if state.agent is None:
        return None
    return [
        sys.executable, "-c", "from orchestrator.cli import app; app()",
        "resume", state.project_slug, "--agent", state.agent, "--ui",
        "--config", str(config_path.resolve()), "--runs-dir", str(runs_dir.resolve()),
    ]  # fmt: skip


class Chatting(Protocol):
    def repos(self) -> list[dict[str, str]]: ...

    def turn(
        self,
        message: str,
        *,
        session_id: str | None,
        repo: str,
        tier: str,
        context: dict[str, Any] | None,
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def close(self) -> None: ...


class Explaining(Protocol):
    def explain(self, item: ExplainInput) -> ExplainResult: ...


Creator = Callable[..., NewProject]
HealthRunner = Callable[[], list[health_checks.Check]]
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")
_MIN_IDEA, _MAX_IDEA = 10, 2000


def start_command(
    *,
    idea: str,
    slug: str,
    agent: str,
    track: str,
    repo: Path | None,
    config_path: Path,
    runs_dir: Path,
) -> list[str]:
    command = [
        sys.executable, "-c", "from orchestrator.cli import app; app()",
        "run", idea, "--slug", slug, "--agent", agent, "--track", track, "--ui",
        "--config", str(config_path.resolve()), "--runs-dir", str(runs_dir.resolve()),
    ]  # fmt: skip
    return command + (["--repo", str(repo)] if repo is not None else [])


_NOT_BUILT = """<!doctype html><meta charset="utf-8"><title>Agent Studio</title>
<body style="font:17px system-ui;max-width:640px;margin:15vh auto;padding:0 20px">
<h1>The web app is not built yet</h1>
<p>Run this once, then reload:</p>
<pre>npm --prefix frontend ci &amp;&amp; npm --prefix frontend run build</pre></body>"""


def create_app(
    runs_dir: Path,
    config: Config,
    *,
    config_path: Path = Path("config.yaml"),
    launcher: Launcher = launch_detached,
    projects_dir: Path | None = None,
    creator: Creator = create_project,
    health: HealthRunner | None = None,
    frontend_dir: Path = FRONTEND,
    explainer: Explaining | None = None,
    chat: Chatting | None = None,
    local_check: Callable[[LocalConfig], LocalStatus] = check_local,
) -> FastAPI:
    app = FastAPI(title="Agent Studio", docs_url=None, redoc_url=None, openapi_url=None)
    csrf_token = secrets.token_urlsafe(32)
    clones = projects_dir or default_projects_dir()
    run_health = health or (
        lambda: health_checks.run_checks(repo_root=config_path.resolve().parent)
    )
    explain_with = explainer or Explainer(config, runs_dir)
    chatting: Chatting = chat or ChatService(
        runs_dir,
        config,
        explain_with,
        studio_root=config_path.resolve().parent,
        projects_dir=clones,
    )
    app.router.on_shutdown.append(chatting.close)
    quick_checks = health or (
        lambda: health_checks.run_checks(
            repo_root=config_path.resolve().parent, fetch=None, network=False
        )
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException) -> JSONResponse:
        return JSONResponse({"error": error.detail}, status_code=error.status_code)

    async def checked_body(request: Request) -> dict[str, Any]:
        """The JSON body of a change, after proving it came from this site's own page."""
        origin = request.headers.get("origin")
        if origin is not None and origin.split("://", 1)[-1] != request.headers.get("host"):
            raise HTTPException(403, "cross-site request refused")
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(415, "send JSON")
        sent = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(sent, csrf_token):
            raise HTTPException(403, "missing or wrong session token")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "the body is not valid JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "the body must be a JSON object")
        return body

    def fail(status: int, text: str) -> JSONResponse:
        return JSONResponse({"error": text}, status_code=status)

    def view_or_404(slug: str) -> data.ProjectView:
        if not _SLUG.match(slug):
            raise HTTPException(404, "no such project")
        view = data.project_view(runs_dir, config, slug)
        if view is None:
            raise HTTPException(404, "no such project")
        return view

    # --- reading ---------------------------------------------------------------------------

    @app.get("/api/session")
    def session() -> dict[str, str]:
        return {"csrf": csrf_token}

    @app.get("/api/projects")
    def projects() -> list[dict[str, Any]]:
        return [serialize.summary(p) for p in data.list_projects(runs_dir, config)]

    @app.get("/api/projects/{slug}")
    def project(slug: str) -> dict[str, Any]:
        log = runs_dir / slug / "run.log"
        if _SLUG.match(slug) and not state_path(runs_dir, slug).exists() and log.exists():
            tail = "\n".join(log.read_text(errors="replace").splitlines()[-20:])
            return {"starting": True, "slug": slug, "log": tail}
        shown = serialize.project(view_or_404(slug), pending_for(runs_dir, slug))
        shown["helper"] = assistant_totals(runs_dir, slug)  # kept apart from the project's spend
        shown["autonomy"] = get_autonomy(runs_dir, slug)
        choice = get_developer_backend(runs_dir, slug)
        shown["developer"] = {
            "backend": choice.backend,
            "source": choice.source,
            "model": developer_model(choice.backend, config),
        }
        shown["todos"] = [todo_view(t) for t in open_todos(runs_dir, slug)]
        return shown

    @app.get("/api/projects/{slug}/activity")
    def activity(slug: str) -> dict[str, Any]:
        """Who is doing what, derived from the saved state and the trace (no model call)."""
        view = view_or_404(slug)
        people = project_activity(runs_dir, view.state, alive=view.summary.alive)
        return {"agents": [asdict(p) for p in people]}

    @app.get("/api/projects/{slug}/artifacts/{name:path}")
    def artifact(slug: str, name: str) -> dict[str, Any]:
        view = view_or_404(slug)
        record = view.state.artifacts.get(name)  # only names the project recorded, never a path
        if record is None:
            raise HTTPException(404, "no such artifact")
        text = ArtifactStore(runs_dir, slug).read(record)
        kind = artifact_kind(name)
        return {
            "name": name,
            "revision": record.revision,
            "approved_at": record.approved_at.isoformat() if record.approved_at else None,
            "kind": kind,
            "html": markdown_html(text) if kind == "markdown" else None,
            "text": pretty_json(text) if kind == "code" else None,
        }

    @app.get("/api/projects/{slug}/trace")
    def trace(slug: str, type: str | None = None) -> dict[str, Any]:
        view_or_404(slug)
        try:
            wanted = EventType(type) if type else None
        except ValueError:
            raise HTTPException(404, "no such event type") from None
        events = data.read_trace(runs_dir, slug, event=wanted)
        return {
            "types": [t.value for t in EventType],
            "selected": wanted.value if wanted else None,
            "events": [json.loads(e.model_dump_json()) for e in events],
        }

    @app.get("/api/inbox")
    def inbox() -> list[dict[str, Any]]:
        return [serialize.pending_item(p) for p in all_pending(runs_dir)]

    @app.get("/api/projects/{slug}/questions/{question_id}")
    def question(slug: str, question_id: str) -> dict[str, Any]:
        view = view_or_404(slug)
        found = next((p for p in pending_for(runs_dir, slug) if p.id == question_id), None)
        if found is None:
            if any(d["id"] == question_id for d in answered(runs_dir, slug)):
                return {"status": "answered", "id": question_id, "project": slug}
            raise HTTPException(404, "this question is no longer open")
        workbench = (
            data.career_workbench(runs_dir, view.state)
            if found.kind == PendingKind.GATE and found.request.get("stage") == "career_coach"
            else None
        )
        return serialize.question(found, rejection_reason(runs_dir, slug, question_id), workbench)

    @app.get("/api/projects/{slug}/questions/{question_id}/explanation")
    def explanation(slug: str, question_id: str) -> dict[str, Any]:
        """A plain-English note on a tool request. Advice only; fetched by the page, not the run."""
        view = view_or_404(slug)
        found = next((p for p in pending_for(runs_dir, slug) if p.id == question_id), None)
        if found is None or found.kind != PendingKind.TOOL:
            raise HTTPException(404, "there is no open tool request with that id")
        request = found.request
        result = explain_with.explain(
            ExplainInput(
                project=slug,
                question_id=question_id,
                tool=str(request.get("tool", "")),
                summary=str(request.get("summary", "")),
                reason=str(request.get("reason", "")),
                idea=view.state.idea,
                stage=view.state.stage.value,
            )
        )
        return {
            "explanation": result.explanation.model_dump() if result.explanation else None,
            "error": result.error,
            "cost_usd": result.cost_usd,
        }

    def options_for(slug: str, request: dict[str, Any]) -> RuleOptions:
        """What may be remembered for a request, worked out here and never taken from the page."""
        stand_in = PolicyContext(worktree=runs_dir.resolve() / slug / "worktree")
        tool, summary = str(request.get("tool", "")), str(request.get("summary", ""))
        return rule_options(tool, summary, stand_in)

    def rule_view(rule: Rule) -> dict[str, Any]:
        return {
            "id": rule.id,
            "description": describe(rule),
            "uses": rule.uses,
            "last_used": rule.last_used.isoformat() if rule.last_used else None,
        }

    @app.get("/api/projects/{slug}/questions/{question_id}/rule-options")
    def rule_choices(slug: str, question_id: str) -> dict[str, Any]:
        view_or_404(slug)
        found = next((p for p in pending_for(runs_dir, slug) if p.id == question_id), None)
        if found is None or found.kind != PendingKind.TOOL:
            raise HTTPException(404, "there is no open tool request with that id")
        options = options_for(slug, found.request)
        return {
            "available": options.available,
            "reason": options.reason,
            "parts": [
                {
                    "label": part.label,
                    "exact": part.exact,
                    "any_label": " ".join(part.any_prefix) + " …any arguments"
                    if part.any_prefix
                    else None,
                }
                for part in options.parts
            ],
        }

    def remember(slug: str, request: dict[str, Any], fields: dict[str, Any]) -> None:
        """Save the rules the human chose, after validating the choice against fresh options."""
        scope = str(fields.get("remember", ""))
        if scope not in ("project", "global"):
            return
        options = options_for(slug, request)
        if not options.available:
            return
        store = project_store(runs_dir, slug) if scope == "project" else global_store()
        modes = str(fields.get("rule_modes", "")).split(",")
        for index, part in enumerate(options.parts):
            wide = index < len(modes) and modes[index] == "any" and part.any_prefix is not None
            try:
                if wide:
                    assert part.any_prefix is not None
                    store.add(part.any_prefix, any_args=True)
                else:
                    store.add(part.exact, any_args=False)
            except ValueError:
                continue

    @app.get("/api/rules")
    def rules_list() -> dict[str, Any]:
        slugs = sorted(v.slug for v in data.list_projects(runs_dir, config))
        return {
            "global": [rule_view(r) for r in global_store().load()],
            "projects": {
                s: [rule_view(r) for r in project_store(runs_dir, s).load()] for s in slugs
            },
        }

    @app.post("/api/rules/revoke")
    async def rules_revoke(request: Request) -> JSONResponse:
        body = await checked_body(request)
        scope, rule_id = str(body.get("scope", "")), str(body.get("id", ""))
        if scope == "global":
            store = global_store()
        elif scope == "project":
            slug = str(body.get("project", ""))
            view_or_404(slug)
            store = project_store(runs_dir, slug)
        else:
            return fail(400, "scope must be global or project")
        if not store.revoke(rule_id):
            return fail(404, "no such rule")
        return JSONResponse({"ok": True})

    @app.post("/api/projects/{slug}/autonomy")
    async def autonomy(request: Request, slug: str) -> JSONResponse:
        body = await checked_body(request)
        view_or_404(slug)
        level = str(body.get("level", ""))
        if level not in AUTONOMY_LEVELS:
            return fail(400, f"level must be one of {', '.join(AUTONOMY_LEVELS)}")
        set_autonomy(runs_dir, slug, level)
        return JSONResponse({"ok": True})

    def local_health() -> health_checks.Check | None:
        """A health check for the local model, only when something is set to use it."""
        in_use = get_default_backend().backend == "local" or any(
            get_developer_backend(runs_dir, p.slug).backend == "local"
            for p in data.list_projects(runs_dir, config)
        )
        if not in_use:
            return None
        status = local_check(config.local)
        return (
            health_checks.Check("Local model (Ollama)", health_checks.OK, status.detail)
            if status.ok
            else health_checks.Check(
                "Local model (Ollama)", health_checks.FAIL, status.detail, status.fix
            )
        )

    def with_local(runner: Callable[[], list[health_checks.Check]]) -> Callable[[], list[Any]]:
        def run() -> list[Any]:
            extra = local_health()
            return [*runner(), *([extra] if extra else [])]

        return run

    @app.get("/api/models")
    def models() -> dict[str, Any]:
        default = get_default_backend()
        status = local_check(config.local)
        return {
            "developer": {
                "default": default.backend,
                "claude_model": config.models.developer,
                "local_model": config.local.model,
                "endpoint": config.local.endpoint,
            },
            "agents": [asdict(a) for a in agent_models(config, default.backend)],
            "local": {
                "ok": status.ok,
                "reachable": status.reachable,
                "installed": status.installed,
                "context": status.context,
                "detail": status.detail,
                "fix": status.fix,
            },
        }

    @app.post("/api/models/developer")
    async def set_default_backend(request: Request) -> JSONResponse:
        body = await checked_body(request)
        try:
            set_global_backend(str(body.get("backend", "")))
        except ValueError as error:
            return fail(400, str(error))
        return JSONResponse({"ok": True})

    @app.post("/api/projects/{slug}/developer-backend")
    async def set_project_developer(request: Request, slug: str) -> JSONResponse:
        body = await checked_body(request)
        view_or_404(slug)
        choice = str(body.get("backend", ""))
        try:
            set_project_backend(runs_dir, slug, None if choice == "default" else choice)
        except ValueError as error:
            return fail(400, str(error))
        return JSONResponse({"ok": True})

    def todo_view(todo: Todo) -> dict[str, Any]:
        return {
            "id": todo.id,
            "project": todo.project,
            "title": todo.title,
            "detail": todo.detail,
            "source": todo.source,
            "created": todo.created.isoformat(),
            "can_mark_done": True,
        }

    @app.get("/api/todos")
    def todos() -> dict[str, Any]:
        """Things only you can do: raised by agents and the pipeline, plus failing health checks."""
        items = [todo_view(t) for t in all_open_todos(runs_dir)]
        for check in health_checks.blocking(with_local(quick_checks)()):
            items.append(
                {
                    "id": f"system-{check.name}",
                    "project": None,
                    "title": check.name,
                    "detail": f"{check.detail} {check.fix}".strip(),
                    "source": "system",
                    "created": None,
                    "can_mark_done": False,
                }
            )
        return {"todos": items}

    @app.post("/api/projects/{slug}/todos/{todo_id}/done")
    async def todo_done(request: Request, slug: str, todo_id: str) -> JSONResponse:
        await checked_body(request)
        view_or_404(slug)
        if not mark_done(runs_dir, slug, todo_id):
            return fail(404, "no such open item")
        return JSONResponse({"ok": True})

    @app.get("/api/health")
    def health_report() -> dict[str, Any]:
        checks = with_local(run_health)()
        return {
            "checks": [serialize.check(c) for c in checks],
            "failing": len(health_checks.blocking(checks)),
        }

    # --- deciding --------------------------------------------------------------------------

    def apply_answer(
        slug: str, question_id: str, fields: dict[str, Any], items: list[str]
    ) -> tuple[int, str | None]:
        """Answer an open question exactly as the form does. (status, error message or None)."""
        found = next((p for p in pending_for(runs_dir, slug) if p.id == question_id), None)
        if found is None:
            return 409, "This question is no longer open: it was answered, or the run moved on."
        try:
            payload = payload_from_form(found, fields, items)
        except FormError as error:
            return 400, str(error)
        try:
            answer(runs_dir, slug, question_id, payload)
        except AnswerRejected as error:
            return 409, str(error)
        if found.kind == PendingKind.TOOL and payload.get("allow") is True:
            remember(slug, found.request, fields)
        return 200, None

    @app.post("/api/projects/{slug}/questions/{question_id}")
    async def decide(request: Request, slug: str, question_id: str) -> JSONResponse:
        body = await checked_body(request)
        view_or_404(slug)
        fields = body.get("fields", {})
        items = body.get("items", [])
        if not isinstance(fields, dict) or not isinstance(items, list):
            return fail(400, "fields must be an object and items a list")
        status, error = apply_answer(slug, question_id, fields, [str(i) for i in items])
        if error is not None:
            return fail(status, error)
        return JSONResponse({"ok": True})

    @app.post("/api/projects/{slug}/pause")
    async def pause(request: Request, slug: str) -> JSONResponse:
        await checked_body(request)
        view = view_or_404(slug)
        if view.state.status == Status.DONE:
            return fail(409, f"{slug} is already done.")
        if view.state.status == Status.HALTED:
            return fail(409, f"{slug} is already paused.")
        request_halt(runs_dir, slug)
        return JSONResponse({"ok": True})

    def start_resume(slug: str, state: ProjectState) -> tuple[int, str | None]:
        if state.status == Status.DONE:
            return 409, f"{slug} is done; nothing to resume."
        if run_is_alive(runs_dir, slug):
            return 409, f"{slug} is already running."
        command = resume_command(state, config_path, runs_dir)
        if command is None:
            return (
                409,
                "This project did not record how it was started, so resume it from the terminal.",
            )
        launcher(command, config_path.resolve().parent, runs_dir / slug / "run.log")
        return 200, None

    @app.post("/api/projects/{slug}/resume")
    async def resume(request: Request, slug: str) -> JSONResponse:
        await checked_body(request)
        state = view_or_404(slug).state
        status, error = start_resume(slug, state)
        if error is not None:
            return fail(status, error)
        return JSONResponse({"ok": True})

    # --- the chat --------------------------------------------------------------------------

    @app.get("/api/chat/repos")
    def chat_repos() -> dict[str, Any]:
        return {"repos": chatting.repos(), "cap_usd": config.assistant.max_chat_usd}

    @app.post("/api/chat")
    async def chat_turn(request: Request) -> StreamingResponse:
        body = await checked_body(request)
        message = body.get("message")
        repo = body.get("repo")
        tier = body.get("tier", "strong")
        context = body.get("context")
        session_id = body.get("session_id")
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 4000:
            raise HTTPException(400, "write a message of 1 to 4000 characters")
        if not isinstance(repo, str) or tier not in ("strong", "fast"):
            raise HTTPException(400, "choose a repository, and a strong or fast model")
        if context is not None and not isinstance(context, dict):
            raise HTTPException(400, "context must be an object")
        if session_id is not None and not isinstance(session_id, str):
            raise HTTPException(400, "session_id must be a string")

        async def events() -> AsyncIterator[str]:
            async for event in chatting.turn(
                message.strip(), session_id=session_id, repo=repo, tier=tier, context=context
            ):
                yield f"data: {json.dumps(event)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    # --- proposals from the chat -----------------------------------------------------------

    def proposal_view(p: Proposal) -> dict[str, Any]:
        return {
            "id": p.id,
            "project": p.project,
            "action": p.action,
            "summary": p.summary,
            "status": p.status,
            "result": p.result,
            "created": p.created.isoformat(),
        }

    @app.get("/api/proposals")
    def proposals_list(project: str | None = None, status: str | None = None) -> dict[str, Any]:
        if project is not None:
            view_or_404(project)
        wanted: Any = status if status in ("pending", "confirmed", "dismissed", "failed") else None
        found = list_proposals(runs_dir, project=project, status=wanted)
        found.sort(key=lambda p: p.created, reverse=True)
        return {"proposals": [proposal_view(p) for p in found]}

    def run_proposal(p: Proposal) -> tuple[int, str]:
        """Carry out a confirmed proposal. (HTTP status, a message for the human)."""
        view = view_or_404(p.project)
        if p.action == "answer_question":
            status, error = apply_answer(
                p.project,
                str(p.args.get("question_id", "")),
                dict(p.args.get("fields", {})),
                [str(i) for i in p.args.get("items", [])],
            )
            return status, error or "Answered."
        if p.action == "resume_project":
            status, error = start_resume(p.project, view.state)
            return status, error or "Resuming."
        if p.action == "set_autonomy":
            level = str(p.args.get("level", ""))
            if level not in AUTONOMY_LEVELS:
                return 400, "unknown autonomy level"
            set_autonomy(runs_dir, p.project, level)
            return 200, f"Autonomy set to {level}."
        if p.action == "leave_note":
            try:
                add_note(runs_dir, p.project, str(p.args.get("text", "")))
            except ValueError as error:
                return 400, str(error)
            return 200, "The Developer will see this note on its next task."
        return 400, "unknown action"

    @app.post("/api/projects/{slug}/proposals/{proposal_id}/confirm")
    async def proposal_confirm(request: Request, slug: str, proposal_id: str) -> JSONResponse:
        await checked_body(request)
        view_or_404(slug)
        proposal = get_proposal(runs_dir, slug, proposal_id)
        if proposal is None or proposal.status != "pending":
            return fail(409, "This proposal was already decided.")
        status, message = run_proposal(proposal)
        if status != 200:
            set_status(runs_dir, slug, proposal_id, "failed", message)
            return fail(status, message)
        set_status(runs_dir, slug, proposal_id, "confirmed", message)
        return JSONResponse({"ok": True, "result": message})

    @app.post("/api/projects/{slug}/proposals/{proposal_id}/dismiss")
    async def proposal_dismiss(request: Request, slug: str, proposal_id: str) -> JSONResponse:
        await checked_body(request)
        view_or_404(slug)
        if set_status(runs_dir, slug, proposal_id, "dismissed", "Dismissed.") is None:
            return fail(409, "This proposal was already decided.")
        return JSONResponse({"ok": True})

    # --- starting a project ----------------------------------------------------------------

    def problems_with(body: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
        fields = {
            "name": str(body.get("name", "")).strip(),
            "idea": str(body.get("idea", "")).strip(),
            "track": str(body.get("track", "engineering")),
            "agent": str(body.get("agent", "full")),
            "visibility": str(body.get("visibility", "private")),
        }
        problems: list[str] = []
        if not _NAME.match(fields["name"]):
            problems.append("The name must be lowercase letters, digits and hyphens (max 60).")
        elif state_path(runs_dir, fields["name"]).exists():
            problems.append(f"A project called {fields['name']} already exists.")
        elif fields["agent"] == "full" and (clones / fields["name"]).exists():
            problems.append(f"{clones / fields['name']} already exists on this computer.")
        if not _MIN_IDEA <= len(fields["idea"]) <= _MAX_IDEA:
            problems.append(f"Describe the idea in {_MIN_IDEA} to {_MAX_IDEA} characters.")
        if fields["track"] not in {t.value for t in Track}:
            problems.append("Choose a track.")
        if fields["agent"] not in {"full", "echo"}:
            problems.append("Choose how to run it.")
        if fields["visibility"] not in {"private", "public"}:
            problems.append("Choose private or public.")
        if fields["agent"] == "full":
            problems += [
                f"{c.name}: {c.detail} {c.fix}".strip()
                for c in health_checks.blocking(
                    health_checks.run_checks(
                        repo_root=config_path.resolve().parent, fetch=None, network=False
                    )
                )
                if c.name in {"Anthropic API key", "GitHub token", "Career profile"}
            ]
        return fields, problems

    @app.post("/api/new/preview")
    async def new_preview(request: Request) -> JSONResponse:
        fields, problems = problems_with(await checked_body(request))
        if problems:
            return JSONResponse({"ok": False, "problems": problems, "fields": fields}, 400)
        return JSONResponse({"ok": True, "fields": fields, "clone": str(clones / fields["name"])})

    @app.post("/api/new/start")
    async def new_start(request: Request) -> JSONResponse:
        fields, problems = problems_with(await checked_body(request))
        if problems:
            return JSONResponse({"ok": False, "problems": problems, "fields": fields}, 400)
        name, agent = fields["name"], fields["agent"]
        repo: Path | None = None
        if agent == "full":
            try:
                created = creator(
                    name, clones, public=fields["visibility"] == "public", confirm=lambda _q: True
                )
            except NewProjectError as error:
                return fail(409, str(error))
            repo = created.path
        command = start_command(
            idea=fields["idea"],
            slug=name,
            agent=agent,
            track=fields["track"],
            repo=repo,
            config_path=config_path,
            runs_dir=runs_dir,
        )
        launcher(command, config_path.resolve().parent, runs_dir / name / "run.log")
        return JSONResponse({"ok": True, "slug": name})

    # --- the React app ---------------------------------------------------------------------

    index = frontend_dir / "index.html"
    if (frontend_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=frontend_dir / "assets"), name="assets")

    @app.get("/{path:path}", response_model=None)
    def spa(path: str) -> FileResponse | HTMLResponse:
        if path.startswith("api/"):
            raise HTTPException(404, "no such endpoint")
        candidate = (frontend_dir / path).resolve()
        if path and candidate.is_file() and frontend_dir.resolve() in candidate.parents:
            return FileResponse(candidate)  # favicon and the like, never outside the app folder
        if index.exists():
            return FileResponse(index)
        return HTMLResponse(_NOT_BUILT, status_code=503)

    return app
