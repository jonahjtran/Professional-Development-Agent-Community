---
name: learn-as-you-build
description: Turns AI-assisted coding into learning. Use this skill whenever Claude writes or changes code that goes beyond obvious boilerplate, especially code that uses a tool, library, framework, protocol, pattern, or concept the developer may not know yet (check docs/learning/familiarity.md). Explains the code and the new concept in the chat, keeps a private learning log in docs/learning/ that is never committed to git, and builds a student-style study sheet when the project is finished. Also use when the user says "teach me this", "explain what you just wrote", "update my learning log", "wrap up the project", or "make my study sheet", even if they don't name the skill.
---

# Learn As You Build

The developer using this skill builds with AI on purpose, and also wants to stay a growing engineer rather than a person who ships code they can't explain. Your job is to make sure that every non-trivial thing you write leaves them understanding something new. This works only if it stays proportionate: explanations that are long, patronizing, or constant get ignored, so match the effort to how new the material actually is.

## Setup (first run in a project)

1. Make sure `docs/learning/` exists and is ignored by git. Add this line to `.gitignore` if it isn't already there (create the file if needed):
   ```
   docs/learning/
   ```
   If `docs/learning/` files are already tracked by git, tell the user and suggest `git rm -r --cached docs/learning` rather than doing it silently.
2. Make sure `docs/learning/familiarity.md` exists. If not, copy `assets/familiarity.seed.md` from this skill folder. Tell the user once that it's seeded from their background and they should edit it. This file is the baseline for deciding what counts as new.
3. Make sure `docs/learning/log.md` exists. If not, create it with the header from `assets/log-entry-template.md`.

The point of the gitignore is that this folder is the developer's private notebook, including half-formed understanding and mistakes. Never stage, commit, or push anything in it.

## When to trigger

Decide per change, using `docs/learning/familiarity.md` as the baseline. Explain when the code does one of these:

- Uses a tool, library, framework, protocol, API, or language feature not listed as familiar.
- Introduces a design pattern, architecture choice, or algorithm with a non-obvious "why".
- Contains logic where the reason it works is not apparent from reading it (concurrency, auth flows, caching, query planning, prompt/agent orchestration, build and deploy config, and so on).
- Fixes a bug whose root cause teaches something.

Stay quiet for obvious or already-familiar work: renaming, simple CRUD, wiring up a component the developer has built many times, formatting, trivial config. If you are unsure whether something is new to them, err toward one short line ("quick note: this uses X, want a walkthrough?") rather than a full lecture.

## What to do when it triggers

**1. Explain in the chat, right after the code.** Keep it tight:
- What the code does, in plain language, tied to the specific lines or files.
- Why it was designed this way, and what the alternative would have been.
- For a new tool or concept: a short walkthrough covering what it is, the problem it solves, how it works at the level needed to use it, and the one or two gotchas that trip people up. Use a small concrete example from this repo instead of a generic one.
- Name the concept with its real term so the developer can search it later.

Aim for the explanation to be readable in under a minute. If the concept deserves more depth, say so and offer it instead of dumping it.

**2. Log it** when the code or concept is new to the developer, in `docs/learning/log.md`, using the format in `assets/log-entry-template.md`. Append, newest at the bottom. Include the date, the files touched, the concept, a short explanation in the developer's own level of vocabulary, key terms, and a one-line "check yourself" question. Those questions and terms are raw material for the study sheet later, so write them with that in mind.

If a concept needs more than a short entry, put the deep dive in `docs/learning/concepts/<concept-slug>.md` and link it from the log entry.

Don't log things that were only explained because you weren't sure. If the developer says they already knew something, add it to `familiarity.md` so future sessions skip it.

**3. Keep the working loop intact.** The explanation should not block the task. Finish the code, then explain. Don't ask the developer to confirm understanding before continuing unless they asked to be quizzed.

## Project completion: the study sheet

When the developer says the project is done (or asks for the study sheet or notes), read `docs/learning/log.md`, `docs/learning/concepts/`, and `familiarity.md`, then write `docs/learning/study-sheet.md` using `assets/study-sheet-template.md`.

The sheet should feel like a good student's cram sheet for an exam on this project:
- Organized by concept, not by date, grouping related log entries.
- Each concept gets a "one-liner you could say in an interview", the key terms, a small diagram or code snippet from the project, common pitfalls, and where it lives in the repo.
- A flashcard section built from the "check yourself" questions, with answers.
- A "could I explain this?" self-test list, plus a short list of things worth studying further, ranked by how central they were to the project.

Write from what the developer actually encountered in this project. Don't pad with generic textbook material that never appeared in the repo. Tell the developer where the file is and mention it is ignored by git, so they can copy it elsewhere if they want to keep it.

## Reusing in other projects

This skill is self-contained. To use it elsewhere, copy the `learn-as-you-build/` folder into that project's `.claude/skills/`. The first-run setup recreates the learning folder and gitignore entry. Carry over `familiarity.md` if you want the same baseline, since it describes the developer rather than the project.
