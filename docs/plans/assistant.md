# Plan: a more autonomous, more visible Agent Studio

Seven additions to the web UI, built as a chain of small pull requests (tests first, `main` always
working). You can stop after any PR. Helper and chat spend is tracked in its own ledger
(`runs/<project>/assistant_cost.json`, and one for the chat) and shown, so it never hides inside a
project's spend or its caps.

The principle behind all of it: **autonomy comes from deterministic rules that code can check, never
from a model's opinion.** A model may explain, propose and summarise. Only rules the policy can
evaluate, or a click from you, may allow an action.

## 1. Explain every approval (PR A)

An approval card gets a plain-English panel: what the request is trying to do, why it probably wants
to, what it could change, and a risk level. Advice only: it never allows or denies anything.

- `orchestrator/explain.py`: one `fast`-tier call (about a tenth of a cent) given only the command or
  request, the policy reason, the task title and the project idea. It never receives file contents,
  environment values or tokens; commands are cut at 2,000 characters.
- Fetched by the page, not the pipeline (`GET .../questions/<id>/explanation`), so the Developer is
  never held up; cached next to the question. If it fails the raw command is still shown.
- Output schema: `what`, `why`, `effect`, `risk: low|medium|high`, `unsure`. Escaped by the server.

## 2. "Always allow", with guard rails (PR B)

Approving a command offers: **Allow once** / **Always allow in this project** / **Always allow
everywhere**. The goal is the fewest interruptions that are still safe.

How it works:

- The rule is not the raw text. The command is parsed (the same parser `permissions.py` uses) and
  generalised to a **pattern you can read**, shown before you confirm: `npm run <script>` becomes
  "any `npm run` script", `npx next lint` becomes "`npx next lint` with any flags". You can edit it
  narrower. Compound commands (`a && b`) save one rule per part.
- Rules live in `runs/<project>/rules.json` (project) or `~/.config/agent-studio/rules.json`
  (everywhere), are listed on a **Rules** page with the last use and a Revoke button, and are
  loaded by `permissions.evaluate()` as a final allow-list step.
- **A rule can never override a refusal.** The policy order stays: deny, then allowed, then rules,
  then ask. Secrets, `git push`, leaving the worktree, `.github/`, recursive deletes, shell
  expansion, `sudo`, piping into a shell and internal network hosts are **not rule-able**; the
  button does not appear for them.
- Installing packages is the main source of repeated asks. Recommended: a separate, deterministic
  rule, "`npm ci` / `npm install` with no package name when a lockfile exists" is allowed
  (the sandbox already limits it to the package registries); naming a new package still asks.
- Every use of a rule is traced (`rule_id` in the event), and the budget caps stay as the backstop.
- Autonomy dial per project (default **Standard**): *Cautious* (every command asks, no rules),
  *Standard* (built-in list plus your rules), *Autonomous* (adds the lockfile install rule and offers
  rules after the second identical approval). Nothing in any setting reaches the refusal list.
- Recommendation not taken: letting the risk label from section 1 auto-approve "low risk" commands.
  A mislabelled command would then run with no human, so the label stays advice.

## 3. Tell me when only I can act (PRs C and D)

- PR C: a new pending kind `todo` ("Action needed") with a title, detail and "Mark done", shown in
  the inbox, on the project page and as a nav count. Sources, all detected by code: a merge check
  that cannot proceed, a missing tool the QA stage needs, a token missing or about to expire, a
  request the policy refuses outright that the task still depends on, and an optional
  `needs_from_human` list in the Developer's final report (prompt v4 explains when to use it).
- PR D: a louder alert for these, separate from ordinary questions: macOS notification with its own
  title and sound (`--mac-notify`), a browser notification, a tab-title count and a red nav badge.
  Each item alerts once.

## 4. See what each agent is doing (PRs E and F)

You can only see which stage is running today. The data to do better already exists: `trace.jsonl`
(agent start and end, every tool call), `tasks.json` (tasks with `depends_on`), the saved state, and
each agent's report (summary, deviations, known gaps).

- PR E: `orchestrator/activity.py` derives a per-agent record **from code, with no model call**:
  `status` (done, working, waiting on <agent or task>, blocked on you, upcoming), `doing` (a
  high-level line built from the latest tool calls, e.g. "Editing the settings page", "Running
  tests"), the current task, spend, and a notes list. `GET /api/projects/<p>/activity`.
- PR F, the page. A **Team** tab on each project, two views of the same data:
  - **Office view (recommended default):** each agent is a person at a desk (Product Owner,
    Architect, Developer, QA, Career coach) with a speech bubble: "Working on T3: normalisation",
    "Waiting for the Architect", "Done: wrote the PRD", "Needs you: merge the PRs". Pure CSS
    animation (typing when working, still when waiting, a check when done), no dependency.
    Click a person to open their **notes and agenda**: their task list, the report they wrote, and
    their recent steps.
  - **Board view:** a Jira-style board for the Developer's work, columns To do / Working / In
    review / Done, one card per task (title, PR link, spend, depends-on). A card is "waiting" when a
    dependency is not done.
  - Recommendation: build the board data first (it is the more honest view of real work) and treat
    the animation as a skin over the same data, not a separate system.
  - Optional later: a `fast`-tier one-sentence summary of the last ten steps for a richer bubble.
    Off by default; the code-derived line is always shown.

## 5. Act from the chat, through MCP (PRs G and H)

You should be able to say "pause the project", "raise the stage cap by $2", "always allow npm lint
here", "tell the Developer to use server actions". Recommended design:

- **One conversation partner: the orchestrator ("Studio").** The individual agents are single-run
  workers with no ongoing conversation, so there is nothing to talk to between runs. "Talk to the
  Developer" becomes the orchestrator relaying your instruction the way the system already supports
  it: as a change request at the next gate, or as a note added to the next task. The chat says which
  it did.
- **The actions are an MCP server.** `orchestrator/ui/studio_mcp.py` exposes tools built on the
  Claude Agent SDK's in-process MCP support (exact API to be read from the current docs before
  coding, not guessed). The chat uses it directly; the same tool definitions can later be served
  over stdio so Claude Code or Claude Desktop can drive Studio too.
- **Three tiers of tool, by consequence:**
  1. *Read, free:* list projects, project status and activity, trace, documents, costs, rules,
     pending questions, plus read-only `Read`/`Glob`/`Grep` on the chosen repository.
  2. *Reversible, runs directly:* pause, resume, mark an action done, ask for an explanation,
     open the chat on a given question.
  3. *Consequential, **proposal only**:* approve or reject a gate, answer a cap or merge check,
     allow a tool request, add an always-allow rule, raise a budget, start a project, publish career
     items. The model can only **propose**; the server writes a proposal card with the exact action
     and arguments, and **you click Confirm**. The model cannot click, and the confirmation is bound
     to that card, so changed arguments mean a new card.
- **Prompt-injection safety:** drafts, code and tool output are agent-written text. In the chat they
  are labelled untrusted data, and because tier-3 actions need your click on a card the server
  built, injected instructions cannot approve anything.
- Same secret rules as everywhere (`.env`, tokens and keys are never readable), same cost ledger,
  a per-chat dollar cap in `config.yaml` (`assistant.max_chat_usd`).
- PR H is the chat backend (`POST /api/chat`, streaming over Server-Sent Events, sessions held in
  memory and resumed by id; reply streams as escaped text and is replaced by the server-rendered
  Markdown when the turn ends). PR I (below) is the page.

## 6. The chat page (PR I)

A Chat item in the nav: repository picker (Agent Studio or any project), message list, proposal
cards with Confirm / Dismiss, cost so far, "New chat". "Ask about this" on every question, failed
project and paused project opens the chat with that item as context.

## Decisions to confirm

1. **Schema changes** (CLAUDE.md says ask first): new pending kinds `todo` and `proposal`; an
   optional `needs_from_human: list[str]` on the Developer report (default empty, old runs load);
   rule files and the chat ledger are new files, not state changes.
2. **Autonomy default:** Standard, with Autonomous opt-in per project. Recommended.
3. **Chat model:** `strong` with a $1 per-chat cap and a `fast` switch. Recommended.
4. **Team view:** office view first with the board as the second tab (recommended), or board only.
5. **One partner:** orchestrator only (recommended), or also direct chats with each agent role.

## Order

A explain, B always-allow rules, C needs-you items, D alerts, E activity data, F team page,
G studio MCP tools with proposal cards, H chat backend, I chat page. Each is one PR, chained.
Live checks: A on a real approval, B against the coffee-receipts T3 commands, H against this repo.
Deferred: chat history across restarts, changing code from the chat, email or phone alerts,
serving the MCP over stdio, model-written bubbles.
