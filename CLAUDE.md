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

## QA

After the last Developer task is approved, merge its pull requests on GitHub. QA asks you to confirm
the merges, then runs the real tools (tests, lint, types, dependency audit, secret scan; set in
`config.yaml` under `qa:`) in a throwaway checkout of `main`. The QA agent only judges whether each
acceptance criterion has a test that proves it. `passed` is computed by rule: any failed or skipped
tool, or any `fail` verdict, fails the report; `untested` is shown but does not fail it. A failing
report goes to you at the gate, never back to the Developer automatically. Install `gitleaks`
(`brew install gitleaks`) or the secret scan is skipped and the report fails. QA live test:
`ANTHROPIC_API_KEY=... uv run pytest tests/live --live -s -k qa`.

## Evals

`uv run --env-file .env python -m evals.run --agent qa [--prompt-version v1] [--tier fast] [--runs 3]`
runs the real QA agent on 12 tiny projects with known-right verdicts (`evals/qa_cases.py`) and
appends a row to `evals/results.csv`: accuracy, false passes (the dangerous error: calling an
unproven criterion pass), reports correct, cost, latency. Cost is a cent or two. Change one thing
at a time (prompt version or model tier), rerun, and compare rows. Add a case whenever a real run
shows the agent getting something wrong. There is an eval for QA only so far.

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
