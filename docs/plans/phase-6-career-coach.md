# Phase 6: career coach (sub-plan)

Status: draft for review. Nothing here is built yet.

## Goal

After a project passes QA, turn what was really built into career material, with a human approving
every place it appears:

1. **Always (behind a pull request you merge):** a proper `README.md` and `docs/case_study.md` in
   the project's own repository.
2. **Only if the project suits your goals, and you say yes:** a resume change.
3. **Only if it suits your goals, and you say yes:** an entry on your portfolio website.
4. **Optional, each confirmed:** the repository's GitHub description and topics, and a profile
   README that lists your projects.

The coach is allowed to say "this does not belong on your resume." Skipping is a normal outcome and
comes with a reason.

The coach knows how consulting resumes are read: one page, results and leadership first, and a technical side project often earns one line or none. It can recommend the website and GitHub only, which is a good outcome.

Out of scope for this phase: the gap analysis, project recommendations and the job-postings MCP
server from the build plan. They feed the Product Owner, not the career material, so they get their
own phase later.

## Ground rules

- **No evidence, no claim.** Every bullet and every number must trace to an `Evidence` item built
  by code from the project's real artifacts: merged PRs, the QA report, eval results, the cost
  report, the trace, PRD metrics. A number that appears in no evidence item fails validation.
- **The coach never invents facts about you.** Your role, team size, dates and employers come only
  from your resume data or from answers you give. Where it needs one (for example "how many people
  used it?") it lists a question at the gate, and your answer is stored as evidence of type `human`.
- **Honest scope.** The eval set is 12 cases; a bullet that mentions its accuracy must say so.
- **Nothing is published without a gate, and nothing goes live by itself.** Every change is a
  pull request. For the website, merging the PR is what deploys it (the site deploys from `main`
  with GitHub Actions), so you control the moment it goes live.
- **The resume is never edited in place.** The coach proposes changes against a structured copy of
  your resume; you apply them to the real file.
- **One page.** Your resume fills one page, so an addition must name what it replaces.

## Decisions

| # | Decision | Recommendation | Why |
|---|----------|----------------|-----|
| C1 | Where does your resume data live? | A **private** repo `career-profile`, cloned next to your other repos, found through `AGENT_STUDIO_PROFILE`. It holds `resume.yaml`, `goals.yaml` and later the gap reports. | This repository is public, and a resume holds contact details. A private repo is versioned, backed up and keeps the history of what was proposed. |
| C2 | What is the resume "source"? | **LaTeX on Overleaf, downloaded as a PDF.** There is no local `.tex`, so the coach does not edit a file. It produces **paste-ready LaTeX snippets** in your template's own macros (you give me one sample entry so the snippets match), plus a plain-language list of what to add, replace or cut. You paste them into Overleaf and compile. `resume.yaml` in the private profile repo (seeded from your PDF) is the data the coach validates against. If you later turn on Overleaf's GitHub sync, the snippets can become a PR. | There is nothing local to patch. Snippets in your own macros are the nearest thing to a safe edit, and the structured copy keeps the claim checks working. |
| C3 | How does the website change? | A **pull request** on `jonahjtran.github.io` that adds an entry to `src/data/projects.js`, in the shape your entries already use. You merge it to publish. | Merge equals deploy, so you keep the final say after the in-pipeline approval. |
| C4 | Approval style | One checklist gate for the stage: approve or skip each item (resume change, README, case study, website entry, description and topics). "Request changes" re-runs the coach. | The current gates are all-or-nothing. This changes gate behavior, so it needs your sign-off. |
| C5 | Resume variants and tracks | One consulting resume (the file you gave me). The two career tracks are **strategy consulting** (MBB and strategy firms such as Strategy&, EY-Parthenon) and **tech consulting**; the coach judges a project against each. | Your goals are consulting only. A technical project may matter for tech consulting and be a skip for a strategy resume. |
| C6 | Model | Strong tier for the judgement and writing, one call. Code builds the evidence. | Writing quality matters here, and one call per project costs well under a dollar. |
| C7 | GitHub profile extras | Ask first, each time. They use your own `gh` login, not the agent token. | Repository settings need permissions the token does not have, and a new repo is a visible action. |

## The flow

```
QA approved
  -> code: build evidence.json from the project's artifacts (PRs, QA report, evals, cost, PRD)
  -> code: load resume.yaml, goals.yaml (private repo), the website's projects.js, the project's README
  -> coach agent (strong, no tools): career_plan.json
        fit per track (include / skip, with reasons), README, case study, resume changes
        (add / replace / skip, each citing evidence and naming what it replaces),
        website entry or none, questions for you
  -> code: validate (evidence ids, numbers, one-page budget, entry shape)
  -> checklist gate: you approve or skip each item; answers to its questions become evidence
  -> code, per approved item, each as a pull request:
        project repo     README.md + docs/case_study.md     (branch agent/career-docs)
        career-profile   resume.yaml + resume_changes.md     (private)
        website          projects.js entry, after `npm run build` passes
        optional         description and topics, profile README (your own login, confirmed)
```

## PRs

All target `main`, merged in order.

1. **Evidence and schemas.** `Evidence`, `ResumeBullet`, `ResumeChange`, `WebsiteEntry`,
   `CareerPlan`, with the rules above as validators (bullet needs evidence, numbers must appear in
   metric or eval evidence, an add to a full section must name what it replaces). Code that builds
   `evidence.json` from a project's artifacts. A loader for the private profile repo, and
   `resume.yaml` and `goals.yaml` seeded from your PDF (kept in your private repo, not here).
   Needs your sign-off on the schemas.
2. **The coach agent.** `agents/prompts/career_coach.v1.md` (including the pushback rule: it must
   say when a project is weak for your goals), a strong-tier spec, the stage spec that stores and
   renders the plan, and the inputs it may read.
3. **The checklist gate.** Per-item approve or skip, answers to questions saved as `human`
   evidence, and the pipeline moving from QA to the coach to done. Needs your sign-off (C4).
4. **Publishing.** The project docs PR, the career-profile PR, the website PR (deterministic
   insertion into `projects.js`, a build check, and the push guard widened from one repo to a short
   allowlist: the project, the website, the profile repo), and the optional GitHub extras.
5. **Evals, live test and docs.** Programmatic checks on the coach (every bullet has evidence, no
   unsupported number, skips a weak project, asks instead of inventing), a live run on this very
   project that stops before anything is published, and `CLAUDE.md`.

## Done when

- A finished project produces a README, a case study, and a plan with a stated fit for each track.
- A bullet with a number that is not in the evidence is rejected before you see it.
- Declining an item at the gate publishes nothing for it. Approving opens one PR per target and
  nothing else.
- The website PR changes only `src/data/projects.js`, builds, and is not merged by the orchestrator.
- The resume change names what it replaces, and the original resume data is never overwritten, and the snippets use only macros from your template.
- ruff, mypy and pytest are green on every PR, and no personal contact detail is committed here.

## Risks

- **The model overstates results.** Evidence-only claims, the number check, and the prompt rule to
  state the eval set size. The coach eval measures it.
- **Personal data in a public repo.** Profile data lives in the private repo; tests use made-up
  data; output that would be committed here is checked for the contact details in your resume.
- **A bad website entry breaks the site.** The entry is generated by code in your existing shape,
  the site's own build must pass, and you merge it yourself.
- **The token reaches more repos.** The guard allows exactly the recorded project repo, the website
  and the profile repo, and only `agent/` branches, never `main` and never forced.
- **Your manual edits collide with the coach's.** Each run branches from the latest `main` of the
  website and profile repos.
