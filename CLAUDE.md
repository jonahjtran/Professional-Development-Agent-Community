# Agent Studio: conventions for Claude Code

Agent Studio is a Python system on the Claude Agent SDK that takes a project idea through
product definition, architecture, implementation, QA, and career documentation, with a human
approval gate after every stage. The full design is in
`Agent Studio Build Plan for Claude Code.md`. Read it before writing code.

## Commands

Use these exact commands (they match CI in `.github/workflows/ci.yml`):

```
uv sync                       # install dependencies
uv run pytest                 # tests (run one file: uv run pytest tests/test_config.py)
uv run ruff check .           # lint
uv run ruff format --check .  # format check (drop --check to apply)
uv run mypy                   # strict type check
uv run studio --help          # the CLI
```

`ruff`, `mypy` and `pytest` must all pass before opening a PR.

Live tests call the real Claude API and cost money, so they are skipped by default and never run
in CI. Run them on purpose with `ANTHROPIC_API_KEY=... uv run pytest tests/live --live -s`.

## Starting and running a project

Every project gets its own private repository. Secrets live in `.env` (git-ignored; load it with
`uv run --env-file .env ...`): `ANTHROPIC_API_KEY`, and `GITHUB_TOKEN` (fine-grained, Contents and
Pull requests read and write). Never put a token in an argument, a test assertion or a log.

```
uv run studio new <name>                     # create the private repo with YOUR gh login, clone it
uv run studio run "<idea>" --slug <name> --agent full --repo ~/Desktop/Github/<name>
uv run studio resume <name> --agent full     # continue after a crash; uses the recorded repo
uv run studio halt <name>                    # stop before the next step, and stop a running agent
```

`--agent echo` (free dummy), `api` (planning only) and `full` (planning, then the Developer opens a
PR per task) are the choices. `full` needs `--repo`; the project records it, and the orchestrator
only ever pushes `agent/` branches to that recorded repository. Worktrees live in
`~/.cache/agent-studio/worktrees` (override with `AGENT_STUDIO_WORKTREES`); projects are cloned to
`~/Desktop/Github` (override with `AGENT_STUDIO_PROJECTS`).

## What the Developer may do without asking

The Developer's tool calls go through `orchestrator/permissions.py`. Inside its own worktree it may
read and edit files, and run ordinary development commands, without asking. It asks you before
installs, deletions, unlisted commands and new environment variables, and it is refused outright
for secrets, pushing, leaving the worktree and `.github/` edits (which ask in project repos).
Three details that decide how many approvals you see:

- A quoted here-document that only feeds `cat` (`cat > path <<'EOF'`) is treated as a file write
  and checked like the Write tool. An unquoted one, or one feeding anything else, still asks.
- Text inside single quotes is literal; `$(...)`, backticks and `${...}` outside them (or inside
  double quotes) still ask.
- The sandbox may reach only the hosts in `sandbox.allowed_domains` in `config.yaml` (the npm and
  PyPI registries). Any other host is shown to you. This computer and private networks (localhost,
  `169.254.x.x`, `10.x`, `192.168.x`, ...) are never reachable, so the agent cannot call the web UI.

## QA

After the last Developer task is approved, merge its pull requests on GitHub. QA asks you to confirm
the merges, then runs the real tools (tests, lint, types, dependency audit, secret scan; set in
`config.yaml` under `qa:`) in a throwaway checkout of `main`. The QA agent only judges whether each
acceptance criterion has a test that proves it. `passed` is computed by rule: any failed or skipped
tool, or any `fail` verdict, fails the report; `untested` is shown but does not fail it. A failing
report goes to you at the gate, never back to the Developer automatically. Install `gitleaks`
(`brew install gitleaks`) or the secret scan is skipped and the report fails. QA live test:
`ANTHROPIC_API_KEY=... uv run pytest tests/live --live -s -k qa`.

## Web UI

`uv run --env-file .env studio ui` opens a local control panel (it listens on 127.0.0.1 only; add
`--mac-notify` for macOS notifications). From it you can start a project, see every project's
stage, spend against its caps and past decisions, read the documents and the trace, and answer
every question the pipeline asks (gates with per-item approvals, budget and revision caps, the
merge check, Developer tool requests) from one inbox. The career gate shows each resume bullet
next to its evidence, a copy button for each LaTeX snippet, and a preview of the website entry.
Halt and Resume are buttons on the project page, and `/health` checks the keys, token expiry,
tools and folders before a run.

The frontend is a React app (TypeScript, Vite, react-router, styled after Apple's Human
Interface Guidelines: system font, grouped lists, large titles, translucent bars, light and dark).
It lives in `frontend/` and builds into `orchestrator/ui/static/app/` (git-ignored), which the
server serves. Build it once before `studio ui`, and again after changing it:

```
npm --prefix frontend ci                 # if npm complains about ~/.npm permissions, add --cache /tmp/npm-cache
npm --prefix frontend run build
npm --prefix frontend run dev            # live reload on :5173, proxying /api to a running `studio ui`
npm --prefix frontend test               # vitest + Testing Library
npm --prefix frontend run typecheck
```

The server is a JSON API (`/api/...`, in `orchestrator/ui/app.py`). `studio run --ui` and
`studio resume --ui` (the UI uses them) answer through files: a question is written to
`runs/<project>/pending/<id>.json`; an answer to `runs/<project>/answers/<id>.json` is validated
with the same models the terminal uses, then the question moves to `done/` with who answered. A run
keeps `run.json` fresh with a heartbeat so the UI can tell a live run from a dead one. The terminal
still works without `--ui`.

Rules that must keep holding: the server only binds to 127.0.0.1 and rejects any other Host
header; every change needs the session token in `X-CSRF-Token`, a JSON body and a matching Origin;
agent-written text is escaped by the server (Markdown without raw HTML) before it is sent, and the
React app only uses `dangerouslySetInnerHTML` for those fields; only documents a project recorded
can be read; nothing outside the built app folder is ever served; no secret value is ever put in
a response. Approving can publish pull requests, so never expose the UI to a network without
adding real authentication first.

## Career coach

After QA is approved the coach turns the finished project into career material. Everything it says
must trace to evidence built by code from the project's real artifacts (merged PRs, the QA report,
spend, your approvals, eval results); a number that is not in a cited measured evidence item is
rejected before you see it. It can say a project does not belong on your resume, and it asks you
questions instead of guessing facts about you (your answers are saved as evidence).

Setup, once: a private `career-profile` clone holding `resume.yaml` (a structured copy of the
resume with a page budget per section) and `goals.yaml` (target roles and firms), and these in
`.env`: `AGENT_STUDIO_PROFILE=~/Desktop/Github/career-profile`, and optionally
`AGENT_STUDIO_WEBSITE=~/Desktop/Github/personal-portfolio`. A full run refuses to start if the
profile is missing, and if the profile, website and project repos are not all on your account.

At the career gate you approve or skip each item separately: the README, the case study, each
resume change, the website entry, and the GitHub description and topics. Each approved item
becomes a pull request, never a merge: the project README and case study (project repo), the
resume proposal with paste-ready LaTeX snippets for Overleaf (your private profile repo), and the
website entry in `src/data/projects.js` (the site's own build must pass first; merging deploys it).
The description and topics use your own `gh` login. The resume itself is never edited: you paste
the snippets into Overleaf. A resume change must name what it replaces, because the resume is one
page. Live test (about twenty cents, publishes nothing):
`ANTHROPIC_API_KEY=... uv run pytest tests/live --live -s -k career`.

## Evals

`uv run --env-file .env python -m evals.run --agent qa|career_coach [--prompt-version v1] [--tier fast] [--runs 3]`
runs the real QA agent on 12 tiny projects with known-right verdicts (`evals/qa_cases.py`) and
appends a row to `evals/results.csv`: accuracy, false passes (the dangerous error: calling an
unproven criterion pass), reports correct, cost, latency. Cost is a cent or two. Change one thing
at a time (prompt version or model tier), rerun, and compare rows. Add a case whenever a real run
shows the agent getting something wrong. The career coach has its own set of five invented projects (`evals/career_cases.py`), scored on
honesty (no unbacked numbers), fit versus skip, website entry only when warranted, and not
repeating text planted in the inputs. There are evals for QA and the career coach only so far.

The end-to-end live test creates and deletes a real repository on your account, so it needs its own
switch: `AGENT_STUDIO_LIVE_E2E=1 uv run --env-file .env pytest tests/live --live -s -k end_to_end`.

## Working rules

- Work one phase at a time from the build plan, and stop after each phase for human review.
- **TDD.** Follow `.claude/skills/tdd/SKILL.md`: write a failing test first, confirm it fails
  for the right reason, write the minimum code to pass, then refactor. Use this repo's commands,
  not defaults. Agent prompts are tested through evals, not unit tests. Pure config and docs
  changes don't need tests.
- Use type hints everywhere. mypy runs in strict mode.
- Do not guess Agent SDK or MCP SDK APIs. Read the current official docs for exact names and
  parameters, and note the SDK version in `pyproject.toml`.
- **Ask before:** changing a schema, adding a dependency not listed in the plan, touching
  credentials or GitHub tokens, or changing a gate's behavior.
- Never commit secrets. Keep model names and budgets in `config.yaml`, not in code.
- Keep prompts in `agents/prompts/` as versioned files, and record the prompt version in every
  trace event.

## Git and PR workflow

- Never commit to `main`. One branch and one small PR per concern, branched from an up-to-date
  `main`. Prefixes: `feat/`, `fix/`, `chore/`, `ci/`, `docs/`, `refactor/`, `test/`.
- Conventional commit titles: `type(scope): imperative summary`.
- Show the PR body for approval before opening the PR. Each body states what was built, how it
  was verified, and anything deferred.
- A PR is not done until acceptance criteria are met, tests are added, CI is green, and docs or
  this file are updated if conventions changed.

## Private learning notes

`docs/learning/` is the developer's private notebook (the `learn-as-you-build` skill). It is
git-ignored. Never stage, commit, or push anything in it.

## Layout

The layout is fixed in the build plan ("Repo layout and tech stack"). Directories are added as
the phases need them. Agents work in a git worktree of the target repository (this repo by default) on an
`agent/<task-id>` branch, never in your checkout. The orchestrator, not the agent, pushes and opens
the PR. When the target is this repo, the agent cannot edit its control files (see
`orchestrator/worktree.py`).
