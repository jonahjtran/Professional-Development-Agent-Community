# Plan: explanations, "needs you" alerts, and a codebase chat

Three additions to the web UI, built as a chain of small pull requests. Each PR is tests first and
leaves `main` working. Cost of the helper calls is tracked separately and shown, so it never hides
inside a project's spend.

## 1. Explain every approval request (PR A)

When the Developer asks to run something, the inbox shows a plain-English card: *what it is trying
to do, why it probably wants to, what it could change, and how risky it looks*. It is advice only.
It never approves or denies anything, and the policy verdict in `permissions.py` is unchanged.

- `orchestrator/explain.py`: one cheap call (the `fast` tier, about a tenth of a cent) given only
  the command or request summary, the policy reason, the task title and the project idea. It never
  gets file contents, environment values or tokens, and the command text is cut at 2,000 characters.
- Asked lazily by the page (`GET /api/projects/<p>/questions/<id>/explanation`) so the Developer is
  never held up, then cached next to the question (`pending/<id>.explanation.json`).
- Output is a small schema (`what`, `why`, `effect`, `risk: low|medium|high`, `unsure`), escaped by
  the server like all agent text. The card says "unsure" when the command is hard to read.
- If the call fails or there is no API key, the card says so and the raw command is still shown.
- Spend goes to `runs/<project>/assistant_cost.json`, shown on the project page as "Helper calls".

## 2. Tell me when only I can act (PRs B and C)

Today the app notifies on any new question. This adds a second, louder kind: **Action needed**,
for things the agent cannot do for you. Examples: merge pull requests, put a token in `.env`,
install `gitleaks`, run `npm install` yourself, edit a protected file, reply to a question only you
know the answer to.

- PR B, the sources and the list. A new pending kind `todo` with a title, detail and "Mark done"
  button, shown in the inbox, on the project page and as a count in the nav. Sources, all detected by
  code: a merge check that cannot proceed, a tool the QA stage needs that is missing, a token
  expiring or missing (from `/health`), a Developer request the policy refuses outright that the
  task still depends on, and an optional `needs_from_human` list in the Developer's final report.
  Developer prompt v4 explains when to use it.
- PR C, the alerts. A separate notification title ("You need to do something") with its own sound
  on macOS (`--mac-notify`), plus a browser notification, the tab title count ("(2) Action
  needed") and a red nav badge, so it is visible without the terminal. Ordinary approvals keep the
  quiet notification. Each alert fires once per item.

## 3. A chat about the code (PRs D and E)

A chat page where you ask questions about Agent Studio itself or any project it built, such as
"why did QA fail?", "where is the budget check?" or "what does this approval want?".

- PR D, the backend. `POST /api/chat` (a turn) with Server-Sent Events for the reply. It runs the
  Claude Agent SDK with only `Read`, `Glob` and `Grep`, `cwd` set to the chosen repository (Agent
  Studio's own checkout, or one project's clone). Every call goes through the same
  `permissions.py` policy: secrets (`.env`, tokens, keys) are refused, nothing outside the repository is
  readable, no writes, no shell, no web. It is given a short summary of the chosen project (stage,
  spend, open questions) and the repo's `CLAUDE.md`. Sessions are kept in memory and resumed by id.
  A per-chat dollar cap in `config.yaml` (`assistant.max_chat_usd`) ends a runaway chat. The reply
  streams as plain escaped text and is replaced by the server-rendered Markdown when the turn ends.
- PR E, the page. A Chat item in the nav: repository picker, message list, input, cost so far,
  "New chat". An "Ask about this" button on every question page and on failed or paused projects
  opens the chat already holding that question or the last trace events as context.

## Decisions to confirm before building

1. **Schema changes** (CLAUDE.md says ask first): a new pending kind `todo`, and an optional
   `needs_from_human: list[str]` (default empty, so old runs still load) on the Developer report.
2. **Model for chat:** the strong tier (best answers, more cost) or `fast`. Recommended: `strong`
   with a visible per-chat cap of $1, and a switch to use `fast`.
3. **Chat scope:** read-only, as above. Letting it make changes is deliberately out of scope.

## Order and size

A explain, B todo items, C alerts, D chat backend, E chat page. Each is one PR, chained so each
builds on the one before. Live checks: PR A against a real approval, PR D against this repository.
Deferred: chat history across restarts, changing code from the chat, other notification channels
(email, phone).
