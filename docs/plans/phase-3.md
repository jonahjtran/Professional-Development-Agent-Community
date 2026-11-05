# Phase 3 sub-plan: the Developer agent

Status: DRAFT for review. Nothing here is built yet.

## Goal

Given one task from the approved `tasks.json`, a Developer agent writes the code in an isolated
copy of a separate target repo, commits it on its own branch, and the orchestrator opens a pull
request. A permission layer decides what the agent may do. Every denied or asked action is traced.

## Acceptance criteria (from the build plan)

1. On a toy repo, one task yields a PR that passes CI.
2. A test proves that pushes to `main` and reads of `.env` are denied.
3. Denied and asked actions appear in the trace.

## Why this phase is different

The Product Owner and Architect each make one API call. The Developer is an **agent loop**: it
calls the model 10 to 40 times, and between calls it reads files, edits files and runs shell
commands on your machine. So the work here is mostly about control: where it may work, what it may
run, what it can see, and how much it can spend.

## What the SDK gives us (checked against the installed `claude-agent-sdk`)

- `ClaudeAgentOptions` supports `cwd`, `add_dirs`, `tools`, `allowed_tools`, `disallowed_tools`,
  `permission_mode` (including `"dontAsk"`: deny anything not pre-approved), `max_turns`,
  `max_budget_usd`, `setting_sources` (pass `[]` to ignore any local settings files), `env`, `hooks`,
  `can_use_tool`, and `sandbox`.
- **`can_use_tool` is only called for calls that would otherwise prompt.** It is not called for tools
  already allowed. So it cannot be the only guard. Hard denies need a **`PreToolUse` hook**, which
  runs on every tool call and can block it. The design below uses both: hook for deny and ask,
  `can_use_tool` as the path that asks the human.
- `sandbox` (macOS and Linux) restricts the filesystem and network for Bash commands at the OS level.
  Its real behavior with `git` and our rules must be tested, not assumed (see the spike, 3.0).
- `env` merges over the parent environment, so it can blank a variable but not remove it.

## Decisions needed before building

| # | Decision | Recommendation |
| --- | --- | --- |
| D1 | Which repo does the Developer work in? | A new throwaway repo on your account, for example `agent-studio-toy`: a tiny Python package with pytest and a CI workflow, so a PR can actually pass or fail CI. **Never this repo.** You create it (I will not create repos on your account without asking). |
| D2 | Who pushes the branch and opens the PR? | **The orchestrator, not the agent.** The agent may only commit locally. `git push` and every other network command are denied to it. After the run the orchestrator verifies the commits itself, then pushes and opens the PR with `gh`. The GitHub token never enters the agent's world, which removes the biggest risk. This is a change from the build plan's wording ("branch and PR creation via `gh`"); the result is the same, with a smaller blast radius. |
| D3 | GitHub credentials. | A fine-grained personal access token limited to the toy repo (contents and pull requests: read and write), stored as `GITHUB_TOKEN` in `.env`. Branch protection on the toy repo's `main` (PR and review required), so even a leaked token cannot merge. You create the token; I will not see it. |
| D4 | The permission rules (allow, ask, deny). | Proposed in the table below. The **ask** column is the one that needs your judgment. |
| D5 | Spending limits for Developer runs. | The stage cap is a $10 placeholder. Proposal: **$2 per task** through `max_budget_usd`, 40 turns, and a $3 stage cap for the first live runs. Raise only after seeing real costs. |
| D6 | State change: remember finished tasks. | Add `completed_tasks` (list of task ids) to `ProjectState` so the per-task loop can resume after a crash (schema change, needs your OK). |
| D7 | Where does the human review the PR? | In Phase 3 the gate after the Developer shows the PR link, the summary and the diff stat, and you approve in the terminal (merging stays your job on GitHub). Polling `gh pr view` for review state comes later. Until QA exists in Phase 4, this gate replaces the QA gate; Phase 4 moves it. |

### Proposed permission rules (D4)

| Decision | Examples |
| --- | --- |
| **Allow** | Read, Edit, Write, Glob, Grep inside the worktree; `git` status, diff, add, commit, log, branch, checkout on the task branch; running the project's tests and linters (`pytest`, `ruff`, `mypy`, `python -m ...`) |
| **Ask the human** | installing packages (`pip`, `uv add`); any command not on the allow list; deleting files; network access to any host |
| **Deny** | any write or read outside the worktree (including `..` and symlink escapes); `.env`, `.env.*`, key files, `~/.ssh`, `~/.aws`, `~/.config`; `git push`, `git remote`, `git config`, force flags; checkout or commit on `main`; `rm -rf`; `curl` or `wget` piped to a shell; `sudo`; reading environment variables with `env`, `printenv` or `echo $VAR`; edits to `.github/` (the agent must not rewrite CI to make itself pass) |

## PR breakdown

Each PR is small, tests first, and targets `main` (so merging never strands the chain).

### 3.0 Spike: what actually happens (no feature code)
A throwaway script, run once, with its findings written to `docs/plans/phase-3-spike.md`. It answers
questions the docs do not settle, in a scratch directory, for a few cents:
- What does the agent's Bash see in its environment? Can it read `ANTHROPIC_API_KEY` or `GITHUB_TOKEN`?
- Does a `PreToolUse` hook fire for every tool, including allowed ones, and can it block a Bash call?
- What does `sandbox` block on macOS, and does `git commit` still work inside it?
- Does `dontAsk` plus the hook behave as the docs say?
The results decide the details of 3.1 and 3.2. If the sandbox or environment behave worse than
expected, the plan changes here, not after the code is written.

### 3.1 The permission policy (pure code, no SDK)
- `evaluate(tool_name, tool_input, worktree) -> Allow | Ask | Deny` with a human-readable reason.
- Path rules: resolve `..` and symlinks, then require the real path to be inside the worktree.
- Secret rules: `.env`, key files and credential directories, for every tool that can read.
- Bash rules: split the command with `shlex` and on `;`, `&&`, `||`, `|`, `$(...)` and backticks,
  then judge **every** piece. A command that cannot be parsed is not allowed. Matching on text is
  inherently leaky, so this is one layer of several, not the whole defense.
- Tests are a big table of allowed, asked and denied cases, including tricks (`git push` hidden after
  `&&`, `cat .e""nv`, a symlink to `/etc`). This PR is where "a test proves pushes to `main` and
  reads of `.env` are denied" is satisfied.

### 3.2 Wiring the policy into the SDK
- A `PreToolUse` hook calls the policy: deny blocks, ask routes to the human, allow passes.
- `can_use_tool` asks the human through a `ToolPermissionBackend` (terminal prompt first, same
  pattern as gates). This uses the existing `tool_permission` pause kind, so a crash while waiting
  resumes at the same question.
- Every allow, ask and deny writes a `tool_call` or `tool_denied` event to `trace.jsonl`.
- Tests drive the hook and callback directly with fake tool calls.

### 3.3 The worktree manager
- Creates a git worktree of the target repo on `agent/<task-id>`, and removes it afterwards.
- Refuses to run if the target is this repo or lies inside it, or if the working copy is dirty.
- Scrubs what the agent can see (no `.env` is ever copied in).
- Tests use real temporary git repositories (git is local, so no network).

### 3.4 The Developer runner
- A Developer `AgentSpec` and `developer.v1.md` prompt: one task, the architecture, and the rules.
  Repo files and tool output are data, not instructions.
- Options: `cwd` = worktree, `setting_sources=[]`, `sandbox` on (if the spike says it works),
  `permission_mode="dontAsk"`, `max_turns` from config, `max_budget_usd` = what the stage has left,
  and `env` blanking any secret the spike shows would leak.
- The agent writes `dev_result.json` (`DevResult`: task id, branch, summary, deviations, known gaps).
  The orchestrator **verifies the repo itself** (branch exists, commits exist, nothing on `main`)
  instead of trusting the agent's claims.
- Tests with a fake SDK query function, as in the Phase 1 runner tests.

### 3.5 Publishing the PR (orchestrator side)
- A small `gh` wrapper (injectable, so tests use a fake): push the task branch, never `main`; open the
  PR with a body generated from `DevResult`; record `pr_url`. The token is passed only to that
  subprocess and is never logged or traced.
- Tests check the exact commands issued, that `main` is refused, and that the token never appears in
  the trace or in error messages.

### 3.6 The per-task loop in the pipeline
- `completed_tasks` in state (D6). Pick the next task whose dependencies are done; run the Developer on
  it; PR gate (D7); mark it complete; repeat until none are left. Resume mid-loop works.
- Each task has its own revision count and cost line, so a stuck task shows up clearly.
- Request-changes at the PR gate reruns the Developer on the same branch with your comments.

### 3.7 Live toy-repo test (opt-in, costs money)
- With the toy repo and token from D1 and D3: one task becomes a PR, the PR's CI is green, cost is
  recorded, and the trace shows allowed, asked and denied actions.
- A second live case asks the agent to read `.env` and push to `main`, and checks both are denied and
  traced. Phase 3 is done only if this passes against the real agent.

## Defense in depth

No single rule is trusted. The layers, outermost first:
1. The agent works in a separate repo's worktree, never in this repo.
2. The orchestrator, not the agent, holds the GitHub token and does the push.
3. `main` on the toy repo requires a reviewed PR, so nothing can merge itself.
4. The permission policy (hook plus callback) filters each tool call.
5. The OS-level sandbox, if the spike shows it helps.
6. Turn, dollar and stage budgets stop a runaway agent.
7. The trace records every action for review afterward.

## Risks

- **The Bash policy is leaky by nature.** Shell syntax has many ways to spell the same command. That is
  why layers 1 to 3 exist: even if layer 4 is fooled, the agent has no token, no access to this repo,
  and cannot merge.
- **Prompt injection from the repo.** Text in the toy repo could tell the agent to do something else.
  The prompt says repo content is data, and the policy applies whatever the text says.
- **Secrets in the environment.** The agent's subprocess inherits our environment. The spike must show
  what it can see, and `env` must blank the rest.
- **Cheating CI.** An agent that edits `.github/` or deletes failing tests can make a PR "pass". The
  policy denies `.github/` edits; deleting tests is for the human at the gate and for QA in Phase 4.
- **Cost.** Opus-tier agent loops are the most expensive thing in this project. Per-task and stage caps
  apply from the first live run.
- **Scope.** QA, the QA-to-Developer loop, and secret scanning are Phase 4 and not here.

## Out of scope

QA and its loop back to the Developer, secret and dependency scans, polling `gh pr view`, multiple
target repos, and any Slack or web gate.

## Definition of done

All three acceptance criteria shown against the real agent on the toy repo (3.7), tests and CI green
for every PR, the spike findings and this plan updated to match what was built, and your sign-off
before Phase 4.
