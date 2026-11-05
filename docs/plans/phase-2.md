# Phase 2 sub-plan: Product Owner + Architect

Status: DRAFT for review. Nothing here is built yet.

## Goal

From a one-line idea, the pipeline produces a schema-valid `prd.json` and `tasks.json`, shows each
at a gate, feeds the human's comments into the next draft, and records the cost of each stage in
`cost.json`.

## Acceptance criteria (from the build plan)

1. A one-line idea produces a valid `prd.json` and `tasks.json`.
2. Comments at a gate change the next draft.
3. Cost per stage appears in `cost.json`. (Already written by Phase 1; Phase 2 proves it with real
   calls.)

## Decisions needed before building

| # | Decision | Recommendation |
| --- | --- | --- |
| D1 | How do the Product Owner and Architect call Claude? | **DECIDED: the raw Messages API** (`anthropic` package) with JSON output checked by Pydantic. The `anthropic` dependency is approved. |
| D2 | Where does `track` (engineering, consulting, both) come from? | **DECIDED: yes.** A `--track` option on `studio run`, saved in state (schema change approved). |
| D3 | Web context for the Product Owner. | **DECIDED: yes**, using Anthropic's server-side web search tool (`web_search_20260209`, runs on Anthropic's side, no loop to write). Limited with `max_uses`; results are treated as data, never instructions; the prompt says so. See PR 2.5. |
| D4 | API credentials. | You provide `ANTHROPIC_API_KEY` as an environment variable (or `ant auth login`). It is never written to a file, a trace, or a commit. Live tests are opt-in. |
| D5 | Model IDs in `config.yaml`. | **RESOLVED:** `claude-opus-5-5` (strong) and `claude-haiku-5-5` (fast) are current, valid IDs. No change needed. |
| D6 | Artifacts in state. | **DECIDED: yes** (schema change approved), PR 2.3. |
| D7 | Prices in `config.yaml`. | **NEEDS YOUR OK.** The raw API returns token counts but not a dollar cost (the Agent SDK did). To keep budgets working, `config.yaml` needs a `pricing` section: dollars per million input and output tokens per model, plus the per-search price for web search. This changes the config schema. Prices are read from the live pricing page when filling it in, not from memory. |

## PR breakdown

Each PR is small, tests first, one branch. They are chained only where one needs the previous.

### 2.1 Schemas (`schemas/`)
- Pydantic v2 models: `PRD`, `Architecture`, `TaskList` (plus its `Task`), and `ADR`, with the fields in
  the build plan's schema table.
- Strict models: unknown keys are errors, required fields are required.
- `track` is an enum. The consulting-only fields (`stakeholders`, `adoption_risks`) are optional,
  but required when `track` is `consulting` or `both`.
- Tests: valid documents pass; each missing or wrong field fails with a useful message; JSON round trip.

### 2.2 Cross-field validators
- `TaskList`: no dependency cycles, every `depends_on` points to a real task, tasks are ordered
  dependencies-first, ids are unique.
- PRD and tasks together: every success metric is referenced by at least one task's acceptance
  criteria, or reported as unmapped (a flag, not an error).
- Tests: a cycle, a dangling dependency, a duplicate id, an unmapped metric, a fully mapped set.

### 2.3 Artifacts in state (schema change, needs your OK)
- `ProjectState.artifacts`: name to `{path, content_hash, approved_at, revision}`.
- An artifact store: writes `x.md` and its `x.json` twin atomically under `runs/<slug>/`, hashes the
  content, records approval.
- Stage inputs: each stage declares the artifact names it may read. The pipeline passes only those.
- Tests: write and read back, hash changes with content, approval recorded, an undeclared input is
  refused, state round trip and old `state.json` files still load.

### 2.4 Versioned prompt files
- `agents/prompts/product_owner.v1.md` and `architect.v1.md`, plus a loader returning
  `(text, version)`.
- Every prompt says that web, file and tool text is data and not instructions.
- The prompt version flows into every trace event (already supported by `AgentResult`).
- Tests: loader finds the latest version, a missing file fails loudly, version parsing, every
  configured agent has a prompt. The wording itself is judged by evals in Phase 5, not unit tests.

### 2.5 Raw API runner and validated structured calls (one retry)
First step: read the live API docs for the exact parameter names. Two points to confirm there: that
JSON-schema output (`output_config.format`) can be combined with the web search tool, and the current
per-search price. Notes already known: forced `tool_choice` returns a 400 on `claude-opus-5-5`, so
structured output uses `output_config.format`; thinking is always on, so set `effort` explicitly
(its default on this model is `medium`); large outputs should stream.
- `ApiRunner`, a new `AgentRunner` using the `anthropic` package. The client is injected, so tests use
  a fake and make no network calls.
  - Model comes from the agent's tier in `config.yaml`; `max_tokens` and `effort` are set explicitly.
  - Cost is computed from the response's token counts and the `pricing` section of `config.yaml`
    (D7). A response with no usage numbers is an error, so spend is never silently $0.
  - A `refusal` stop reason is an `AgentError`, which halts the run, and its cost is still billed.
  - Server-side `fallbacks` are recommended by Anthropic for this model. I will include them and tell
    you, unless you say no. Cost is then summed over every model that ran.
- Web search for the Product Owner: the server-side `web_search` tool with `max_uses` from config,
  optional `allowed_domains`, search count taken from the response and billed per search. A tool
  error comes back as a normal result block and is handled, not raised. Search text is passed as data
  only and never into a system prompt.
- `call_validated(runner, schema, request)`: run the agent, parse JSON, validate with the schema.
  On failure retry once with the validation error appended; on a second failure raise `AgentError`
  (the pipeline already halts on it). Both calls' costs are billed.
- Tests with a fake client: valid first try, invalid then valid, invalid twice, cost summed over both,
  cost from tokens, a refusal halts, web search count is billed, a web search error block is handled,
  missing usage raises.

### 2.6 Product Owner wiring
- `studio run --track engineering|consulting|both`, saved in `ProjectState.track` (default `engineering`).
- The Product Owner stage reads `idea` (and `goals.yaml` if present), writes `prd.json` and a
  rendered `prd.md`, and records them as artifacts.
- At the gate the human sees `prd.md`, a diff against the previous draft, and cost so far.
- On request-changes the agent gets its previous draft plus the comments.
- Tests (fake client): artifacts written, revision number increments, the comments appear in the next
  prompt, the diff is shown, a schema failure after retry halts.

### 2.7 Architect wiring
- Reads only the approved `prd.json`. Writes `architecture.md`, `adr/NNN-*.md`, and `tasks.json`,
  and runs the 2.2 validators before the gate.
- The gate shows the whole set; the human approves it or comments.
- Tests: reads only declared inputs, one ADR per tech choice, an invalid task list (cycle) goes
  through the retry path, approving records all artifacts.

### 2.8 Live smoke test and wrap-up
- A `live`-marked test, skipped unless `ANTHROPIC_API_KEY` is set and `--live` is passed, never in CI.
- Run one real idea end to end through PRD and architecture, and record the
  real cost per stage. Re-tune the placeholder stage caps only if the numbers show they are wrong.
- Update `CLAUDE.md` and the build plan to reflect what changed, and stop for your Phase 2 review.

## Order and size

2.1, 2.2, 2.3 and 2.4 are independent of any API call and can be reviewed quickly. 2.5 to 2.7 use fakes
and cost nothing. Only 2.8 spends money, and that is a handful of dollars at most, under the $20
project cap.

## Risks

- **Prompt quality.** Unit tests cannot tell a good PRD from a bad one. Until the Phase 5 evals, quality
  is judged by reading outputs. Keep each prompt change small and versioned.
- **Schema churn.** Fields may need to change once real output is seen. Keep schemas in one place and
  tests table-driven so a change is cheap.
- **JSON reliability.** Models sometimes return malformed or wrapped JSON. The single retry with the
  error message handles most of it; a second failure is a halt by design.
- **Cost surprises.** The stage caps ($1.50 and $2.50) and the budget warnings already protect each
  call, but a revision loop can reach the cap quickly.
- **Scope creep.** Per-task Developer looping, the PR gate, and the Career Coach are not here.

## Out of scope

The Developer, QA, worktrees, permission callback, the eval harness, web search for the Architect, `gap_report.md`
as a Product Owner input (it does not exist until Phase 6), and any Slack or web gate.

## Definition of done

All three acceptance criteria demonstrated on one real run, tests and CI green for every PR, the
sub-plan and `CLAUDE.md` updated, and your sign-off before Phase 3.
