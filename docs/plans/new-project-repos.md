# Sub-plan: a new repository for every project

Status: DRAFT for review, with the owner's answers recorded. Nothing here is built yet.

## Goal

When you start a project, the agents create a new private repository for it on your personal GitHub
account, clone it next to your other projects, and do all development there, one `agent/<task>`
branch and one pull request at a time. Agent Studio itself only holds the orchestration and the
run records.

## Decisions (answered by the owner)

| # | Decision | Answer |
| --- | --- | --- |
| D1 | Where do project repos live on GitHub? | Your personal account. No organization. |
| D2 | Visibility | Private by default (`studio new` has a `--public` option). |
| D3 | Where are clones kept? | `~/Desktop/Github/<project-name>`, next to your other repos. |
| D4 | Token | One fine-grained token covering all your repositories. |
| D5 | Who creates the repo? | You, through `studio new`, using **your own `gh` login**. The agents' token is never used to create repositories. |

## What changes in the current design

1. **The project remembers its repo.** `ProjectState` gets `repo_path` and `repo_url`, so
   `studio resume` can never point at the wrong place. Schema change, needs your OK.
2. **`--repo` is required for `--agent full`.** Today it defaults to the current folder, which is this
   repository. After this change a full run needs a project repo, so building in Agent Studio itself
   becomes something you must ask for explicitly (`--repo .`).
3. **Scoping moves from the token into code.** A token that covers every repo is broad, so the
   orchestrator enforces the limit: the publisher refuses to push unless the `origin` of the repo it
   is working in matches the repo recorded in the project, it still only pushes `agent/` branches, and
   it never forces. The agent never sees the token.
4. **Private repos have no branch protection on the free GitHub plan.** (Public repos do.) For a private
   project repo, nothing on GitHub stops a leaked token pushing to `main`; the code-level guards above
   are the defense. GitHub Pro would add real protection and is worth considering.
5. **CI for the new repo.** The Developer may never edit `.github/` in this repo, but a brand-new
   project needs a workflow file from somewhere. In a project repo, creating or changing `.github/`
   asks you first (D7).
6. **The plan documents get committed.** The approved PRD, architecture and ADRs are opened as the new
   repo's first pull request, so the project carries its own design record.

## Further decisions (answered by the owner)

| # | Decision | Answer |
| --- | --- | --- |
| D6 | State change: `repo_path`, `repo_url` in `ProjectState`. | Yes. |
| D7 | In a project repo, edits to `.github/` ask the human instead of being denied. In this repo they stay an outright deny. | Yes. |
| D8 | Worktree folders. | `~/.cache/agent-studio/worktrees/<project>/<task>`, outside the synced Desktop and your Github folder. Overridable with the `AGENT_STUDIO_WORKTREES` environment variable. |

## PR breakdown

Each is small and tests-first, and targets `main`.

1. **Repo in state, and the push guard.** `repo_path` and `repo_url` in state. `--repo` required for
   `--agent full`. `Publisher` refuses to push unless `origin` matches the recorded repo. Worktrees move
   to `~/.cache/agent-studio/worktrees` (D8).
2. **`studio new <name> [--public]`.** Using your `gh` login: create the private repo, clone it to
   `~/Desktop/Github/<name>`, write a first commit (`README.md`, `.gitignore`), push `main`, record the
   repo in a new project's state, and print the steps to enable branch protection if the repo is public
   or the plan allows it. The `gh` calls are injected, so tests use a fake `gh` and real local git
   repositories.
3. **Policy: `.github/` asks in project repos (D7).** The protected-path rule becomes per-target: deny
   when the target is Agent Studio itself, ask in a project repo.
4. **The design record as the first PR.** After the Architect is approved, the orchestrator writes the
   PRD, architecture and ADRs into a `docs/` folder on branch `agent/plan` in the project repo and opens
   the PR through the existing publisher, with the usual gate.
5. **Docs and the live run.** Update `CLAUDE.md` and the build plan, and add an opt-in live test that
   runs `studio new`, the planning stages and one Developer task against a real throwaway repository on
   your account, then deletes it.

## Risks

- **A broad token.** Mitigated by the push guard, `agent/`-only pushes, no force, and the token never
  reaching the agent. Not eliminated: anyone holding the token can still write to any repo you own. Keep
  its expiry short and rotate it.
- **No branch protection on private repos (free plan).** See change 4.
- **Creating repos is a one-way door.** `studio new` asks for confirmation and shows the exact name and
  visibility before it creates anything.
- **Name collisions.** `studio new` refuses a name that already exists locally or on GitHub.

## Out of scope

Organizations, GitHub Apps, templates for every language, and deleting repositories.
