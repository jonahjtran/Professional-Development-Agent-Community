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
the phases need them. Agents work in a separate target repo's worktree, never in this repo.
