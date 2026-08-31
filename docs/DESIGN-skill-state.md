# DESIGN — SKILL.state (arXiv:2608.26263) adaptation and evaluation record

Status: **shipped default-off** (2026-08-30). The harnesses are the durable product; the
measured verdict at apex's real horizons is *behavioral parity, token parity, robustness
parity* — so nothing is promoted to a default. Re-run the benches when the model, the
corpus, or the horizon changes.

## The paper

SKILL.state replaces the append-only conversational runtime with an explicit, mutable
execution state. Each step the model sees `(P, Σ_t, O_t)` — immutable procedural spec,
structured state, latest observation — and emits `(reasoning R_t, state patch ΔΣ_t, action
a_t)`; `R_t` is discarded after a validated state update (`Σ ⊕ ΔΣ`, null-deletion merge).
Claimed benefits: O(T) vs O(T²) cumulative tokens, distractor-noise immunity, and zero-step
recovery from silent environment drift (history runtimes anchored 5–8 turns). Open-weight
error taxonomy (§5.7): 68% premature state overwrite / 20% schema-type / 12% JSON syntax —
motivating patch-only updates, validation, and constrained decoding.

## What was adapted, and what was measured

### 1. Codegen state lane (local model) — `ornith/state_codegen.py`

The one-shot codegen lane (generate → run caller tests → escalate cold) becomes a bounded
self-repair loop: model emits **patch-only** ΔΣ (never the full state — the 68% overwrite
mode becomes structurally impossible), runtime validates against the lane schema (unknown
keys / wrong types / deleting `code` → rejected and counted under the paper's taxonomy
labels), merges server-side, re-runs tests; test output is the next observation. Verdict
doctrine unchanged (`ok` == tests pass == `gated`); usage summed across calls so retry cost
is booked honestly; escalation carries the structured state, not a transcript.

Dispatch wiring: `ORNITH_CODEGEN_STATE_LANE=on` (default off; injected stubs never bypassed).
Bench: `python -m apex_router.ornith.state_bench [--suite x.jsonl] [--fake]` — Wilson CIs,
escalation delta, tokens/pass, taxonomy, explicit POSITIVE/DEGRADATION verdict lines.
Suites: `benchmarks/codegen_hard.jsonl`, `benchmarks/codegen_probe.jsonl`.

**Measured (Ornith-1.5-35B-A3B, 17 tasks × 2 arms):** 100% first-attempt pass in BOTH arms
on all three suites — the repair loop never engaged; the state arm cost 1.9–2.9× tokens/pass
(JSON-patch contract overhead). JSON contract held 17/17 first attempts (no §5.7 failures on
this tier). **Verdict: default-off; pure overhead while first-attempt pass ≈ 100%.**
Break-even needs `first_attempt_failure_rate × rescue_rate` to cover ~2× per-job overhead.

### 2. (P, Σ, O) frontier driver — `proxy_engine/tuner/state_driver.py` + `driver_bench.py`

The behavioral gate's tool loop (append-only Anthropic Messages loop) got a state variant:
one user message per round = (original prompt, runtime-maintained Σ, latest results). The
runtime owns Σ (`ref → fragment` map) — the frontier model is never asked for structured
patches. `behavioral_driver` gained a `call_api` seam (production path unchanged) so the
bench swaps arms by name. Budget enforced between rounds; a forced tool-less answer round
fires when the cap is exhausted mid-retrieval.

**Measured (offline, scripted API, input from actual request bytes):** behavior parity all
tasks; token parity at the gate's 4-round horizon (state ≈ 102–113% of transcript — the
Σ-resend overhead dominates the discarded-narration savings at short T). Live opus run not
executed (no bearer in CI env); `--live` is ready.

### 3. GPT transferability + drift — `proxy_engine/tuner/codex_driver_bench.py`

Model-agnostic harness over `codex exec` (no Anthropic auth): RETRIEVE/ANSWER directive
protocol over the real json_crush/StubResolver probe; transcript vs (P, Σ, O) arms; tokens
from codex's own report. Plus the paper's experiment 3: silent mid-run fragment swap +
corrective alert — transcript keeps the stale value in history (anchor contest), Σ is
replaced.

**Measured (live GPT, 2026-08-30):** identical ref sequences, zero protocol errors,
character-identical *correct* answers — behavior transfer confirmed. Tokens parity
(~38k/arm; codex exec's ~4.7k/call fixed overhead dominates; prompt-size replay shows state
pulling ahead only as rounds grow). **Drift: both arms RECOVERED** — GPT did not anchor on
stale history given one explicit alert. The paper's robustness advantage does not transfer
to a frontier model at this horizon/salience (N=1/arm; harder untested variants: no-alert
drift, multi-drift, distractor noise).

### 3b. Claude-family transferability — `proxy_engine/tuner/claude_driver_bench.py`

The DESIGN's opus open item ("live opus run not executed — no bearer in CI") closed via the
`claude` CLI (no raw Anthropic bearer needed), same model-agnostic RETRIEVE/ANSWER harness as
the codex bench: `claude -p --bare --output-format json` (`--bare` pins a ~1.7k system prompt
so marginal growth is the signal; same no-prefix-cache cost caveat as codex). Offline test:
`tests/test_claude_driver_bench.py`.

**Measured (live claude sonnet + haiku, 2026-08-31):**

- **Behavior A/B — a probe-validity bug surfaced and was fixed.** The original probe encoded
  each deploy window as `deploy-window-<service-name>`, guessable from the visible service
  key: sonnet & haiku retrieved only **2 of 6** refs and pattern-guessed the rest — *correctly*
  by luck, so the offline (scripted-model) bench could never catch it. Fixed by making each
  window an **opaque 6-letter code** (`driver_bench.WINDOW_CODES`) recoverable *only* by
  retrieval. Re-run: the transcript arm still early-stops at 2/6 and now **HALLUCINATES**
  plausible-wrong codes for the 4 it never fetched (verifiably incorrect); the state arm
  retrieves all 6 and reports the real codes. So at this horizon the state arm's edge is
  **correctness under an early-stopping model**, not just token profile — a stronger result
  than the GPT/offline parity finding.
- **Drift.** On the un-guessable probe, sonnet neither anchors nor recovers — it **ABSTAINS**
  (answers early after the alert, ~1 ref retrieved, "value not present in document" for
  svc-alpha). Distinct from GPT (both arms recovered). N small; protocol brittleness (model
  emits prose instead of a directive; two consecutive → arm aborts) adds noise and is itself
  a robustness note on the CLI-directive harness.
- **Injection-refusal confound (important).** sonnet treats the plain corrective alert as a
  **prompt-injection attempt** and refuses it in a prose note — a *safety* behavior that a
  naive verdict scored as anchoring. Added `build_drift(authoritative=True)` +
  `--authoritative`: SAME transport, wording only — it drops the injection cue (frames the
  change as a canonical retrieval update), which *reduces but does not eliminate* the confound
  (still confounded by wording/directiveness). The paper's cooperative-alert assumption does
  not hold for Claude by default.
- **Probe integrity (found in cross-validate).** `claude -p --bare` still exposes Read/Bash,
  so a model run inside this repo could read the opaque codes from source instead of
  retrieving. `claude_driver_bench` now passes `--disallowedTools` for the file/exec tools and
  runs from a neutral cwd — retrieval is the only path to the codes.
- **Two verdict-scoring bugs found live and fixed** (`codex_driver_bench._drift_verdict` /
  `render_drift`), each with a pinning test: (1) whole-answer substring match scored a
  refuse-and-explain answer as MIXED — now **scoped to the service's own answer line**
  (`identity=svc-alpha host`), with a `scoped` low-confidence flag on fallback; (2) a model
  abbreviating `deploy-window-qxlmtv`→`qxlmtv` scored as *neither* (silent false-negative) —
  marks are now **distinct bare opaque codes** (no containment); (3) a NEITHER answer
  (abstain/not-found/unfinished) was mislabeled MIXED by a render fall-through — verdict is
  now a proper 4-way (RECOVERED/ANCHORED/MIXED/NEITHER).

### 4. Structured handoffs and chain contracts

- `handoff_state.py` + `hooks/cache-handoff-nudge.sh` — the expensive-session handoff is a
  6-field structured state block (goal/constraints/decisions/files_touched/open_issues/
  next_action) instead of prose; the session fills it before stopping (the hook stays
  LLM-free); `validate` CLI catches unfilled blocks; a drift-guard test pins the hook's
  embedded template to the module.
- `integrations/pi/learn.ts` — the /learn chain's VALIDATE slot emits a JSON verdict Σ
  (authoritative/weak/sections/focus_questions); EXPLAIN is prompted (P, Σ) with sources
  restated. Parse is contract-enforced (non-empty `authoritative` string array, known keys,
  typed values) and fails open to the legacy prompt.

## Decisions and open items

- **Everything default-off.** Three settings, parity everywhere; adoption is earned, not
  assumed — the same gate any routing change passes.
- **Cross-validated with GPT (codex-cli)** on the initial diff: 8 findings, all triaged —
  7 real (state-loop fragment loss, parity-by-count measurement, verdict starvation guard,
  mid-loop budget, cap-exhaustion answer, lax handoff validation, bytes-vs-tokens
  overclaim), each fixed by the author with a pinning test. Reviewer-proposed fixes were
  not adopted (per the cross-validate discipline).
- **Known bugs found by running, fixed before commit:** probe content tripping json_crush's
  Δ7 lexeme guard (zero elisions); identical leaves collapsing to one content-addressed ref;
  double-sending fragments in both Σ and observation.
- **Claude-family run (2026-08-31)** closed the opus open item via `claude_driver_bench.py`
  and surfaced a guessable-probe bug (now opaque codes), an injection-refusal confound (now
  the `--authoritative` drift variant), and three verdict-scoring bugs — all fixed with
  pinning tests. Net Claude findings: behavior transfers but early-stopping makes the state
  arm *more correct* on the un-guessable probe; Claude ABSTAINS under drift and REFUSES the
  plain alert as injection (unlike GPT's recovery).
- **Re-run triggers:** local model change or first-attempt failure rate rises (state lane);
  longer-horizon workloads appear (drivers); a subtler drift test is wanted (codex/claude
  bench `--drift` / `--authoritative` variants).
- **Open:** `/learn` contract-hold instrumentation (verdict-parse rate per run) + one live
  `/learn`; handoff hook's first natural fire, then `handoff_state validate` on the output;
  Claude drift under `--authoritative` at larger N (protocol brittleness inflates NEITHER);
  no-alert / multi-drift / distractor variants still untested on any model.
