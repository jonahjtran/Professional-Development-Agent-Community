# A web UI for Agent Studio (sub-plan)

Status: draft for review. Nothing here is built yet.

## Goal

Run everything from a browser instead of the terminal: start a project, see where every project is,
and answer every question the pipeline asks (gates, merge checks, budget caps, tool permissions,
career items) in one place, with the evidence in front of you when you decide.

The UI is a **control panel over files the orchestrator already writes**, not a second
orchestrator. The state machine, the safety rules and the tests stay exactly where they are.

## What you can do in it, ranked by how much it helps you

**Must have**

1. **An inbox: "needs you".** One page listing every pending decision across all projects, newest
   first: a stage to review, PRs to confirm merged, a budget pause, a tool the Developer wants to
   run, a career plan to approve. Each opens a decision page with the rendered Markdown (the PRD,
   the architecture, the QA report), what changed since the last draft, the PR links, and the
   buttons: approve, request changes (with a box for what to change), reject. This is the part
   that replaces the terminal.
2. **A project page.** The stage timeline (done, current, waiting on you), money spent against the
   stage and project caps with the 80% line drawn, revisions used, every gate decision you made with
   your comments, and the pull requests it opened.
3. **Start a project.** A form: name, idea, track, private or public. It shows exactly what it will
   create on your GitHub account and asks you to confirm, like `studio new` does, then starts the run.
4. **Controls.** Halt (the kill switch), resume after a crash, raise a cap, abandon a project.
5. **Notifications.** A browser notification, and optionally a macOS one, when something needs
   you, because runs take a long time and you will not be watching the page.

**Should have**

6. **A career workbench.** The plan with the resume bullets next to the evidence behind each
   number, a copy button on each LaTeX snippet for Overleaf, a preview of the website entry, a
   yes/no switch per item, a box for each question the coach asks, and a list of the pull requests it
   opened with a reminder that nothing was merged for you.
7. **A health check.** One page that tells you what will break a run before it does: the API key
   and GitHub token present, how many days until the token expires (it lasts 30 days), `gh` logged
   in, `gitleaks` installed, the profile and website folders present and on your account, `main`
   protected.
8. **A trace viewer.** What each agent did: every model call with model, prompt version, tokens,
   cost and time; every tool call and every denial. This is how you learn what the agents actually
   do, and where the money goes.

**Could have, later**

9. **Eval history.** Charts from `evals/results.csv`: accuracy by prompt version, cost per run.
10. **Remote access** so you can approve from your phone (needs real authentication; see risks).
11. **Compare projects**: cost, revisions and QA outcomes side by side, the raw material for case
    studies.

## Decisions

| # | Decision | Recommendation | Why |
|---|----------|----------------|-----|
| U1 | How does the UI talk to the orchestrator? | **Through files.** A new set of backends writes each question to `runs/<project>/pending/<id>.json` and waits for `answers/<id>.json`. The UI shows pending files and writes answers. | The orchestrator already blocks on small interfaces (`GateBackend`, `CapBackend`, `MergeBackend`, the tool backend) and already saves state before every question. File-backed answers survive the UI restarting, leave the terminal backends working, and are easy to test. |
| U2 | Stack | **FastAPI, Jinja templates, HTMX, server-sent events.** No JavaScript build step. | One language and one process. The UI is forms and rendered text, not a rich app. FastAPI is already on your resume. |
| U3 | Where does it run? | `uv run studio ui` on `127.0.0.1` only, and it opens your browser. | Approving can publish pull requests and edit repository settings, so it must not be reachable from the network. |
| U4 | Do runs survive closing the UI? | Yes. A run is its own process that the UI starts detached; the UI watches its pid and a heartbeat file and offers "resume" if it died. | Closing a tab must not kill a three-hour run. Closing the laptop still stops it: that is not solved here. |
| U5 | New dependencies | `fastapi`, `uvicorn`, `jinja2`, `python-multipart`, `markdown-it-py`. | Needs your approval, per CLAUDE.md. |
| U6 | Notifications | The browser Notification API, plus `osascript` on macOS if you turn it on. | No account or service needed. |

## The protocol (U1)

```
runs/<project>/pending/<id>.json   written by the run: kind (gate, cap, merge, tool), the
                                   request as JSON, created time
runs/<project>/answers/<id>.json   written by the UI (or a terminal): the decision as JSON
runs/<project>/run.pid, heartbeat  written by the run, read by the UI
```

The run polls for the answer file, so answering from the CLI and the UI never disagree: the first
valid answer wins and the pending file is moved to `done/` with who answered, for the audit trail.
Answers are validated with the same models the terminal backends use (`GateDecision`,
`CapResolution`), so the UI cannot send something the orchestrator would not accept.

## PRs

All target `main`, merged in order.

1. **File-backed backends and `--ui`.** The four file backends, the pending and answers protocol,
   the pid and heartbeat, and a `--ui` flag on `studio run` and `studio resume` that selects them.
   No web code yet. Tests with real files and a fake human, including the crash and resume case.
2. **Read-only dashboard.** `studio ui`, the project list, the project page, artifact viewing with
   rendered Markdown, the gate history, the cost bars, PR links, and the trace viewer.
3. **Decisions and controls.** The inbox, the gate form (including per-item approvals and questions
   for the career gate), merge confirmation, budget pauses, tool permission asks, halt and resume,
   and notifications.
4. **Start a project and the health check.** The new-project form with its confirmation, launching
   the run detached, and the health page.
5. **Career workbench, then docs.** The evidence-linked plan view, copy buttons, website entry
   preview, PR results, and the CLAUDE.md and README sections.

## Done when

- You can run a whole project, from the idea to the career pull requests, without opening a terminal
  after `studio ui`.
- Every question the CLI asks has a page in the inbox, and an answer from the UI produces the same
  state as the same answer from the terminal.
- Killing the run and the UI at any moment, then restarting both, loses nothing: the question is
  still pending and answering it resumes the run.
- The server refuses connections from anywhere but the local machine, every form post is
  protected against cross-site requests, and no token or API key ever appears on a page or in a log.
- ruff, mypy and pytest are green on every PR, and a browser-level check confirms each page loads and
  each button reaches the right backend.

## Risks

- **A UI that can approve is a UI that can publish.** Bind to localhost, check the request's origin
  on every POST, and show exactly what an approval will do next to its button. Remote access (item
  10) waits for real authentication.
- **Two answers at once** (UI and terminal). The first valid one wins and the second is told it was
  too late.
- **Stale pages.** Pages that show a pending decision re-check that it is still pending before
  accepting an answer.
- **Secrets in the trace.** Pages render the same redacted trace the files hold, and tool inputs are
  shown only as the permission policy already records them.
- **Scope creep.** Items 9 to 11 stay out until the five PRs are done and you have used the UI on a
  real project.
