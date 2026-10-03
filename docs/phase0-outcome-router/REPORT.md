# Phase 0 — outcome-router readout (K3 handoff execution)

**Date:** 2026-08-24 (local) · **Executor:** kimi-k3 orchestrating; data analysis + join
pipeline delegated to kimi-k2.7-code subagents; all headline numbers recomputed by the
orchestrator on a **frozen snapshot** (telemetry rows with `ts ≤ 1787625600`
= 2026-08-25 04:00 UTC; 1,270 request rows) — the live log grows continuously because
measured sessions flow through the proxy, so every §3 figure below comes from that one
frozen basis, not a moving one.
**Verdict: NO-GO on training a router this cycle. CANNOT-DECIDE per guardrail 5 — the
label×feature join produces ZERO rows, and the only per-cell rate that clears the
sample floor shows a mechanically periodic pattern consistent with a synthetic
generator, not organic outcomes. The opportunity itself is confirmed real (§3). The
deliverable that matters this cycle is the instrumentation fix list (§5).**

---

## 1. Deliverable 1 — labeled training table (route_log ⋈ conformance)

**Pipeline shipped:** `join_table.py` (this directory) implements the LOG-SCHEMAS
recipe: join on `task_type` equal AND `|Δts| ≤ 300 s`, nearest-ts match, at most one
conformance row per route_log row. Strict parsing (malformed lines counted, skipped);
conformance rows with `surface=agent, matched=null` would be excluded from every
denominator (honesty invariant — currently vacuous: no agent-surface rows exist yet);
`ts=null` route_log rows counted and excluded from the time-join.

**Emitted schema** (`labeled_table.jsonl`):
```json
{"ts": 1787540000.0, "task_type": "extract", "model": "kimi-k2.7-code",
 "escalated": false, "label": "easy", "surface": "resolve",
 "requested_tier": "sonnet", "resolved_model": "claude-sonnet-5", "matched": true}
```
`label` = `hard` if `escalated` else `easy` (measured cheap-model outcome — the label
swap the handoff calls for).

**Result: the table is EMPTY — 0 joinable pairs.** Two independent causes, both
verified against the raw logs:

| Cause | Evidence |
|---|---|
| **84% of labels have no timestamp.** 32/38 route_log rows carry `"ts": null`. | These were written **outside `log_outcome`**: the writer defaults `ts=None → time.time()` (`src/apex_router/route_log.py`), so it cannot emit a JSON-null `ts`. The identity of the out-of-band writer is unknown; the rows carry models `cheap`/`qwen3.8`/`ornith` and empty notes. |
| **The conformance log holds no organic traffic.** All 7 rows are one synthetic batch. | 5.3 ms timestamp cluster @ 1787621882, `resolved_model` = placeholder `"S"`/`"O"`, all `matched=false` — an emitter e2e probe, not real dispatches. The 6 real-ts route_log rows sit ~20.9 h away, far outside the 300 s join window. |

**Per-task-type rates, with a provenance warning.** `apex-router route-advise`
(Wilson 95% CI, min_n=30 floor, cost break-even 0.80 at cost-ratio 5) reports:

| task_type | n | escalated | rate | 95% CI | verdict | Provenance |
|---|---|---|---|---|---|---|
| extract | 30 | 6 | 0.20 | [0.10, 0.37] | COST_FAVORS_CHEAP_START | ⚠ **probable synthetic — see below** |
| debug | 5 | 1 | 0.20 | [0.04, 0.62] | INCONCLUSIVE (n < floor) | mixed (real-ts rows present) |
| adhoc / e2e_probe / generate | 1 each | — | — | — | INCONCLUSIVE (n < floor) | unknown |

⚠ **The `extract` cell must be quarantined.** All 30 `extract` rows are null-ts
out-of-band writes (`model=ornith`, empty notes), and their escalations fall at row
positions **6, 11, 16, 21, 26, 31 — exactly every 5th row** (verified against the raw
log). Organic escalation is not perfectly periodic; this is the signature of a scripted
generator. The COST_FAVORS_CHEAP_START verdict is therefore a verdict about a
synthetic file and must not drive routing. Consequence: **no task-type cell currently
has a trustworthy, floor-clearing escalation rate.**

## 2. Deliverable 2 — baseline outcome-router + out-of-sample metrics

**Not trained. Any AUC or cost-savings number on this corpus would be a faked metric**
(guardrail 5: prefer CANNOT-DECIDE). Blocking reasons, in order:

1. 0 joined rows → no feature-bearing training examples at all.
2. The only cell at the sample floor is probable-synthetic (§1); the largest
   non-quarantined cell is n=5.
3. The conformance feature source contains no organic rows (§1), so no honest
   feature matrix exists.

**Eval design, specified now so it is ready when labels flow** (guardrails 1–3 baked in):

- **Split:** session-level, time-ordered (train = earlier sessions, test = later).
  Never a random row shuffle — turns within a session are correlated and would leak.
- **Baseline router:** logistic regression (then GBDT) on `(task_type, context_size,
  venue, surface)` → P(escalated). Competitor = the CURRENT context-size venue rule
  (`resolve --venue`), which is the bar to beat, not a strawman.
- **Metrics:** out-of-sample AUC; cost-savings vs the venue rule with a bootstrap CI;
  per-task-type decisions FDR-corrected (BH) across cells; promotion gate strictly
  out-of-sample. Cold cells return the safe default, never a hallucinated score.
- **Kill criterion (restated, with the caveat the number deserves):** if the learned
  router cannot beat the context-size rule out-of-sample above the noise floor, keep
  the rule. The rule's existing win is a **counterfactual price ratio** (§3: 2.91×),
  i.e. realized only if k2.7-code quality holds on downshifted traffic — which is
  precisely what the escalation labels exist to measure. A negative result is valid.

## 3. Deliverable 3 — telemetry verification + niche positioning

**Frozen snapshot** (cutoff above). Two scopes are reported explicitly because the
codex client is **no longer single-model**: mix = kimi-k3 337 req (88.5%), kimi-k2.7-code
37 req (9.7%), unresolved-model 7 req (1.8%); 9 distinct sessions. The k2.7-code
minority is consistent with the venue's `<250k` downshift rule being live — the
DECISION doc's "100% k3" held at pin time (252 req / 2 sessions) but does not hold now.

| Metric | DECISION doc (pinned) | Frozen snapshot (this pass) | Holds? |
|---|---|---|---|
| Codex client model mix | 100% k3 | 88.5% k3 / 9.7% k2.7-code / 1.8% unresolved | ✗ changed (downshift rule live) |
| k3 traffic: tokens | 37.9M cache-read + 38.6M fresh + 137k out | 52.8M cache-read + 54.1M fresh + 194k out | ✓ (larger; same shape) |
| k3 traffic: spend at list rates | ≈ $129 | **$181.18** | ✓ (grew with volume) |
| k3 per-request context | p50 346k, p95 495k, max 513k | p50 342k, p95 552k, max 578k | ✓ |
| k3 requests > 250k ctx | 73.8% | **67.7%** (all-codex scope: 59.8%) | ✓ regime holds |
| Counterfactual: same k3 traffic on k2.7-code | ≈ 2.9× cheaper | **$62.25 vs $181.18 = 2.91×** | ✓ **ratio reproduced** |
| Claude-side regime | 96.5% cache-hit, 0 busts | 99.97% cache-hit (889 req), 0 busts | ✓ cache-dominated |

The agentic-turn regime is confirmed: median per-request context ~342k on the k3
traffic, ~⅔ of requests above the 250k downshift ceiling, economics dominated by
cache-read + fresh input. This is the workload class general routers (RouteLLM,
FrugalGPT cascades, AutoMix, Martian/Not Diamond/Unify/OpenRouter) do not route:
tool-call loops with huge cached prefixes where a "turn" is not standalone. **The
contribution framing stays as the handoff puts it: the RouteLLM recipe, correctly
targeted (measured-outcome label), on the agentic-turn workload — not "unexplored
territory."**

**Blind spots named (guardrail 4):**

- **The escalation label can't see heavy-started cells.** Labels exist only where a
  cheap start was attempted; tasks dispatched heavy from the start have no measured
  cheap outcome → selection bias toward cheap-eligible work.
- **Escalation embeds the dispatcher's judgment.** A human/model decided the cheap
  output was inadequate. It IS the decision-relevant outcome (why it's the right
  label), but it is not a purely objective correctness oracle.
- **A per-cell rate can't see *why* it escalated** — context size, the strongest
  candidate feature, is absent from both log schemas (F4, §5).
- **The counterfactual cost number can't see quality.** The 2.91× assumes k2.7-code
  output is acceptable on the same traffic; the cost model and the label model must
  ship together or the savings figure is self-confirming.
- **A rate readout can't see provenance.** Null-ts rows enter `read_rates()` without
  any flag — the periodic `extract` cell (§1) produced a clean-looking verdict until
  the raw rows were inspected.

## 4. Deliverable 4 — go/no-go on the frontier-difficulty cold-start feature

**NO-GO for this cycle.** Preconditions and the design for when they're met:

1. **Precondition: warm cells must exist first.** A cold-start feature can only be
   validated against cells that later accumulated measured outcomes. Today there are
   zero cells with joined features and zero trustworthy floor-clearing cells, so there
   is nothing to validate a scorer against. Shipping it now = guardrail 3 violation
   (a self-authored difficulty map feeding itself with no independent signal).
2. **Precondition: F1 + F4 fixed (§5)** so outcomes arrive era-sliceable and with
   context features.
3. **Design when unblocked:** frontier score computed ONLY for cells with `n < floor`
   escalation samples; **logged prospectively as a feature at dispatch time** (before
   the outcome is known), never used as a training target. Validation = as those cells
   accumulate real outcomes, test whether score-guided dispatch beat the safe default
   (heavy tier) on measured outcomes. Warm cells are never touched by the scorer. If
   it can't beat the safe default on cold cells, drop it.

## 5. Instrumentation fixes — the actual ask this cycle

| # | Defect | Fix | Status |
|---|---|---|---|
| F1 | 32/38 route_log rows have `ts=null`, written outside `log_outcome` (which defaults `ts` and cannot emit null — `src/apex_router/route_log.py`). Worse: they form a probable-synthetic periodic cell that still flows into `route-advise` verdicts. | Route ALL writes through `log_outcome`; quarantine the existing null-ts rows; add a read-side provenance flag so null-ts rows can't silently enter rates. | **open** |
| F2 | Conformance log has zero organic rows; emitter is wired only on the resolve-static path (`route_resolve.py` → `log_resolve_conformance`). | Get real dispatch volume through resolve/pi/agent surfaces; quarantine or mark the synthetic S/O probe batch. | **open** |
| F3 | `route-check` readout missing from the CLI. | — | **RESOLVED upstream** @ 240e39d (verified working during this pass) |
| F4 | **Schema gap: neither log carries `context_size` or `session_id`.** LOG-SCHEMAS lists `context_size` as the headline join feature, but no field and no shared key with `telemetry.jsonl` exists — even a third join is currently impossible. | Add `context_size` + `session_id` to both schemas (emitters know them at dispatch time). | **open — blocks everything downstream** |
| F5 | Collection floor not met: need ≥3 task-type cells at n≥30 with joined features and known provenance before Phase 0 re-run. | Measure-first: keep logs flowing fail-open after F1/F4; re-run `join_table.py` weekly. | **open** |

## 6. Cross-validation — pass 1 reconciliation

Independent adversarial review by a fresh **Opus-tier Claude reviewer** (cross-family
vs the K3 producer; given the raw artifacts, not a summary; prompted to refute).
10 findings, triaged at ground truth:

- **CONFIRMED (7):** §3 basis was a moving live log making "verified" unfalsifiable
  (F-J); "100% k3" no longer true (F-A — codex client now 88.5% k3); >250k share and
  percentiles quoted with mixed scopes vs the artifact (F-B/F-C); headline spend and
  counterfactual mismatched the shipped JSON (F-D/E/F); **the report's one
  floor-clearing cell is drawn entirely from the null-ts rows it discredits** (F-G —
  on ground-truth inspection the escalations are exactly periodic, probable synthetic);
  "2.9× context-reduction win" conflated a price ratio with demonstrated savings (F-I).
- **REJECTED (1):** the claim that "`log_outcome` can never emit null ts" is
  unsupported (F-H) — it is directly verifiable in `src/apex_router/route_log.py`
  (`ts=None → time.time()`); claim retained with citation, writer-identity claim
  softened to "unknown".
- **Minor (2):** honesty invariant currently vacuous (no agent-surface rows — now
  stated); token figures restated on the frozen basis.

**Fixes applied (authored here, not by the reviewer):** all §3 numbers recomputed on
one frozen snapshot with explicit scopes; the `extract` cell quarantined with the
periodicity evidence; F1 escalated from "era-slicing defect" to "synthetic data
reaching verdicts"; F3 left as resolved (verified live during the update). No pass 2
spawned: every fix is a number regenerated by the frozen-snapshot script above or a
text change directly checkable against the cited raw rows — terminating at pass 1 per
the review budget for small, independently verifiable fixes.

---

## Appendix — artifacts & reproduction

All in this directory (`docs/phase0-outcome-router/`):

| File | What |
|---|---|
| `analyze.py` | telemetry analysis (request rows only; heartbeats excluded) |
| `telemetry_analysis.json` | sub-agent telemetry run (superseded in §3 by the frozen-snapshot figures, which use explicit scopes) |
| `join_table.py` | Phase-0 join pipeline (route_log ⋈ conformance) |
| `labeled_table.jsonl` | the labeled table (**currently 0 rows** — that is the finding) |
| `join_report.md` | join stats + data-quality defect list |

Reproduce:
```bash
python3 analyze.py        # reads ~/.apex/telemetry.jsonl (live — expect drift vs frozen snapshot)
python3 join_table.py     # reads ~/.apex-router/{route_log,conformance}.jsonl
apex-router route-advise  # per-task-type escalation verdicts (Wilson + floor + cost)
apex-router route-check   # conformance drift readout
```
