"""The web UI: a local control panel over a run directory.

It only listens on this machine (the server binds to 127.0.0.1 and the app rejects any other Host
header, which also blocks DNS-rebinding tricks). Pages here are read-only; they show what the
orchestrator already wrote to `runs/`.
"""

import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from orchestrator.artifacts import ArtifactStore
from orchestrator.config import Config
from orchestrator.trace import EventType
from orchestrator.ui import data
from orchestrator.ui.render import artifact_kind, markdown_html, pretty_json

HERE = Path(__file__).parent
LOCAL_HOSTS = ["127.0.0.1", "localhost", "testserver"]
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")  # the same rule as a project name in state.py


def create_app(runs_dir: Path, config: Config) -> FastAPI:
    app = FastAPI(title="Agent Studio", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")

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
        view = project_or_404(slug)
        return templates.TemplateResponse(request, "project.html", {"view": view})

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

    return app
