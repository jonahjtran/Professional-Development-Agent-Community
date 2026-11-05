# Phase 4: lean QA agent (sub-plan)

Status: draft for review. Nothing here is built yet.

## Goal

After every Developer task is approved and merged, check the finished project once, independently of
the agent that wrote it. The Developer wrote both the code and its tests, so QA answers a different
question: do the real checks pass, and does each PRD acceptance criterion have a test that proves it?

Out of scope for this phase: the automatic QA to Developer fix loop, load tests, per-PR QA, and
non-Python projects.

## Decisions

| # | Decision | Recommendation | Why |
|---|----------|----------------|-----|
| Q1 | Who runs the tools? | The **orchestrator** runs them as plain subprocesses. The QA agent only reads the results. | Numbers come from real tools, not from a model. No new permission layer or SDK sandbox, because the agent has no tools. |
| Q2 | What does the agent see? | Check results, PRD acceptance criteria, task list, and the test files' contents. | Enough to judge "is this criterion tested", and it fits in one raw API call. |
| Q3 | Who decides pass or fail? | The **orchestrator**, by rule: any failed check or any `fail` verdict fails the report. `untested` is shown but does not fail it. | A model should not be able to talk its way to a pass. |
| Q4 | When does QA run? | Once, after all tasks are approved and their PRs are merged into `main`. | QA must see the integrated code. A per-PR run duplicates CI. |
| Q5 | Failure handling | The report goes to the human gate. No automatic loop: set `limits.max_qa_loops: 0`. | The loop is the expensive part. Add it later if QA failures turn out to be common. |
| Q6 | Model | `fast` (Haiku), stage cap stays `$2.5`. | The job is evidence-matching. Revisit in Phase 5 evals. |
| Q7 | Languages | Python only (`uv run pytest`, `ruff`, `mypy`, `pip-audit`, `gitleaks`). Commands live in `config.yaml`. | `studio new` projects are Python today. |
| Q8 | Missing tool | Recorded as `skipped`, shown in the report, never counted as a pass. A skipped required check fails the report. | A silent skip would look like a clean scan. |

## The flow

```
Developer tasks approved + merged
  -> orchestrator: confirm every completed task's branch is in main (else ask the human to merge, then resume)
  -> orchestrator: throwaway worktree on main, run each check with a timeout, keep the last lines of output
  -> QA agent (raw API, JSON schema): per criterion, verdict pass | fail | untested, with evidence
  -> orchestrator: compute passed, write test_report.json, render it, open the QA gate
```

## PRs

All target `main`, merged in order.

1. **QA report schema, prompt and stage spec.** `schemas/test_report.py` (`checks`, `criteria`,
   `passed`, `summary`), `agents/prompts/qa.v1.md`, renderer, default stage spec. Needs your sign-off
   on the schema, per CLAUDE.md. Tests: schema validation, rendering.
2. **Check runner.** `orchestrator/qa_checks.py` runs the configured commands in a worktree with
   timeouts and output redaction. Tests use a real tiny Python project in a temp dir: passing, failing,
   timed-out and missing-tool cases.
3. **Pipeline wiring.** Merged-into-main check, QA worktree, check results passed in as request
   inputs, `passed` computed by rule, `max_qa_loops: 0`, and the gate text says "no retries" instead of
   "the Developer's 0 retries". Tests: fake runner, real git; unmerged task halts with a clear message.
4. **Live test and docs.** Seeded-bug test: a repo with a failing test and a criterion no test covers
   must produce `passed: false` and an `untested` verdict. Update `CLAUDE.md`. Costs well under a dollar.

## Done when

- A clean project yields `passed: true` and a readable report at the QA gate.
- A seeded failing test, and a leaked fake secret, each fail the report.
- A criterion with no test shows as `untested`.
- The report validates against its schema, and the trace records the prompt version.
- ruff, mypy and pytest are green on every PR.

## Risks

- **The agent calls an untested criterion "pass".** The prompt requires quoting the test name as
  evidence, and the live seeded-bug test checks this. Phase 5 evals measure it properly.
- **Slow or hanging checks.** Per-command timeouts, recorded as a failure.
- **Tool output containing secrets.** Output is redacted with the same helper `publish.py` uses.
