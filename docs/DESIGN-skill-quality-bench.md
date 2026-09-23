# DESIGN — skill-quality benchmark (the SkillOpt borrow)

**Status:** BUILT (measure-only). Two Codex/astra adversarial passes; all findings fixed at
ground truth. `src/apex_router/skillopt_bench.py`, CLI `apex-router skill-bench`,
`tests/test_skillopt_bench.py` (15 tests), seed skill `benchmarks/skills/edgecase_discipline.md`.

## What this is, and why

SkillOpt (microsoft/SkillOpt) brings the one axis apex-router structurally lacks: treat a compact
skill document as the **trainable state of a frozen model** and measure whether it raises task
success (apex is measure-only on cost/cache/routing — it can pick a cheaper model but never make an
answer *better*). We borrow that MECHANISM. We do **not** borrow SkillOpt's acceptance gate — it
accepts on a POINT ESTIMATE (`cand_score > current_score`) with a `semantic_density` Goodhart bonus,
and keeps its rigorous instrument (`evalkit`: McNemar + bootstrap CI) reporting-only. apex already
has the stronger gate SkillOpt declined to wire in (`gate.run_gate`: sample floor + out-of-sample
confirmation + BH-FDR + replication across windows), so the borrow is **SkillOpt's quality axis
behind apex's gate**.

## The reframe

apex's gate pairs "candidate MODEL vs incumbent MODEL on the SAME step". For skills:
"candidate SKILL vs no-skill BASELINE, SAME frozen model, SAME step" → `delta = score(with) −
score(without)`. Encoded as pseudo-model ids (`skill:<name>` vs `skill:baseline`); the step context
is identical across arms, the only difference is the injected skill. This feeds `run_bench` →
`_paired_cell_evidence` → `run_gate` with **zero gate changes**. "Over time" = each run is a capture
`window_id`; a skill promotes only after replicating across ≥ M windows on **fresh** tasks.

## Soundness (what the two adversarial passes hardened)

The gate is only as trustworthy as the evidence fed to it. Both passes attacked exactly that; the
guards below are the result (each has a ground-truth regression test):

- **Objective grading only** — executable tests (`run_python_tests`), no LLM judge. Scoped honestly:
  it is NOT an OS sandbox (hostile `os._exit(0)` can forge a pass), so this measures a *non-hostile*
  skill's effect on a *trusted* model, not adversarial code.
- **Real replication, not pseudoreplication (F4)** — re-running the SAME task in another window is
  rejected (the base bench's duplicate guard); replication must come from DISTINCT tasks.
- **Paired windows only (F3)** — a window counts toward replication only if that step has BOTH arms;
  a candidate-only window (baseline dropped by a flaky rollout) contributes nothing.
- **Window isolation (pass2 F1)** — a task may appear in AT MOST ONE window, so a task's baseline and
  skill rows can never pair ACROSS windows (the complementary-failure bypass).
- **Campaign identity (F5 + pass2 F5)** — `corpus_snapshot` binds the skill BYTES + frozen model id
  (`campaign_snapshot`), and grading refuses rows that mix snapshots/runs across the split — so a
  harmful new revision can't promote on an old winner's windows. The CLI passes `model=model_id` so
  the ACTUAL model matches the bound id.
- **Real family FDR (F6)** — one skill is one hypothesis (BH-of-one = raw threshold). A *campaign* of
  many skills uses `grade_skills_family`, which runs the gate over ALL cells together so BH-FDR is
  real; duplicate skill ids are deduped (no extra BH slots).
- **Honest persistence (F8)** — CLI dedups rows so a rerun can't multiply history.

## Measure-only boundary

Reports whether a skill lifts quality; **never deploys it**. Autonomous skill deployment is the
`DESIGN-whitepaper-research-loop.md` self-evolution loop, BLOCKED pending an evidence verifier +
capability manifest + deploy supervisor. A human reads the verdict and adopts the skill at the
skill/agent layer.

## Automation (end-to-end, no restart)

The feature runs **on a schedule with no new agent**: `skillopt_bench.run_nightly()` is called from
`nightly.run()`, which the existing daily launchd unit (`com.apex-router.daily`, 09:00) invokes via
`watch --run-daily`. The chain is:

```
launchd 09:00  →  watch --run-daily  →  nightly.run()  →  skillopt_bench.run_nightly()
                                                            → digest in ~/.apex-router/offload_daily.md
```

Each night it: discovers `benchmarks/skills/*.md`; draws a **persistent DISJOINT** task slice for the
day (`~/.apex-router/skillbench/window_tasks.json` — each task belongs to exactly one window, so the
gate never sees pseudoreplication); rolls the resident local Ornith tier on both arms; dedup-appends
rows to `~/.apex-router/skillbench/<skill>.rows.jsonl`; grades the whole history behind apex's gate;
and appends the verdict to `~/.apex-router/skillbench/ledger.jsonl`. Fail-open: no local model, no
skills, or an exhausted corpus each degrade this digest section, never the daily run.

**Install state (this box):** `com.apex-router.daily` was NOT loaded (only `ornith-daily`, which runs
the offload report, was) — so `nightly.run()` (and route-advise) weren't actually scheduled. Fixed by
`apex-router watch install --no-drain` (the Ornith stack already drains the queue; `--no-drain` avoids
two daemons on one GPU inbox). Verified live: `launchctl kickstart com.apex-router.daily` ran the full
path and the skill-bench section landed in the digest. **No proxy/machine restart is required** — the
daily unit runs the dev tree via `PYTHONPATH`, so code changes take effect on the next fire.

**Corpus note:** the seed corpus is 11 tasks — enough for ~2 disjoint windows, then `run_nightly`
honestly reports `corpus exhausted` (it never fabricates fresh evidence). Add more tasks to
`benchmarks/*.jsonl` to keep accruing replication over more nights. A skill also can't promote if the
frozen model already aces the tasks (Δ=0, no headroom) — that is honest measurement, not a failure.

## Manual usage

```bash
apex-router skill-bench --skill benchmarks/skills/edgecase_discipline.md \
  --tasks benchmarks/codegen_hard.jsonl --window "$(date +%F)" \
  --ledger ~/.apex-router/skill_ledger.jsonl \
  --rows-in ~/.apex-router/skill_rows.jsonl --rows-out ~/.apex-router/skill_rows.jsonl
```

Run it on **different task sets** across days; the ledger accumulates windows and the verdict flips
to PROMOTED only when the lift replicates behind apex's gate. Reruns on the same tasks are rejected
as pseudoreplication — that refusal is the point.

## Not done (backlog)
- OS-sandboxed grading (the current subprocess is not a security boundary).
- A larger, content-diverse task corpus (11 seed tasks is enough to wire the gate, not to earn a
  real promotion — the harness is the product; feed it more tasks over time).
- An adaptive-holdout guard: `_split_for` is a fixed holdout for a fixed benchmark, not protection
  against a human over-fitting a skill to the confirmation tasks across many edits.
