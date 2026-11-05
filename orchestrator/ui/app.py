"""The web UI: a local control panel over a run directory.

It only listens on this machine (the server binds to 127.0.0.1 and the app rejects any other Host
header, which also blocks DNS-rebinding tricks). Pages here are read-only; they show what the
orchestrator already wrote to `runs/`.
"""

import re
import secrets
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from orchestrator.artifacts import ArtifactStore
from orchestrator.config import Config
from orchestrator.halt import request_halt
from orchestrator.inbox import (
    AnswerRejected,
    Pending,
    all_pending,
    answer,
    answered,
    pending_for,
    rejection_reason,
    run_is_alive,
)
from orchestrator.newproject import (
    NewProject,
    NewProjectError,
    create_project,
    default_projects_dir,
)
from orchestrator.state import ProjectState, Status, state_path
from orchestrator.trace import EventType
from orchestrator.ui import data
from orchestrator.ui import health as health_checks
from orchestrator.ui.decisions import FormError, describe, payload_from_form
from orchestrator.ui.render import artifact_kind, markdown_html, pretty_json
from schemas import Track

HERE = Path(__file__).parent
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


def create_app(
    runs_dir: Path,
    config: Config,
    *,
    config_path: Path = Path("config.yaml"),
    launcher: Launcher = launch_detached,
    projects_dir: Path | None = None,
    creator: Creator = create_project,
    health: HealthRunner | None = None,
) -> FastAPI:
    app = FastAPI(title="Agent Studio", docs_url=None, redoc_url=None, openapi_url=None)
    csrf_token = secrets.token_urlsafe(32)
    clones = projects_dir or default_projects_dir()
    run_health = health or (
        lambda: health_checks.run_checks(repo_root=config_path.resolve().parent)
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["csrf"] = csrf_token
    templates.env.globals["describe"] = describe

    async def checked_form(request: Request) -> Any:
        """The submitted form, after proving the post came from this site's own pages."""
        origin = request.headers.get("origin")
        if origin is not None and origin.split("://", 1)[-1] != request.headers.get("host"):
            raise HTTPException(403, "cross-site request refused")
        form = await request.form()
        sent = form.get("csrf")
        if not isinstance(sent, str) or not secrets.compare_digest(sent, csrf_token):
            raise HTTPException(403, "missing or wrong form token")
        return form

    def message(request: Request, text: str, back: str, status: int = 200) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "message.html", {"text": text, "back": back}, status_code=status
        )

    def project_or_404(slug: str) -> data.ProjectView:
        if not _SLUG.match(slug):
            raise HTTPException(404, "no such project")
        view = data.project_view(runs_dir, config, slug)
        if view is None:
            raise HTTPException(404, "no such project")
        return view

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        projects = data.list_projects(runs_dir, config)
        return templates.TemplateResponse(request, "index.html", {"projects": projects})

    @app.get("/p/{slug}", response_class=HTMLResponse)
    def project(request: Request, slug: str) -> HTMLResponse:
        log = runs_dir / slug / "run.log"
        if _SLUG.match(slug) and not state_path(runs_dir, slug).exists() and log.exists():
            tail = "\n".join(log.read_text(errors="replace").splitlines()[-20:])
            return templates.TemplateResponse(
                request, "starting.html", {"slug": slug, "tail": tail}
            )
        view = project_or_404(slug)
        return templates.TemplateResponse(
            request, "project.html", {"view": view, "pending": pending_for(runs_dir, slug)}
        )

    @app.get("/p/{slug}/a/{name:path}", response_class=HTMLResponse)
    def artifact(request: Request, slug: str, name: str) -> HTMLResponse:
        view = project_or_404(slug)
        record = view.state.artifacts.get(name)  # only names the project recorded, never a path
        if record is None:
            raise HTTPException(404, "no such artifact")
        text = ArtifactStore(runs_dir, slug).read(record)
        kind = artifact_kind(name)
        body = markdown_html(text) if kind == "markdown" else pretty_json(text)
        return templates.TemplateResponse(
            request,
            "artifact.html",
            {"view": view, "name": name, "record": record, "kind": kind, "body": body},
        )

    @app.get("/p/{slug}/trace", response_class=HTMLResponse)
    def trace(request: Request, slug: str, type: str | None = None) -> HTMLResponse:
        view = project_or_404(slug)
        try:
            wanted = EventType(type) if type else None
        except ValueError:
            raise HTTPException(404, "no such event type") from None
        events = data.read_trace(runs_dir, slug, event=wanted)
        return templates.TemplateResponse(
            request,
            "trace.html",
            {"view": view, "events": events, "types": list(EventType), "selected": wanted},
        )

    # --- decisions -------------------------------------------------------------------------

    def pending_or_404(slug: str, question_id: str) -> Pending:
        project_or_404(slug)
        found = next((p for p in pending_for(runs_dir, slug) if p.id == question_id), None)
        if found is None:
            raise HTTPException(404, "this question is no longer open")
        return found

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "inbox.html", {"items": all_pending(runs_dir)})

    @app.get("/api/pending")
    def api_pending() -> JSONResponse:
        return JSONResponse(
            [
                {
                    "id": p.id,
                    "project": p.project,
                    "title": describe(p)[0],
                    "url": f"/p/{p.project}/q/{p.id}",
                }
                for p in all_pending(runs_dir)
            ]
        )

    @app.get("/p/{slug}/q/{question_id}", response_class=HTMLResponse)
    def question(request: Request, slug: str, question_id: str) -> HTMLResponse:
        view = project_or_404(slug)
        try:
            pending = pending_or_404(slug, question_id)
        except HTTPException:
            done = next((d for d in answered(runs_dir, slug) if d["id"] == question_id), None)
            if done is None:
                raise
            return message(request, "This question was already answered.", f"/p/{slug}")
        summary = pending.request.get("summary")
        return templates.TemplateResponse(
            request,
            "question.html",
            {
                "view": view,
                "q": pending,
                "summary_html": markdown_html(summary) if isinstance(summary, str) else "",
                "refused": rejection_reason(runs_dir, slug, question_id),
            },
        )

    @app.post("/p/{slug}/q/{question_id}")
    async def decide(request: Request, slug: str, question_id: str) -> Response:
        form = await checked_form(request)
        try:
            pending = pending_or_404(slug, question_id)
        except HTTPException:
            return message(
                request,
                "This question is no longer open: it was answered, or the run moved on.",
                "/inbox",
                409,
            )
        try:
            payload = payload_from_form(pending, form, [str(v) for v in form.getlist("items")])
        except FormError as error:
            return message(request, str(error), f"/p/{slug}/q/{question_id}", 400)
        try:
            answer(runs_dir, slug, question_id, payload)
        except AnswerRejected as error:
            return message(request, str(error), "/inbox", 409)
        return RedirectResponse("/inbox", status_code=303)

    # --- controls --------------------------------------------------------------------------

    @app.post("/p/{slug}/halt")
    async def halt(request: Request, slug: str) -> Response:
        await checked_form(request)
        view = project_or_404(slug)
        if view.state.status in (Status.DONE, Status.HALTED):
            return message(request, f"{slug} is already {view.state.status.value}.", f"/p/{slug}")
        request_halt(runs_dir, slug)
        return RedirectResponse(f"/p/{slug}", status_code=303)

    @app.post("/p/{slug}/resume")
    async def resume(request: Request, slug: str) -> Response:
        await checked_form(request)
        view = project_or_404(slug)
        state = view.state
        if state.status not in (Status.RUNNING, Status.AWAITING_HUMAN):
            return message(
                request, f"{slug} is {state.status.value}; nothing to resume.", f"/p/{slug}"
            )
        if run_is_alive(runs_dir, slug):
            return message(request, f"{slug} is already running.", f"/p/{slug}")
        command = resume_command(state, config_path, runs_dir)
        if command is None:
            return message(
                request,
                "This project did not record how it was started, so resume it from the terminal.",
                f"/p/{slug}",
                409,
            )
        launcher(command, config_path.resolve().parent, runs_dir / slug / "run.log")
        return RedirectResponse(f"/p/{slug}", status_code=303)

    # --- starting a project ----------------------------------------------------------------

    def problems_with(form: Any) -> tuple[dict[str, str], list[str]]:
        fields = {
            "name": str(form.get("name", "")).strip(),
            "idea": str(form.get("idea", "")).strip(),
            "track": str(form.get("track", "engineering")),
            "agent": str(form.get("agent", "full")),
            "visibility": str(form.get("visibility", "private")),
            "public_ok": "on" if form.get("public_ok") == "on" else "",
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
        if (
            fields["visibility"] == "public"
            and fields["agent"] == "full"
            and not fields["public_ok"]
        ):
            problems.append("Tick the box to confirm you want a public repository.")
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

    @app.get("/new", response_class=HTMLResponse)
    def new_form(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "new.html", {"fields": {}, "problems": [], "tracks": list(Track)}
        )

    @app.post("/new/preview", response_class=HTMLResponse)
    async def new_preview(request: Request) -> HTMLResponse:
        form = await checked_form(request)
        fields, problems = problems_with(form)
        if problems:
            return templates.TemplateResponse(
                request,
                "new.html",
                {"fields": fields, "problems": problems, "tracks": list(Track)},
                status_code=400,
            )
        return templates.TemplateResponse(
            request, "confirm_new.html", {"f": fields, "clone": clones / fields["name"]}
        )

    @app.post("/new/start")
    async def new_start(request: Request) -> Response:
        form = await checked_form(request)
        fields, problems = problems_with(form)
        if problems:
            return message(request, " ".join(problems), "/new", 400)
        name, agent = fields["name"], fields["agent"]
        repo: Path | None = None
        if agent == "full":
            try:
                created = creator(
                    name, clones, public=fields["visibility"] == "public", confirm=lambda _q: True
                )
            except NewProjectError as error:
                return message(request, str(error), "/new", 409)
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
        return RedirectResponse(f"/p/{name}", status_code=303)

    @app.get("/health", response_class=HTMLResponse)
    def health_page(request: Request) -> HTMLResponse:
        checks = run_health()
        return templates.TemplateResponse(
            request, "health.html", {"checks": checks, "failing": health_checks.blocking(checks)}
        )

    return app
