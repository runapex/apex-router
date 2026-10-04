---
name: datapce
description: Use when choosing which model tier a delegated task should run on or before a parallel fan-out, when you want a claim labelled PASS/FAIL/BLOCKED instead of a bare "done", and before a public push or adding a dependency. Former skill names, kept for discovery — model-routing, disciplined-execution, verify-claims, evidence-labels, cross-validate, change-classification, local-references, public-repo-hygiene, dependency-vetting, unattended-loop.
---

# datapce

datapce watches every Agent and Workflow dispatch on this machine (task type, tier, outcome, tokens,
duration, upstream pressure) and keeps a ledger. It is advise-only: it never changes the model you
or the user chose. This skill does not plan and does not dispatch; it supplies numbers to the
planning and dispatch skills you already use (writing-plans, subagent-driven-development,
dispatching-parallel-agents) and covers the checking that comes after.

## Route

Feed the plan's model selection with measured numbers, then pick a tier per task.

- **Read the ledger.** `/apex` opens the pane; its Evidence section has one row per task type and
  tier: `n`, `ok%` with the failure kinds (unavailable / other), `tok(all) μ` (mean tokens billed: input, output, cache reads and cache writes), `dur μ`
  (mean duration). `/apex json` prints the same sections as JSON for a headless run. Quote those
  numbers in the plan next to each task's tier; never invent a rate.
- **Read ok% correctly.** There is no quality label yet. `ok%` means the dispatch finished and the
  model was available, not that the answer was good. Many failures are `unavailable` (a rate limit
  or outage), which says nothing about the tier's ability. A small `n` is an anecdote: with a
  handful of dispatches a 100% means little, and thirty labels would not settle it either (a proven
  route needs 35 of 35 clean at a 0.9 target, and none is proven yet). Use the ledger to compare
  cost and duration between tiers you were already considering, and to spot a tier that keeps
  failing; do not use it to justify a cheaper tier on quality grounds.
- **Cost is the measured part.** `tok(all) μ` and `dur μ` record what a dispatch cost here. Compare
  within a task type and expect allocation bias (harder work goes to bigger tiers); they do not pick
  a tier for you. A row with no tokens or duration shows `—`.
- **Never override an explicit `model:`.** If the user or the task names a model, pass it through.
  datapce never rewrites it, and neither should you. Where the pane shows `▲ <tier>: basis`, that is
  advice that differed from what ran; it is informational.
- **Delegate, don't switch.** Changing your own model mid-session evicts the prompt cache. Hand
  uneven subtasks to subagents with a tier each.
- **Pressure before fan-out.** The band (`apex ●LEVEL …`) and the pane's Signals section show
  upstream pressure. GREEN: fan out. AMBER: keep the fan-out small and send explore and mechanical
  work one tier down unless a model is named. RED: serialize heavy (opus) work; the pane shows the advisory heavy-spawn rate.
  No band (headless `claude -p`) and the backend is installed? Run `apex-router pressure --check`
  first: exit 0 GREEN, 1 AMBER, 2 RED, 3 UNKNOWN, 4 usage error.
- **Lane breakers.** When the pane's Signals section shows a lane breaker `open`, send that work to
  a frontier tier for a while instead of retrying the lane.
- **Backend extras (only if `apex-router` is installed).** `apex-router route-advise --json` gives
  a cost-only verdict per task type (cheap-start vs heavy-start; it assumes the cheap output was
  acceptable, so it is not a quality claim). `apex-router review-preread` takes a diff path (or `-`
  for stdin) and optional `--requirements FILE`, and prints claims for a reviewer to verify.

Defaults, only as the no-data fallback when the ledger has nothing for a task type. When the
planning skill has its own Model Selection, that wins; do not compete with it:

| task type | tier | why |
|---|---|---|
| explore / search / summarize | sonnet (haiku for mechanical lookups) | breadth, low stakes |
| generate / refactor | sonnet; opus when the design is unsettled | volume vs judgement |
| debug | opus | the first theory is often wrong |
| review / verification | opus, independent of the author | disagreement is the signal |
| synthesis / final report | the session's model | it holds the context |

## Verify

Five gates, in order, for anything where the first idea might be wrong. Skip them for a one-line
edit; never skip them for a claim someone will act on.

1. **Scope before work** — restate the goal, the constraints and what "done" means; list unknowns.
2. **Evidence before reasoning** — read the data, the code, the log; do not reason from memory
   about things you can look at.
3. **Reason adversarially** — attack your own answer: what would make it wrong, what did you not
   check, which number would change the conclusion.
4. **Verify before declaring done** — run the check that would fail if you were wrong, after the
   last edit; grep the number back to its source.
5. **Report calibrated** — say what you checked, how, and what you did not.

Grep the number: before quoting a count, ID, filename or metric that came from a model (a subagent,
a local model, or you), find it in the source. A fluent figure is not a fact.

Label every claim in a handoff, PR description or "done" message:

| label | means |
|---|---|
| **PASS** | the check ran on the final state and passed |
| **FAIL** | the check ran and failed |
| **PARTIAL** | ran on a subset; say which |
| **BLOCKED** | could not run; say why |
| **INCONCLUSIVE** | ran, result does not decide the question |
| **STALE** | ran before the last edit |
| **ASSUMED** | not checked; believed for a stated reason |
| **N/A** | does not apply here; say why |
| **WAIVED** | skipped by an explicit decision; say whose |

A gap in verification must never read as a pass.

## Review

- **Independent reviewer.** For a non-trivial change or a report someone will act on, dispatch a
  fresh reviewer (no shared context, opus tier) with the diff, the requirements and the test output,
  and ask it to find what is wrong, not to confirm. Fix or rebut each finding in writing.
- **Panel for risky changes.** For high blast radius, ask several independent models to classify
  the change on three axes: change class, requirement fit, blast radius. Trust where they agree;
  look hardest where they disagree.
- **Pre-read is optional.** With the backend, the local pre-read (see Route) hands the reviewer
  claims to check; it never replaces the reviewer.
- **Ground in your own sources.** When a book, paper or code sample you have locally covers the
  question, cite it rather than paraphrasing from memory.
- **Long sessions.** `/apex handoff` adds the structured handoff template to the context (it writes no file); fill
  every field before starting fresh, and do not paste it while any field is still unfilled.

## Ship

- **Public-repo hygiene.** Before pushing to a shared or public repository, scan the added lines for
  secrets, internal identifiers and personal paths, and check the committer identity. Deleting a
  leak later does not remove it from history.
- **Dependency vetting.** Before adding, replacing or upgrading any third-party package (runtime,
  test or build), check: official index; exact pinned and hash-locked version; maintained (a release
  in the last 24 months); permissive OSI license; no known vulnerability per an archived OSV query;
  scoped to the component that needs it. Re-scan the whole lock before a release.
- **Unattended loops.** When running without a person (nightly job, `/loop`): on a blocker, document
  it, take one advisor round, re-check, act, verify. Never loosen a failing gate to go green. Hard
  limits (deleting data, force-push, spending, publishing) are never unlocked by agreement. Commit
  scoped changes on a dated branch and leave a morning report.
