# Phase 3 spike findings (PR 3.0)

Run on 2026-11-04 with `scripts/spike_phase3.py` (fast model, scratch folders, about $0.01 in total).
Everything below was observed from the SDK's own hook and callback logs and from the filesystem, not
taken from what the model said, except where noted.

## Results

| # | Question | Observed | Consequence |
| --- | --- | --- | --- |
| E1 | What can the agent's shell see in its environment? | **`ANTHROPIC_API_KEY` is visible**, with or without the sandbox. A canary variable from our environment was visible too. | The agent could read our API key and any other secret in our process. This must be closed. |
| E1 | Does blanking a variable through `env=` work? | Yes: the name stays but the value is empty (`EMPTY`). | Use it for `GITHUB_TOKEN` and any other secret that is not an Anthropic credential. |
| E1 | Does `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` help? | Yes: `ANTHROPIC_API_KEY` disappeared from the shell's environment, and the run still authenticated. It only strips Anthropic and cloud credentials; the canary stayed. The CLI also forces permission mode to `default` when it is set, so allowed tools must be listed explicitly (we do that anyway). | Set it for every Developer run, and blank the other secrets separately. |
| E2 | Do hooks fire for tools that are already allowed? | **Yes**, for both Bash and Read. `can_use_tool` was **not** called for them. | A `PreToolUse` hook is the only place that sees every call. The policy lives there. |
| E3 | Can a hook block a call? | **Yes.** The command did not run, the file was not created, and the model was told why. | Hard denies work as designed. |
| E4 | Sandbox: write inside the working folder? | Allowed. | |
| E4 | Sandbox: write outside it (`/tmp`)? | **Blocked** (`operation not permitted`). | Good, a useful second layer. |
| E4 | Sandbox: read a home dotfile (`~/.zshrc`)? | **Allowed** (exit 0). | The sandbox does **not** protect secrets from being read. The policy must deny reads outside the worktree and of secret files itself. |
| E4 | Sandbox: network? | **Blocked** (proxy returned 403). | Good. Network stays closed unless the human approves a host. |
| E4 | Sandbox: `git init` in the working folder? | **Blocked**: the sandbox protects `.git`. | Plain sandboxed Bash cannot commit. |
| E5 | A non-trivial command that is not allowed, in default mode? | The hook ran, then `can_use_tool` was asked. A trivial read-only command such as `echo` ran without asking. | The ask path works; the hook is what must catch everything, including trivial-looking commands. |
| E6 | Can git commit inside a **worktree** with the sandbox on and `git` excluded from it? | **Yes**: both commits were on the task branch. | Use the sandbox with `excludedCommands=["git"]`. |
| E7 | Does a hook answer of "ask" reach `can_use_tool`, even for an allowed tool? | **Yes**, with the hook's reason passed along as `decision_reason`. | This is the mechanism for "ask the human", including for new environment variables. |

## Decisions that follow

1. **The policy lives in one `PreToolUse` hook** that returns allow, ask or deny. Asks reach the human
   through `can_use_tool`, with the reason attached.
2. **Developer runs set `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`** and blank `GITHUB_TOKEN` (and any other
   non-Anthropic secret named in a small list) through `env=`. A test checks the agent's shell cannot see
   them.
3. **Turn the sandbox on, with `git` excluded** from it. It blocks writes outside the worktree and all
   network. Because git then runs outside the sandbox, the policy is the only guard on git subcommands, so
   it must deny `push`, `remote`, `config`, force flags and anything touching `main`.
4. **The policy must itself deny reads of secrets and of anything outside the worktree**, because the
   sandbox allows reading (E4).
5. The allowed tool list is explicit (`Read`, `Edit`, `Write`, `Glob`, `Grep`, `Bash`), which the env
   scrub requires anyway.

## Not covered

- The spike used the fast model. Hook, sandbox and environment behavior does not depend on the model, but
  how reliably the Developer model stays inside the rules is tested in the live run (3.7).
- Linux sandbox behavior was not tested (this was run on macOS).
