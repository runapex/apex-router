# Project-memory compaction tool — design

**Status:** approved design (brainstorm 2026-08-21), pending spec review → writing-plans.
**Home:** `apex-router` (`scripts/memory_compact.py` + `hooks/memory-compact-nudge.sh`).

## Problem (measured, not assumed)

A Claude Code project-memory dir can grow large enough that its **`MEMORY.md`
index is re-read into every session's prefix** — the same re-read-every-turn
cache-read cost the C1–C4 cache toolkit measures. Ground truth for the motivating
repo (`~/.claude/projects/-Users-juri-kern-dev-avenger/memory/`):

- **87 memory files, 361 KB total corpus**
- **`MEMORY.md` = 14.7 KB / 120 lines, loaded every session**
- 11 natural clusters by name prefix (orphan ×14, ppm ×9, eye134748 ×6, ml_pipeline ×4, feedback ×4, managed_by ×3, fleet ×3, cdv3 ×3, ven ×2, ultron ×2, + singletons)
- Every file carries frontmatter `type` (`feedback`/`project`/`reference`) + `modified` date; corpus spans May→Aug (real freshness signal)
- The `codeqa` DIGEST for avenger is only 26 KB / 90 KB cap → the ornith Plan-A *digest* fitter is a **no-op** here; this is a different artifact and a different tool.

## What it is (boundary)

A **measure-first, advisory** tool that hierarchically compacts a project-memory
dir. It mirrors the C3 cache-handoff split exactly:

- **Auto-detect + propose** (safe, automatic): a Stop hook fires every session,
  measures the memory dir, and when it crosses an aggressive-start threshold,
  writes a **proposed** compacted index to a known path and emits a one-line nudge.
- **Apply** (mutating, human-run only): `--apply` is the ONLY path that moves/
  rewrites files. Never automatic. Memory files are hand-curated and mutation is
  irreversible, so auto-applying on a Stop event is out of scope by design.

This is "same as cache handoff": auto-fires, writes a proposed artifact, nudges —
never mutates the thing it watches unattended.

## Component 1 — `scripts/memory_compact.py` (engine)

Deterministic, no LLM in the hot path (reuses the settled Plan-A shape: static
tier + freshness-weighted fit, applied to *files* not digest sections).

**Tiering inputs (all already present, all deterministic):**
- **cluster**: file name prefix → the 11 groups (maps to existing MEMORY.md section headers)
- **tier**: frontmatter `type` — `feedback`/`project` = keep-hot; `reference` = compactible
- **freshness**: frontmatter `modified` date — older + low-tier degrades first

**Index compaction (the actual per-session win):** replace the flat 120-line index
with **cluster rollups** — one section per cluster, hot files listed with their
one-line description, cold files collapsed to a single "(+N archived — see
`archive/<cluster>/`)" line.

**Outputs (no mutation):**
- a **tier manifest** (per file: cluster, type, modified, tier verdict kept/abstracted/archived)
- a **proposed `MEMORY.md`** written to a side path (never overwrites the live index without `--apply`)
- size deltas: proposed index bytes vs current, files kept-hot vs archived

**`--apply` (mutating, human-run, git-backed):** moves cold files to
`<memory>/archive/<cluster>/`, rewrites `MEMORY.md` to the rolled-up form. Idempotent.
Refuses to run if the memory dir is not inside a git repo / not clean (so every
apply is reversible via git).

**Flags:** `--dir <memory>` (required), `--json`, `--apply`, `--min-age-days N`
(don't archive anything modified more recently), `--check` (exit 2 if index over
a byte budget — for the hook / cron).

## Component 2 — `hooks/memory-compact-nudge.sh` (Stop hook)

Same contract/guards as `cache-handoff-nudge.sh`:
- reads `session_id` + `transcript_path` from stdin; validates session_id (path-injection guard); `stop_hook_active` loop guard; once-per-session stamp
- derives the memory dir from the transcript path (`~/.claude/projects/<slug>/memory/`)
- **aggressive-start threshold** (policy, not data-fit): nudge when index ≥ ~8 KB
  OR file count ≥ ~50. Relax later per repo if premature. Env-overridable:
  `MEMORY_COMPACT_INDEX_BYTES` (default 8192), `MEMORY_COMPACT_FILE_COUNT` (default 50).
- on trigger: run `memory_compact.py --dir <memory>` to produce the proposed index,
  write it to `~/.claude/handoffs/memory-<slug>.md`, emit `additionalContext` nudge
  ("avenger memory index is 14.7 KB / 87 files — run `memory_compact --apply` to
  shrink the per-session prefix"). Never blocks, `jq` + `python3`, no LLM call.

## The measurement gate (carried from the Plan-A guardrail)

Ship it **measured**, not asserted:
1. **Index-shrink is the primary, provable claim** — proposed index bytes <
   current bytes. Report the delta; that alone justifies the tool (smaller
   every-session prefix = less cache-read).
2. **Answer-quality A/B is a SEPARATE, gated claim** — before asserting the
   compacted index doesn't hurt, run an A/B: an agent answers ≥N avenger questions
   with the full index vs the compacted index; report the **noise floor first**.
   Effect < noise → "no measured answer difference; index-shrink only" is a valid,
   honest ship outcome. Do NOT claim the compaction improves answers without this.

## Explicitly out of scope
- Auto-`--apply` on a Stop event (mutation must be human-run + git-backed).
- Porting the ornith digest fitter (no-op on avenger's 26 KB digest).
- LLM-judged per-file importance scoring (the OKF-null trap; deterministic
  tier+freshness is the settled cheaper proxy).
- Rewriting the *content* of individual memory files (compaction acts on the
  index + file placement, not file bodies).

## Test plan (TDD)
- tiering: cluster grouping, type→tier mapping, freshness ordering, `--min-age-days` guard
- index rollup: N files → rolled-up index; cold files collapsed; hot files listed
- size delta: proposed < current on a fixture corpus
- `--apply`: refuses on non-git / dirty tree; moves files; idempotent second run
- `--check`: exit 2 over budget
- hook: fires over threshold, silent under, loop/stamp/path-injection guards
  (mirror the C3 e2e matrix)
