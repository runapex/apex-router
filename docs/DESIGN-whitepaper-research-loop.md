# DESIGN — whitepaper research loop (pull → hypothesize → bounded experiment → gated deploy)

Status: **design BLOCKED pending prerequisites** (was "proposed"). Reasoned + refuted by
`it-entra-gpt-6-astra`, then independently cross-validated by `it-entra-gpt-5.6-sol` +
Claude — the cross-validation **REJECTED the original first slice** and found the premise
over-claimed. Corrected below. Companion to `DESIGN-rag-qlora-loop.md` /
`DESIGN-learn-chain.md`.

> ## Cross-validation outcome (verified against the live tree — do not skip)
>
> Three claims in the first draft were checked at ground truth and are **FALSE as relied
> upon**. They are prerequisites, not details:
>
> 1. **`rag_eval` cannot evaluate the proposed change.** `measure_improvement` compares
>    inject-vs-no-inject at a **single fixed `k`** (`rag_eval.py:131-135`) — it cannot
>    compare production `k=3` against candidate `k=1`. The paired deltas `gate.run_gate`
>    needs come from `bench.cell_evidence_from_rows` (`bench.py:226`), which the slice omitted.
> 2. **The exemplar-count knob has NO production consumer.** `k` is read only inside
>    `rag_eval` itself (`rag_eval.py:87`); repo-wide search finds no live retrieval path or
>    config pointer using it. So "shadow → switch one pointer → restart" has **nothing to
>    point at**. This was the weakest link, and it invalidates the chosen first slice.
> 3. **The "existing safe loop" is scaffolding, not operational.** `rag_nightly` harvests
>    `[]` and only prints "would invoke" (`rag_nightly.py:197-208`); harvested records store
>    `lineage` but retrieval needs `source_job_id` so they retrieve as `[]`
>    (`exemplars.py:80`); the QLoRA pointer is a non-atomic JSON write the live resolver
>    never reads (`qlora_serve.py:65`, `local_tier.py:147`). **"Extend the working loop" is
>    false — the loop does not yet work.**
>
> Sol also demonstrated by probe that `gate.run_gate` **trusts supplied deltas/windows**
> (it promoted invented arrays at p=0.0005) and `rag_eval` **does not freeze evaluator
> state** (an empty corpus scored `delta=1.0` from a stateful callback). So neither is a
> safety boundary on its own — they are correct *statistics over honest inputs*, not
> *verifiers of input honesty*. That verification is unbuilt.
>
> **Net:** the loop is a valid long-term direction, but it cannot be built on today's
> components without first making the base loop real and adding an evidence verifier. The
> corrected first slice is below.

## The ask, and why the literal version is refused

The original request: *"apex-router periodically pulls whitepapers, evaluates them against
existing metrics, implements the ideas, and restarts/updates its tools in the background."*

apex-router already self-improves its **model weights** safely (L1 RAG + L2 QLoRA), because a
weight-adapter is a **bounded** change behind a stable serving interface, adopted only when it
beats production on a paired, FDR-corrected gate (`gate.run_gate`), with a shadow tag and a
one-pointer rollback. The dangerous word in the ask is **"implements"**: letting the loop write
and deploy **arbitrary tool code** autonomously breaks that entire safety model —

> An adapter changes behavior behind a comparatively stable interface. Arbitrary Python can
> replace the judge, alter imports, erase evidence, steal credentials, or disable rollback.
> Passing paired tests establishes measured performance, not the absence of these capabilities.

So the accepted design is: **extend the experiment loop, not the router's authority to rewrite
itself.** Paper discovery, hypothesis generation, and *bounded* experiments become autonomous;
novel executable code stays human-gated. `VERDICT: BUILD-NARROW`.

## Core decision: a paper is an *untrusted experiment proposal*, never an instruction

A pulled paper is evidence, not a command. It may only ever become a **registered, falsifiable
experiment** whose primary metric already exists in the ledger, scored by the existing gate. If
a claim can't be turned into such an experiment on *this* box, it is REJECTED — not implemented.

> Parked survivors of manual paper triage live in `docs/RESEARCH-FIT-BACKLOG.md` — telemetry-gated
> candidate experiments (currently P2/P3/P4 of a KV/serving digest, astra-cross-validated), to be
> revisited as the ledger enriches. That file is a backlog of hypotheses, not a build list.

This mirrors the books guardrail in `rag_nightly.py` (books ground RAG, never train weights):
research-derived material is **explicitly non-training**, and its descendants stay non-training —
summarizing a paper does not launder it into first-party data.

## Components (new), and the existing machinery each reuses

| New component | Does | Reuses |
|---|---|---|
| `paper_scout.py` | scheduled pull from an **allowlisted** source set, download/compute budget, dedupe by content hash, store `{paper_id, version, hash, provenance, retrieved_at, extracted_claims}`. Parses without credentials, prod-write, or code-exec. | `rag_nightly.harvest` loader conventions + staging lifecycle; a **separate typed research-proposal queue** (not `Candidate` — books-vs-other isn't the same axis) |
| `research_cycle.py` | turns one claim into a registered experiment: `{paper_hash, prod_baseline_hash, exact config/artifact diff, affected task population, mechanism, primary metric + direction, min useful effect, guardrail margins, eval splits/windows, resource ceiling, permitted capabilities, rollback target}`. **Missing any field ⇒ REJECT.** | `rag_eval.run_condition` snapshot + lineage isolation for the L1 screen |
| `experiment_registry` | reserves fresh confirmation data, records failed attempts, forbids confirmation-driven retuning, budgets tests across cycles | `gate.run_gate` (paired, out-of-sample, FDR across the registered family) |
| `deploy_supervisor.py` | shadow → bounded canary → freeze-admit → drain/fence in-flight → switch **one** release-manifest pointer → restart affected labels → verify loaded release hash + behavior + telemetry → rollback on any failure | `watch.py` launchd `bootout`/`bootstrap` restart plumbing |
| `event_collector` | owns request IDs, denominators, timestamps, release identity, independently-verified outcomes; candidate workers **cannot** write canonical ledgers/approved-feedback/scorer/deploy-metadata | tightens today's fail-open `route_log.py` / `offload_telemetry.py` writers |

## Which metric each candidate family is scored on (no new gate)

| Candidate family | Existing evidence | Required qualification |
|---|---|---|
| retrieval / exemplar selection / context allocation | `rag_eval` held-out escalation; L1 reward + crowding | freeze judge + corpus; compare vs **production** retrieval, not a weak baseline |
| routing / escalation policy | paired task-quality / SC2 reward; route outcomes; offload cost/latency | `route-advise` finds *opportunities only*; its cheap-selected population is **not** evidence about heavy-started tasks |
| chain-stage selection | SC2 reward, `chain-bench` CIs, cost-per-delta | need whole-candidate-vs-production rows; existing `prior-stage` deltas are not that comparison |
| quantization / serving | frozen 200-prompt probe, paired reward, offload latency/tokens | keep forgetting rails; **plus new local instrumentation**: peak memory, swap pressure, cold starts, failures |
| bandit / exploration | independently-scored paired outcomes on covered tasks | historical outcomes can't establish unobserved arms; live exploration needs separately-approved exposure limits |

"Beats production" keeps its current meaning: **superiority**, not "quality didn't significantly
decline." Cost-only/noninferiority adoption requires a separately reviewed, powered contract.
Scorer definitions and gate thresholds stay **outside** the candidate's change surface.

## Two deployment tracks (the safety fork)

- **Autonomous track — declarative only.** The candidate instantiates a **human-approved policy
  template** with bounded values and *no executable expressions* (e.g. "exemplar_count ∈ {1,2,3}
  for task_class X"). This is the only thing the loop deploys on its own, and only after the gate.
- **Review-gated track — novel code.** Generated in an isolated workspace, tests + shadow eval
  run, then **human approval before any production exposure.** Initial code extensions restricted
  to stateless, capability-limited workers. No autonomous migrations, shared-state rewrites, or
  irreversible external actions.

Every candidate is an **immutable release** (code/policy + pinned runtime/deps + model/corpus
refs + interface versions); the live checkout and shared env are **never edited in place**. A gate
certificate binds the exact candidate hash to the exact production hash tested; a changed baseline
voids the certificate.

## The refutation this design must keep answering (astra, Part B)

1. **Action space** — arbitrary code ≠ bounded adapter; refuse unrestricted autonomous code deploy.
2. **Goodhart** — proposer can optimize the metric (suppress escalation, cherry-pick easy traffic);
   keep thresholds/labels/tests/judges/telemetry-semantics off the change surface.
3. **Repeated research = repeated holdout search** — FDR-in-a-batch isn't a license to propose
   forever after seeing outcomes; fresh confirmation windows + a reviewed cross-cycle test policy.
4. **Transfer** — a paper may solve a different machine's problem; on a 38 GB single box, measure
   memory/concurrency/cold-start locally, serialize heavy incumbent/candidate runs. No feasible
   local test ⇒ no adoption.
5. **Ledger corruption** — a restored pointer can't unlearn poisoned feedback; **prevention before
   detection** — deny candidate writes, quarantine untrusted evidence, verify before admission.
6. **Restart ≠ deploy success** — need generation fencing, behavioral checks, rehearsed rollback
   with the old runtime still live; supervisor/rollback code lives outside the candidate.
7. **Permanent human gates** — novel code; deps/native kernels; new permissions/network; live
   exploration; ledger/schema migrations; scorer/gate changes; provenance/training-policy changes;
   supervisor/restart changes; irreversible actions. The router searches a bounded vocabulary; it
   never enlarges its own authority.

## First slice (CORRECTED after cross-validation — implement this, nothing more)

The original slice (autonomously tune RAG exemplar-count, then shadow/switch/restart) is
**withdrawn**: the knob has no production consumer, `rag_eval` can't evaluate it, and the
slice self-contradicted (it said "switch one pointer / restart" *and* "deployment is the
second slice"). Replace it with a **read-only, deploy-nothing** slice that is buildable today:

> A paper nominates one already-measured knob (a candidate family in the table above whose
> metric ALREADY has paired rows — e.g. a routing/escalation change scored on existing
> `route_log`/SC2 reward). `research_cycle.py` emits the **registered experiment record**
> (all required fields or REJECT). Build the evidence via `bench.cell_evidence_from_rows`
> (the real pairing bridge), run `gate.run_gate`, and **emit a read-only experiment report.**
> **No deploy, no config flip, no restart, no ledger write, no generated code.** The report
> is the deliverable; a human reads it.

Hard prerequisites before *any* autonomous deploy slice (all currently unmet):
1. **Make the base loop real** — real harvest loaders + the `source_job_id`/retrieval schema
   fix, so the RAG path this design rides on actually retrieves and trains (its own work,
   `DESIGN-rag-qlora-loop.md`).
2. **Build the evidence verifier** — the piece that checks pairing, split ownership,
   candidate/baseline hashes, min-useful-effect, guardrails, and **holdout freshness across
   cycles** BEFORE `gate.run_gate` sees the deltas. `gate` trusts its inputs; something must
   make the inputs trustworthy. This is the single most important fix (sol) and subsumes
   Open Question #1.
3. **A deny-by-default, per-knob capability manifest** — enumerate each autonomously-writable
   knob and *prove its complete runtime effects are bounded*. "Declarative / no executable
   expressions" bounds syntax, not semantic impact (a bounded number can still disable a
   safety path). No autonomous deploy of a knob not on this manifest.
4. **A real deployment supervisor** — `watch.py` does `bootout`/`bootstrap` and ignores
   return codes with no drain/fence/readiness/hash/behavior check/rollback (`watch.py:204`,
   `:270`). Transactional deploy + rollback is unbuilt and must exist before slice 2.

Slice 1 delivers `paper_scout.py` (one operator-owned allowlisted source), `research_cycle.py`
(registered experiment + REJECT-on-missing-field), the `bench`→`gate` evidence path, and a
read-only report. Everything that deploys, flips a pointer, or restarts is later and gated on
prerequisites 1-4.

## Open questions — RESOLVED by cross-validation (all three were BLOCKERS, not open)

Sol's pass established all three are prerequisites, not deferrable questions:

1. **Cross-cycle error control + immutable single-use holdouts must exist before any result
   authorizes deployment.** `rag_eval` explicitly leaves single-use to "caller discipline"
   (`rag_eval.py:106`) and `gate`'s FDR covers only the cells in one invocation — nothing
   controls error across an unbounded research stream. A defined online-testing policy is
   mandatory; a per-quarter cap is optional on top. (= prerequisite #2 above.)
2. **A deny-by-default, per-knob semantic allowlist is mandatory** before any autonomous
   config change — bounded syntax ≠ bounded effect. (= prerequisite #3 above.)
3. **The source allowlist must be operator-owned, outside the loop's write authority**,
   with redirect restrictions, content limits, and parser isolation, before unattended
   ingestion. (Folded into `paper_scout` in slice 1.)

## Provenance

- **Design + Part-B refutation:** `it-entra-gpt-6-astra` (independent vendor). Transcript:
  `/tmp/astra_apex_selfimprove.txt`.
- **Cross-validation (author != reviewer):** `it-entra-gpt-5.6-sol` (different model than the
  GPT author) + Claude, prompted to refute. Sol ran live adversarial probes and returned
  **VERDICT: REJECT** on the original first slice; Claude verified each load-bearing claim
  against the code (`rag_eval.py:87,131-135`, `rag_nightly.py:197-208`, `exemplars.py:80`,
  `bench.py:226`, `watch.py:204`) — all confirmed. Transcript: `/tmp/sol_xval_design.txt`.
- **Reconciliation:** findings CONFIRMED at ground truth; doc corrected (status -> BLOCKED,
  first slice -> read-only, open questions -> prerequisites). Per cross-validate discipline,
  sol's *findings* were taken but its *fix* was derived here from the code, not ported. Kimi K3
  (their usual third vendor) was **not reachable** here — noted so the independence chain is honest.
- **Not yet done:** a second pass on THIS corrected version. Marked BLOCKED, not accepted.

<!-- superseded provenance retained below for lineage -->
Design + adversarial refutation produced by `it-entra-gpt-6-astra` (independent vendor) from a
ground-truth digest of the live tree; all cited files/lines verified present
(`rag_nightly.py:56`, `gate.py:286`, `rag_eval.py`, `route_advise.py`, `watch.py:204`, …). Full
reasoning transcript: `/tmp/astra_apex_selfimprove.txt`. Next: independent cross-validation pass
before this is marked accepted.
