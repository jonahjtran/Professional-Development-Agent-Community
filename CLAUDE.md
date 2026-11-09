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

## The journey is not a straight line

**The orchestrator chooses the agents.** The five agents are what the orchestrator has available;
it decides which are needed, what each is told and given, and whether any runs again. There is
no required order and no required loop. After each agent finishes (and at the start), the
**Director** (`orchestrator/director.py`, prompt `agents/prompts/director.v1.md`, one call on the
strong tier, cost in the helper ledger as `director_usd`, not in any agent's cap) reads the idea, the
journey so far and what exists, and answers with a `NextStep`: run any agent (skipping ahead, or
repeating one for a stated reason) or finish; a `reason` shown in the journey; a `note` for the next
agent ("the Product Owner was skipped, keep it small"); and the approved `documents` to hand it. Code
checks the answer against rules the model cannot override: not the agent that just finished; the
Developer needs the approved task list; QA needs the tasks built; the Career coach needs QA's report
when QA runs real tools; a project cannot finish with tasks open; only approved documents can be handed
on; and after `limits.max_moves` (40) visits it stops choosing. If the call fails or an answer breaks a
rule, the usual order (PO, Architect, Developer, QA, Career coach) is followed and the trace says why.
Your approval of each agent's work still gates it. Echo (practice) runs use the usual order. An agent
that was skipped leaves its documents missing, so the Architect and Career coach take only the
documents that exist, and the design record leaves the PRD out. Live check (cents):
`ANTHROPIC_API_KEY=... uv run pytest tests/live --live -s -k director`.

The orchestrator can move back as well as forward
(`orchestrator/journey.py`): to **any agent the Director chooses**, and back to **any earlier agent**
(QA to the Architect, the Developer to the Architect, and so on; never back to intake). Every move
goes through one function (`_goto` in `pipeline.py`), which refuses an illegal move, saves the move
in `state.path` (each entry: the agent, why, when, and where it came from) and traces it with its
direction. Because an agent can be called back, **no agent is "done" until the project is**:

- an agent that has finished shows as **finished**; the project's agents show **done** only when the
  whole project is complete;
- an agent that has been visited more than once shows **↻ round N**; one the project went back to
  shows **↩ and the reason** ("QA failed: fix round 1"); an agent after the current one that has run
  before shows **will run again**;
- the project page shows the **journey** (every agent visited, moves back marked) and a notice while
  the project is back at an earlier agent; the chat's `project_status` includes the same line.

A project saved before this existed has no path; it is read as having passed each earlier agent once.

**You talk to the coordinator, not to an agent, at any time the project is paused or finished.**
`uv run --env-file .env studio send-back <project> --comment "..."` reopens it with your
instruction (a finished project becomes running again), then `studio resume <project>` continues
it. The **coordinator** (`orchestrator/coordinator.py`, prompt `agents/prompts/coordinator.v1.md`,
one call on the strong tier, cost in the helper ledger as `coordinator_usd`) reads your request and
the project's documents and picks the agent, with a reason in plain words and your request restated
for that agent (your own words are kept). It may only choose an agent the project has reached; a
big or vague request ("make it pass the PRD") goes to the **Architect**, one concrete small edit to
the **Developer**, changed requirements to the **Product Owner**. If the call fails or answers
badly the request goes to the Architect (who turns any request into tasks) and says so. Naming an
agent yourself (`--to product_owner|architect|developer|qa|career_coach`) skips the coordinator.
What each agent does with it: the **Product Owner** and
**Career coach** revise their own document with your comment and you review it at the gate (after
a changed PRD the Architect plans only the tasks the change needs); the **Architect** turns your
instruction into new tasks (`R<round>-<n>`, appended; finished tasks stay finished); the
**Developer** builds your instruction directly as a new task (`H<n>-1`); **QA** runs again. A project
cannot be sent forward past where it has got to, and one that is running must be paused first. The
open questions of the old run are put aside (`runs/<project>/stale/`), not answered. The change is
kept as `state.rework` until the project finishes again. Career material is published again when the
Career coach is approved again.

In the web UI it is the **Change something** card on the project page: write what should change and
**Send back and continue**; the coordinator decides (the default), and the page says where it went
and why. You can pick an agent instead (only those the project has reached). It is shown for
a paused or finished project (a running one says to pause first); it reopens the project and starts
the run again, so it spends credits. The chat can propose the same (`propose_send_back`, the agent is optional), and it
happens when you confirm; the coordinator chooses then. Live check (cents):
`ANTHROPIC_API_KEY=... uv run pytest tests/live --live -s -k coordinator`.

**Going back after a failure.** The rules below are still code, not a model's choice; the Director
decides the ordinary moves (above).

- QA fails and the failure is in the code: back to the **Architect** for a fix plan (above).
- The **Developer** reports a problem with the design or task list it cannot solve inside a task
  (`needs_architect` in its report, from prompt `developer.v5`): when you approve that task, the
  project goes **back to the Architect** (`agents/prompts/architect_replan.v1.md`), at most
  `limits.max_replans` (2) times. The Architect returns new tasks (ids `R<round>-<n>`, appended to the
  task list and built by the Developer), or answers that the design stands (the Developer gets that as
  a note on its next task, and you are not alerted), or hands a choice only you can make to Action
  needed. If it cannot plan, or the limit is used, the problem goes to you as an action item.
- The **Developer** cannot start a task and makes no commits, but says why in its report
  (`needs_architect`): the project goes **back to the Architect** at once, with the task's own
  text, instead of pausing (same `max_replans` limit; past it, or with no Architect available, it
  pauses and shows what the Developer said). A task you added by sending the project back (`H…`)
  that got stuck this way is taken out of the task list, so the Architect's new tasks replace it and
  it cannot sit first and block them. Tasks the Architect planned are never removed.
- Every such visit is checked against the Architect's own budget, saved with a `handoff` so a pause
  or crash resumes into it, and shown in the journey.

## Starting and running a project

Every project gets its own private repository. Secrets live in `.env` (git-ignored; load it with
`uv run --env-file .env ...`): `ANTHROPIC_API_KEY`, and `GITHUB_TOKEN` (fine-grained, Contents and
Pull requests read and write). Never put a token in an argument, a test assertion or a log.

```
uv run studio new <name>                     # create the private repo with YOUR gh login, clone it
uv run studio run "<idea>" --slug <name> --agent full --repo ~/Desktop/Github/<name>
uv run studio resume <name> --agent full     # continue after a crash; uses the recorded repo
uv run studio pause <name>                   # stop spending: pauses before the next step, stops a running agent
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
- `npm test` and `npm run test|typecheck|build|lint|check|format`, and `npx vitest|tsc|eslint|jest|prettier`,
  run without asking. `npm install`, `npm ci`, other scripts and other `npx` packages still ask.
- **Always allow.** When you approve a command you can remember it for this project or everywhere,
  exactly or as a prefix with any arguments (never for installs, publishing or programs that run
  other code). Rules live in `runs/<project>/rules.json` and `~/.config/agent-studio/rules.json`
  (override with `AGENT_STUDIO_HOME`), are listed on the Rules page, and can be revoked there. A rule
  is applied last, so it can never allow anything the policy refuses or asks about for a special
  reason (secrets, push, paths outside the worktree, `sudo`, shell expansion...). Each project also
  has an autonomy level: *cautious* ignores rules, *standard* (default) uses them, *autonomous*
  also allows `npm ci` and a bare `npm install` when a lockfile exists.
- The sandbox may reach only the hosts in `sandbox.allowed_domains` in `config.yaml` (the npm and
  PyPI registries). Any other host is shown to you. This computer and private networks (localhost,
  `169.254.x.x`, `10.x`, `192.168.x`, ...) are never reachable, so the agent cannot call the web UI.

## The Developer's model

The Developer runs inside Claude Code's harness whichever model drives it, so its tools, permission
policy and sandbox are identical. Two backends, chosen **per project** (overrides) or **globally**,
and read at the start of every task, so a switch applies from the next task on:

- **Claude** (default): `models.developer` in `config.yaml`, currently `claude-sonnet-5-5` (the
  planning agents, QA, the career coach and the chat stay on `models.strong`, Opus 5.5).
- **Local**: an open-source model served by Ollama (`local:` in `config.yaml`: endpoint, model tag,
  `min_context`). Claude Code is pointed at Ollama's Anthropic-compatible endpoint with a placeholder
  token, the real Anthropic key is blanked, and every model name Claude Code might ask for is set to
  the local model. A local task costs $0 and never counts against the caps. Before a local task starts,
  Studio checks that Ollama answers, the model is installed and (if loaded) its context is at least
  `min_context`; if not, the project pauses with the exact command to fix it. Start Ollama with
  `OLLAMA_CONTEXT_LENGTH=65536 ollama serve` and `ollama pull <the tag>`.

**Who pays for the Developer** (`orchestrator/developer_billing.py`). Separate from which model it
runs: *API credits* (default; the Developer's Claude Code is given your `ANTHROPIC_API_KEY`) or *My
subscription*, where that key is blanked for the Developer's process only, so Claude Code uses the
Claude account you signed in with (`claude auth login`, or a token from `claude setup-token` in
`CLAUDE_CODE_OAUTH_TOKEN`). Per project (overrides) or global, read at every task start, like the
model switch. Before each subscription task Studio runs `claude auth status` with the key blanked
(it keeps only signed-in and the method, never the account details) and pauses with the exact fix
if you are not signed in or Claude Code would still use an API key; if the check cannot run it
does not block. A plan failure (sign-in, usage limit, plan or billing) pauses with a plain message.
Under a subscription the dollar figures are estimates and the caps still apply, as a guard on how
much of your plan a project may use; a local model task ignores the setting. Only the Developer
(and the Agent SDK chat, unchanged) can use a plan: the planning agents, QA, the Career coach and
the helper calls use the Messages API, which needs a key. Anthropic's terms allow a subscription
for ordinary individual use of Claude Code and the Agent SDK and ask developers building products
for other people to use API keys, so keep this to your own projects. Live check (one tiny message
from your plan, a fake key in the environment on purpose):
`ANTHROPIC_API_KEY=sk-ant-fake uv run pytest tests/live --live -s -k subscription`.

**Settings menu.** Models (`/models`) and Rules (`/rules`) are reached from the profile button at the
right of the top bar, not the main bar. There is no sign-in (the UI listens on this computer only),
so the menu has no logout.

The **Models** page (`/models`) shows every agent's exact model id, switches the default Developer
backend, and shows whether Ollama is ready (with the fix when it is not). Each project page shows
which model its Developer uses and why, with a switch (follow the default, Claude, or the local
model). Health lists the local model only when something is set to use it.
The Team page shows each agent's exact model id under its name, marked local when it is the
open-source model and "running now" (the Developer's current task), "used last" (what the API
reported) or "will use" (configured, not yet used). The chat can propose switching a project's
Developer model; it applies once you confirm, from the next task.

## Skills the Developer follows

The Developer works in a project's own worktree and loads no settings files, so it cannot see this
repo's `.claude` folder. The skills listed under `developer.skills` in `config.yaml` (today: `tdd`,
from `.claude/skills/tdd/SKILL.md`) are read when each task starts and added to its instructions,
after the role prompt, for both the Claude and the local backend. They are told to discover each
project's own test commands rather than assume ours. A skill named in `config.yaml` that cannot be
read stops the task before it starts, with the path.

## QA

After the last Developer task is approved, merge its pull requests on GitHub. QA asks you to confirm
the merges, then runs the real tools (tests, lint, types, dependency audit, secret scan; set in
`config.yaml` under `qa:`) in a throwaway checkout of `main`. The QA agent only judges whether each
acceptance criterion has a test that proves it. `passed` is computed by rule: any failed or skipped
tool, or any `fail` verdict, fails the report; `untested` is shown but does not fail it. Install
`gitleaks` (`brew install gitleaks`) or the secret scan is skipped and the report fails. QA live
test: `ANTHROPIC_API_KEY=... uv run pytest tests/live --live -s -k qa`.

**When QA fails, the project fixes itself, for up to `limits.max_qa_loops` rounds (2).** The
orchestrator moves **back to the Architect** (a real visit: the project's stage becomes the Architect,
and the journey shows QA → ↩ Architect → Developer → QA) and gives it the failed report, the
architecture and the task list
(`agents/prompts/qa_fix_planner.v1.md`, the Architect's model tier and budget). It returns a fix plan:
a short analysis, new tasks for the Developer (ids `F<round>-<n>`, added to the end of
`tasks.json`, acceptance criteria that a test proves) and anything only you can do (these become
Action needed items). The **Developer** builds each fix task like any other: one pull request per
task, and you still review and merge each. When they are merged, QA runs again. It stops looping
and brings the report to you at the QA gate when QA passes, when the rounds are used up, or when the
Architect finds nothing the Developer can fix (for example a tool that is not installed). A planner
failure or an invalid plan changes nothing: the project returns to QA and shows you the report. `max_qa_loops: 0` restores
"a failing report goes straight to you". The plan is kept as `qa_fix_plan_<n>.md` and shown in the
Architect's notes on the Team page. Live check (a few cents):
`ANTHROPIC_API_KEY=... uv run pytest tests/live --live -s -k fix_planner`.

## Budget warnings

At 80% of a cap the run stops and asks. Choosing to continue is remembered against the cap in force
(`allowances.warned` in the project's state), so the same warning is not asked again as spend grows
or when the work returns to that stage. The cap itself still stops the run, and raising a cap
re-arms the warning at 80% of the new cap. The project and each stage warn separately.

**Raising a cap ahead of time.** At a cap pause you can raise the cap that was hit. You can also
raise the project cap or any one agent's cap (Product Owner, Architect, Developer, QA, Career
coach) without waiting: the Spend card on the project page has **Raise the cap of** and **Add
dollars** (`POST /api/projects/<p>/raise-cap`, `{scope, amount}`, `caps.add_room`). It adds to the
room already granted (`allowances`, no schema change) and needs the project paused; a project
waiting on a budget question is raised there instead.

## Web UI

`uv run --env-file .env studio ui` opens a local control panel (it listens on 127.0.0.1 only; add
`--mac-notify` for macOS notifications). From it you can start a project, see every project's
stage, spend against its caps and past decisions, read the documents and the trace, and answer
every question the pipeline asks (gates with per-item approvals, budget and revision caps, the
merge check, Developer tool requests) from one inbox. The career gate shows each resume bullet
next to its evidence, a copy button for each LaTeX snippet, and a preview of the website entry.
Pause and Resume are buttons on the project page (a paused project loses nothing and can be resumed at any point; `studio resume` does the same), and `/health` checks the keys, token expiry,
tools and folders before a run.
Each tool approval shows a plain-English explanation (what it does, why, what it could change, a
risk label) from one cheap `fast` model call. It is advice only, never sends file contents or
anything that looks like a secret, and its cost goes to `runs/<project>/assistant_cost.json`,
shown apart from the project's caps.
**Action needed.** Things only you can do are listed apart from questions (inbox, project page, nav
count) and never block a run: what the Developer says it needs from you (`needs_from_human` in its
report), a project that paused on an error, a QA tool that is not installed (for example
`gitleaks`), and failing setup checks. Mark an item done, or resume the project to close an error
item; setup items clear themselves when fixed.
A new action item raises a louder alert than a question: a macOS notification with a sound (with
`--mac-notify`), a browser notification titled "You need to do something", a red count in the nav
and "Action needed" in the tab title. Each item alerts once; ones open when the page loads are
not announced again.

`GET /api/projects/<p>/activity` says what each agent is doing (done, working, waiting for you,
paused, stopped, or waiting on the agent before it), with the current task, the Developer's agenda,
notes from what the agents reported and the last steps in plain words. It is built from the saved
state, the trace and `tasks.json`, never from a model call.
The **Team** page (`/p/<slug>/team`) shows it two ways: an office with each agent as a person
and a speech bubble (animated when working, pulsing when waiting for you; click a person for their
notes, agenda and latest steps), and a board of the Developer's tasks (To do, Working, In review,
Done, with pull request links).

**Tasks page** (`/tasks`, `GET /api/tasks`, `orchestrator/task_control.py`). Every unfinished
project, with its current agent and its Developer tasks (working, next up, waiting, in review; done
ones are only counted), and cancelled tasks below. A project runs one agent at a time, so **Pause**
(on the project, not on a task) pauses the project and keeps everything, as on the project page.
**Kill** cancels a task: it is removed from the task list, its folder and branch are discarded if
the branch holds no commits (a branch with commits is never touched), and it is recorded in
`runs/<project>/cancelled_tasks.json`. A task cannot be killed when it is finished, waiting for your
review (reject it there), or needed by another unfinished task (the message names which). The run
owns the saved state, so a kill asked of a live run stops the run (a halt) and is queued in
`cancel_requests.json`; the run applies it as it stops, and a paused project is changed at once. The
Architect owns the plan, so nobody can add, edit or reorder tasks here, and the Architect's own row
has only Pause.

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

**Assistant tools (MCP).** `orchestrator/ui/studio_mcp.py` is an in-process MCP server with three
tiers: *read* tools (projects, status, activity, steps, open questions, documents, rules,
explanations); *direct* tools that are reversible and cheap (pause a project, mark an action item
done); and *propose* tools for everything consequential (answer a gate, a budget limit or a command
request, resume, change the autonomy level, leave a note for the Developer). A propose tool only
writes a proposal (`runs/<project>/proposals.json`); the server builds the card from the proposal's
own arguments and Confirm (which needs the session token) carries it out through the same code as
the normal form. Text returned from agents or files is labelled untrusted. A note for the Developer
(`runs/<project>/notes.json`) is added to its next task prompt once.

**Chat backend.** `POST /api/chat` (Server-Sent Events: `session`, `text`, `step`, `done`, `error`)
talks to one assistant, "Studio", through `orchestrator/ui/chat.py`. It runs the Agent SDK in the
chosen repository (this app or any project clone) with only `Read`, `Glob` and `Grep` (the same
secret and path rules as the Developer, enforced by a PreToolUse guard) plus the studio MCP tools,
with no shell, edits or web. A `Grep` of a whole folder is refused when an env file sits anywhere
under it (a matching line would print a secret); the refusal tells it to search a subfolder or name
the files. Sessions are in memory; each chat is capped by
`assistant.max_chat_usd` in `config.yaml` (and `max_turns`), `tier` picks the strong or fast model,
and its cost goes to a ledger apart from projects (`assistant_cost.json`; `runs/_studio/` for this
app). The reply streams as escaped text and is replaced by server-rendered Markdown when the turn
ends; proposals made during the turn come back in the `done` event. Live check (a few cents):
`ANTHROPIC_API_KEY=... uv run pytest tests/live --live -s -k chat`.

**Chat page.** `/chat`: pick what to ask about (this app or a project), a Strong or Fast model, and
see this chat's cost against its cap. Answers stream in as plain text with the steps taken, then are
replaced by the server's rendered Markdown. A proposal appears as a card ("Waiting for you: nothing
has happened yet") with Confirm and Dismiss. "Ask the assistant about this" on a question or a
paused project opens the chat with that question as context.

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
