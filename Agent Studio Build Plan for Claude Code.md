# Agent Studio: Build Plan for Claude Code

Oct 10, 2026 · @Jonah

## Overview and key decisions

Build **Agent Studio**: a Python system on the Claude Agent SDK that takes a project idea through product definition, architecture, implementation, QA, and career documentation, with a human approval gate after every stage.

**Goals**

- A working pipeline: idea → PRD → architecture + task list → PRs → test report → README, case study, and resume drafts.
- Every stage gated by the human: approve, reject, or request changes with comments.
- An eval harness plus cost and trace logging, so the system itself is a portfolio piece for AI engineer, FDE, and AI-consulting roles.
- A career coach with separate goal profiles for consulting (strategy, M&A, tech) and AI engineering/FDE.

**Non-goals**

- Autonomous merges to main, autonomous resume submission, or any production credentials.
- A multi-user service. This is a single-user tool; a web or Slack gate UI is a stretch phase.

**Key decisions**

| Decision | Choice | Why |
| --- | --- | --- |
| Language | Python 3.12 (assumed; confirm before Phase 0) | Best-supported Agent SDK path, easy evals and Pydantic schemas |
| Framework | Claude Agent SDK | Custom orchestration, tracing, and evals are interview talking points |
| Orchestrator | Plain code state machine, not an LLM | Cheaper, predictable, testable; LLM judgment only where needed |
| Shared state | Files in `runs/<project>/`, not chat context | Handoffs stay checkable and survive crashes |
| Deterministic work | Real tools (pytest, ruff, pip-audit, secret scan) run by CI; agents trigger and read results | Don't use an LLM where a tool is exact |
| Resume output | Draft-only; every bullet must cite evidence | Prevents invented metrics |

## Repo layout and tech stack

Create this layout in Phase 0. Directory names are fixed so later phases and evals can rely on them.

```
agent-studio/
  pyproject.toml
  CLAUDE.md                  # conventions for Claude Code (see Working instructions)
  config.yaml                # model tiers, budgets, max revisions, per-agent max_turns
  orchestrator/
    cli.py                   # commands: new, run, resume, status, report
    state.py                 # ProjectState, Stage enum, load/save state.json
    pipeline.py              # stage order and transitions
    gates.py                 # human_gate(): approve / reject / request_changes
    budget.py                # token and dollar caps, kill switch
    runner.py                # wraps Agent SDK query(); returns typed result + usage
    trace.py                 # append-only trace.jsonl writer
  agents/
    base.py                  # AgentSpec: prompt, tools, model tier, schema, max_turns
    product_owner.py
    architect.py
    developer.py
    qa.py
    career_coach.py
    prompts/                 # one versioned .md prompt per agent
  schemas/                   # Pydantic v2 models for every handoff artifact
  mcp_servers/job_postings/  # custom MCP server for the career coach
  profile/                   # goals.yaml, skills.yaml (versioned inputs)
  runs/<project-slug>/       # state.json, artifacts, trace.jsonl, cost.json
  evals/                     # datasets, rubrics, run.py, results.csv
  tests/
  .github/workflows/ci.yml   # ruff, mypy, pytest on every PR
```

**Stack:** Python 3.12, uv, `claude-agent-sdk`, Pydantic v2, Typer + Rich (CLI), PyYAML, the official `mcp` Python SDK, pytest, ruff, mypy, GitHub CLI (`gh`) for PRs.

SDK parameter and class names change; check the current Agent SDK docs before writing runner code instead of relying on memory.

## Orchestrator design

The orchestrator is a deterministic state machine in plain Python. It loads the right files into each agent's context, validates outputs, enforces gates and budgets, and records everything. It never calls an LLM to decide what happens next.

```
INTAKE
  → PRODUCT_OWNER → GATE(prd)
  → ARCHITECT     → GATE(architecture + tasks)
  → for each task in tasks.json (dependency order):
        DEVELOPER → QA → (fail? back to DEVELOPER, max 2) → GATE(pull request)
  → CAREER_COACH  → GATE(readme, case study, resume drafts)
  → DONE
```

**Gate outcomes:** approve moves on; request\_changes re-runs the same agent with the previous draft plus your comments; reject restarts the stage or halts, your choice.

**`state.json` fields**

| Field | Meaning |
| --- | --- |
| `project_slug`, `idea` | Identity and the original one-paragraph idea |
| `stage`, `status` | Current stage; `running`, `awaiting_gate`, `halted`, or `done` |
| `artifacts` | Map of name → path, content hash, approved\_at, revision number |
| `current_task_id` | Task being built in the Developer loop |
| `revisions` | Revision count per stage |
| `gate_history` | Every decision with timestamp and comments |
| `cost` | Tokens and dollars per stage and total |

**Rules**

- Validate every agent output against its Pydantic schema before showing it at a gate. A failure triggers one automatic retry with the validation error, then halts.
- Max 3 revisions per stage and `max_turns` per agent, both set in `config.yaml`. At the cap, ask the human: approve anyway, abandon, or raise the cap.
- Budget caps per stage and per project in dollars; exceeding one halts the run before the next agent call.
- Writes to `state.json` are atomic (write temp file, then rename). `studio resume <project>` continues from the saved stage after a crash or Ctrl-C.
- Each stage reads only the approved artifacts it declares as inputs, never the whole run directory.
- Append every transition, agent call, tool call, and gate decision to `trace.jsonl`.

## Agent specs

Each agent is an `AgentSpec` in `agents/`: system prompt (versioned file), tool allowlist, model tier, `max_turns`, and an output schema. Model tiers (`strong`, `fast`) map to concrete model names in `config.yaml`, never hardcoded in agent code, so cost-versus-quality experiments are a config change.

| Agent | Tier | Inputs | Outputs | Tools |
| --- | --- | --- | --- | --- |
| Product Owner | strong | `idea.md`, `goals.yaml`, latest `gap_report.md` | `prd.md`, `prd.json` | Read, WebSearch |
| Architect | strong | approved `prd.json` | `architecture.md`, `adr/NNN-*.md`, `tasks.json` | Read, Write (run docs dir only) |
| Developer | strong | one task from `tasks.json`, `architecture.md` | git branch, PR, `dev_result.json` | Read, Write, Edit, Bash, git in an isolated worktree; permission callback |
| QA | fast | PR branch, task acceptance criteria | `test_report.json` | Bash with an allowlist (pytest, ruff, mypy, pip-audit, secret scan, basic load test); read-only on source |
| Career Coach | fast for gap analysis, strong for writing | `goals.yaml`, `skills.yaml`, merged-project artifacts, job-postings MCP | `gap_report.md`, `evidence.json`, `resume_variants/`, `readme_draft.md`, `case_study.md` | Read, job-postings MCP; no Bash |
| Orchestrator | none (code) | everything | `state.json`, `trace.jsonl`, `cost.json` | n/a |

**Mode notes**

- **Product Owner** has two modes set by the `track` field: `engineering` (features, users, metrics) and `consulting` (adds stakeholders, business success metrics, adoption risks, rollout considerations). Both can run on one project.
- **QA** runs the real tools and interprets their output. Test and lint runs also execute in GitHub Actions so CI is the source of truth. Any performance numbers must come from a defined load-test script against a defined target, with the script and target recorded in the report.
- **Developer** completes exactly one task per run, on its own branch, with the task's acceptance criteria as the definition of done.

## Schemas and handoff artifacts

Define one Pydantic v2 model per artifact in `schemas/`. Every agent run ends with the model validated; each `.md` artifact has a `.json` twin the orchestrator can check.

| Model | Key fields |
| --- | --- |
| `PRD` | `problem`, `users`, `track` (engineering, consulting, both), `goals`, `success_metrics[{name, target, how_measured}]`, `scope`, `non_goals`, `risks`, `assumptions`, optional `stakeholders`, `adoption_risks` |
| `Architecture` | `components[{name, responsibility, tech}]`, `data_flow`, `interfaces`, `tech_choices[{choice, alternatives, rationale}]`, `risks`; each decision also written as an ADR |
| `TaskList` | `tasks[{id, title, description, acceptance_criteria[], depends_on[], size}]` ordered so dependencies come first |
| `DevResult` | `task_id`, `branch`, `pr_url`, `summary`, `deviations_from_plan`, `known_gaps` |
| `TestReport` | `tests_passed`, `tests_failed`, `coverage_pct`, `lint_errors`, `type_errors`, `vulnerabilities[]`, `secrets_found`, `load_test` (script, target, results, or null), `verdict` (pass, fail) |
| `GapReport` | per track: `target_roles`, `required_skills`, `have`, `gaps`, `recommended_projects[{idea, closes_gaps, track_coverage, interview_talking_points}]` |
| `Evidence` | `id`, `type` (pr, metric, eval\_result, case\_study, trace), `ref` (URL or file path), `description` |
| `ResumeBullet` | `text`, `track`, `evidence_ids[]` |

**Validation rules the code must enforce**

- A `ResumeBullet` with empty `evidence_ids` fails validation.
- Any number in bullet text must appear in a linked `Evidence` of type `metric` or `eval_result`.
- `tasks.json` must be acyclic and every `depends_on` must reference an existing task.
- Every PRD success metric must be referenced by at least one task's acceptance criteria or flagged as unmapped.

## Human-in-the-loop gates

There are two layers: artifact gates run by the orchestrator, and tool gates inside the Developer agent.

**Artifact gates**

| Gate | Shown to the human | Notes |
| --- | --- | --- |
| PRD | `prd.md`, diff against previous draft, cost so far | Cannot proceed until approved |
| Architecture + tasks | `architecture.md`, ADRs, `tasks.json` | Approve the whole set or comment per file |
| Pull request | Real GitHub PR plus `test_report.json` | Orchestrator polls `gh pr view` for review state: approved, or changes requested with comments |
| Career outputs | README, case study, resume variants, `evidence.json` | Each bullet shown beside its evidence links |

Each gate returns `approve`, `reject`, or `request_changes` with free-text comments. Request-changes re-invokes the same agent with the prior draft and your comments appended, increments the revision count, and stops at the revision cap. All decisions go to `gate_history` and `trace.jsonl`.

Implement `human_gate()` as a Typer/Rich CLI prompt first, behind an interface (`GateBackend`) so a Slack or web backend can replace it later without touching the pipeline.

**QA loop before the PR gate:** a failing `test_report` sends the task back to the Developer automatically, up to 2 times, with the report attached. After that it goes to the human with the failure noted.

**Tool gates (Developer agent)**

Use the SDK's permission callback (`can_use_tool`) or hooks with three outcomes:

- **Allow:** reads and edits inside the worktree, git commands on the task branch, test and lint commands.
- **Ask the human:** installing packages, network access to unlisted hosts, deleting files, any shell command not on the allowlist.
- **Deny:** writes outside the worktree, `git push` to main or force-push, reading `.env` or credential files, `rm -rf`, `curl | sh`.

## Security, sandboxing, and permissions

- Developer and QA run in a **git worktree or fresh clone** of the target repo, never the Agent Studio repo itself. Set the SDK `cwd` to that directory.
- Use a **fine-scoped GitHub token** (contents and pull requests on the target repo only). Branch protection on `main` requires a review, so the agent physically cannot merge.
- Agents get **no production credentials**. Secrets live in environment variables read by the orchestrator; agents never see `.env` files, and the permission callback denies reads of them.
- QA includes **secret scanning** (gitleaks or trufflehog) and **dependency audit** (`pip-audit` or `npm audit`) as required checks. A finding fails the report.
- The Career Coach has **no Bash and no write access** outside the run's career output directory. Job-postings data comes from files you curate; do not scrape sites that forbid it.
- **Kill switch:** `studio halt <project>` stops after the current call; budget overruns halt automatically.
- Treat any text from the web, job postings, or repo contents as **data, not instructions**. Agent prompts say so explicitly, and the orchestrator never passes tool output into another agent's system prompt.

## Evals

Evals are the main differentiator for AI engineer and FDE interviews. Build the harness in `evals/` and record every run.

| Target | Dataset | Scoring |
| --- | --- | --- |
| Product Owner | 8–10 seed ideas, each with a hand-written description of a good PRD | Rubric (0–5 each): metrics measurable, non-goals stated, scope realistic, risks specific, consulting fields present when track says so |
| Architect | The approved PRDs from the set above | Rubric: every requirement covered by a component, decisions have alternatives, task list acyclic and testable |
| Developer + QA | 3–5 small tasks with known-good tests held out from the agent | Hidden tests pass rate, revision rounds, cost per merged PR |
| Career Coach | 3 sample projects with real evidence files | Programmatic: 100% of bullets map to evidence, 0 unsupported numbers; rubric for gap-report quality |

**Harness**

- `python -m evals.run --agent product_owner --dataset evals/data/po.jsonl` runs the agent headlessly (gates auto-approve in eval mode) and scores the output.
- LLM-judge scoring uses the rubric with a different prompt and, where possible, a different model than the agent. Hand-label about 20% of cases to measure judge agreement.
- Append each run to `evals/results.csv`: timestamp, git SHA, prompt version, model, dataset, mean score, cost, latency.
- Change one thing at a time (prompt wording, model tier, schema), rerun, and keep the table. That table is what turns into resume bullets and interview stories.

## Observability and cost tracking

- `trace.jsonl` is append-only, one JSON object per event: `ts`, `project`, `stage`, `event` (agent\_start, tool\_call, tool\_denied, agent\_end, gate, transition), `agent`, `model`, `tokens_in`, `tokens_out`, `cost_usd`, `latency_ms`, plus event-specific fields.
- Read token and cost data from the SDK's result messages in `runner.py`; never estimate.
- `studio report <project>` prints cost and latency per stage, revisions per stage, gate outcomes, tool calls and denials, and QA pass rate.
- Keep trace format simple enough to export to Langfuse or OpenTelemetry later (stretch phase) without changing call sites.
- Report cost per merged PR and cost per approved artifact. Compare model tiers once evals exist; a measured "cheaper model held quality" result is a strong interview story.

## Career coach and resume agent

The coach treats `profile/goals.yaml` and `profile/skills.yaml` as versioned inputs you edit directly. It reads them at the start of every run and never modifies them; it proposes changes at a gate and you apply them.

```yaml
# profile/goals.yaml (starter; the human edits)
tracks:
  consulting:
    target_roles: [strategy consulting, M&A advisory, technology consulting]
    target_firms: []          # fill in
    emphasis: [client impact, structured problem solving, business case]
  ai_engineering:
    target_roles: [AI engineer, forward deployed engineer, AI implementation consultant]
    target_companies: []      # fill in
    emphasis: [agent reliability, evals, tool integration, cost control]
constraints:
  timeline: ""                # e.g. recruiting dates
  avoid: []
```

**Gap analysis procedure**

1. Load goals and skills, then query the job-postings MCP for each track.
2. Compute skill frequency across postings and compare with `skills.yaml`.
3. Output `gap_report.md` per track with evidence (posting IDs) for every claimed gap.
4. Recommend 3 project ideas per run, each tagged by track coverage, gaps closed, and interview talking points. Prefer projects that serve both tracks and projects with a real user.
5. Feed the approved recommendation to the Product Owner as an input.

**Job-postings MCP server (`mcp_servers/job_postings/`)**

- Tools: `list_postings(track, query?)`, `get_posting(id)`, `skill_frequency(track, top_n)`.
- Data source: a folder of postings you curate (JSON or markdown with title, company, source URL, date, text). Add an import script for pasted postings. No scraping.
- Attach this server only to the Career Coach agent.

**Resume and README rules**

- Produce one variant per track (`resume_variants/consulting.md`, `resume_variants/ai_engineering.md`). Drafts only; the human decides what goes on the real resume.
- Bullets follow action, scope, tool, measured result, and link to `Evidence` IDs. No evidence, no bullet. Metrics may only come from `evidence.json`, which is built from PRs, test reports, eval results, and trace cost data.
- Produce `case_study.md` with: problem, who it is for, approach, measured results, failures and fixes, tradeoffs, rollout and adoption considerations, what you would do differently.
- Produce `readme_draft.md` for the target repo with business framing (time saved, error rate, cost) beside the tech stack, and an architecture diagram when the Architect supplied one.

**Pushback instruction in the coach prompt:** it must challenge weak project ideas, cite posting evidence, and refuse to chase keywords for projects that would not teach you something you can discuss in depth.

## Phased build plan

Build in this order. One PR per phase, reviewed by the human before the next phase starts. A phase is done only when its acceptance criteria pass.

| Phase | Build | Acceptance criteria |
| --- | --- | --- |
| 0. Scaffold | Repo layout, `pyproject.toml`, `config.yaml`, ruff, mypy, pytest, CI workflow, `CLAUDE.md` | CI green on an empty test suite; `uv run studio --help` works |
| 1. Orchestrator core | `state.py`, `pipeline.py`, `gates.py`, `budget.py`, `trace.py`, `runner.py`; a dummy echo agent | Dummy pipeline passes approve, reject, and request-changes paths; killing the process mid-run and running `studio resume` continues correctly; budget cap halts a run; unit tests cover every transition |
| 2. Product Owner + Architect | Schemas, prompts, agent specs, real SDK calls, schema validation with one retry | From a one-line idea, produces a valid `prd.json` and `tasks.json`; comments at a gate change the next draft; cost per stage appears in `cost.json` |
| 3. Developer | Worktree sandbox, permission callback, branch and PR creation via `gh` | On a toy repo, one task yields a PR that passes CI; a test proves pushes to main and reads of `.env` are denied; denied and asked actions appear in the trace |
| 4. QA | Allowlisted tool runner, `test_report.json`, auto-loop back to Developer (max 2) | Seeded bugs are caught; secret and dependency scans run; report validates against schema; a deliberately failing PR returns to the Developer once |
| 5. Eval harness | `evals/` datasets, rubrics, judge, `results.csv` | Baseline scores recorded for Product Owner and Architect; one prompt change shows a measured before/after |
| 6. Career coach | `goals.yaml`, `skills.yaml`, job-postings MCP, gap report, `evidence.json`, resume variants, README and case study | Validator rejects a bullet with no evidence or an unsupported number; two track variants generated from one project; gap report cites posting IDs |
| 7. Reporting and demo | `studio report`, README with architecture diagram, one full end-to-end run on a real project | Every gate exercised once; case study written from real trace and eval data |
| 8. Stretch | Slack or web `GateBackend`, Langfuse export, model-routing experiment | Gate decisions work from Slack; measured cost reduction with quality held |

Suggested first real project for Phase 7: something with an actual user (a student org workflow, for example), so the consulting track has a client story and measured adoption.

## Working instructions for Claude Code

Copy this section into `CLAUDE.md` at the repo root.

- Read this whole plan before writing code. Work one phase at a time, starting with Phase 0, and stop after each phase for human review.
- Write tests first for orchestrator logic (state transitions, gate outcomes, budget halts, schema validators). Agent prompts are tested through evals, not unit tests.
- Use type hints everywhere; `ruff`, `mypy`, and `pytest` must pass before opening a PR.
- Do not guess Agent SDK or MCP SDK APIs. Read the current official docs for exact names and parameters, and note the SDK version in `pyproject.toml`.
- Ask before: changing a schema, adding a dependency not listed in this plan, touching credentials or GitHub tokens, or changing a gate's behavior.
- Never commit secrets. Keep model names and budgets in `config.yaml`, not in code.
- Open one PR per phase with a description listing what was built, how the acceptance criteria were verified, and anything deferred.
- Keep prompts in `agents/prompts/` as versioned files; record the prompt version in every trace event.
- Definition of done for any task: acceptance criteria met, tests added, CI green, docs or `CLAUDE.md` updated if conventions changed.

## Risks, open questions, and resume framing

| Risk | Mitigation |
| --- | --- |
| Runaway cost from agent loops | Per-stage and per-project dollar caps, max revisions, max turns, kill switch |
| Plausible but wrong QA or performance claims | Real tools produce the numbers; reports record script and target; CI is source of truth |
| Inflated resume claims | Evidence-linked bullets, validator, draft-only output |
| Coach tells you what you want to hear | Pushback instruction, posting-based evidence required for every gap |
| Scope creep across five agents | Phase gates; Phases 0–4 deliver a usable dev pipeline before the coach exists |
| SDK API drift | Pin versions, read current docs before coding, isolate SDK calls in `runner.py` |

**Open questions to settle before Phase 0**

1. Confirm Python (assumed) or switch to TypeScript.
2. Budget caps in dollars per stage and per project.
3. Where job postings will come from (manual paste, saved files) and the first list of target firms and companies.
4. The first real project to run through the full pipeline in Phase 7.
5. Public or private repo for Agent Studio itself.

**How this appears on the resume (fill brackets only from measured data)**

- Built a human-gated multi-agent development pipeline on the Claude Agent SDK with deterministic orchestration, schema-validated handoffs, and tool-level permission controls.
- Built an eval harness across \[N\] agents; improved \[rubric\] from \[X\]% to \[Y\]% over \[N\] prompt iterations.
- Reduced cost per merged PR by \[X\]% by routing \[stage\] to a smaller model with quality held at \[Y\]% on the eval set.
