# P6 world model — implementation contract (E1–E4)

**Status:** contract for the fan-out, 2026-10-07. The experiment itself is registered in
`RESEARCH-FIT-BACKLOG.md` P6 (gate G1, kill criterion); its place in the map is
`RESEARCH-MAP.md` §3.3; the planner it serves is `PLAN-workflow-graph-optimizer.md`. This file
fixes the shared pieces so E1 (data), E2 (baselines + Zeno/Markov layer) and E3 (JEPA in MLX)
can be built in parallel and meet at E4 (evaluation). Nothing here changes a shipped readout:
`zeno report`, `markov.py`, `labels.py` stay as they are.

## 0. Package and privacy

- Package `src/apex_router/worldmodel/` (new): `steps.py` (E1), `actions.py` (E1 classifier),
  `baselines.py` + `chains.py` + `progress.py` (E2), `jepa.py` + `train.py` (E3), `evaluate.py`
  + `protocol.py` (E4), `cli.py` wired as `apex-router worldmodel <cmd>`.
- Data home `~/.apex-router/worldmodel/` (honour `APEX_ROUTER_HOME`), files 0600, dir 0700.
- **Never store text.** A step record holds classes, counts, buckets, flags, ids and hashes.
  Embeddings (if used) are stored as float16 arrays keyed by step id, never the text they came
  from. Same rule as `labels.py` and the widget.
- Read-only over transcripts; every reader fails open on a malformed line.

## 1. Step record (E1 output, consumed by E2/E3/E4)

One JSON line per tool call (= one step), `steps.jsonl`:

```
{
 "sid":   "<session id>",         # pi / Claude session id (subagent: the PARENT session id)
 "agent": "<subagent id|null>",   # null for the main thread
 "src":   "pi|claude",
 "task":  "<task id>",            # sid + ordinal of the user turn (a task = one user turn that ran ≥ 1 tool)
 "i":     int,                    # step index within the (task, agent) STREAM (0-based); dt/phase are per stream too
 "ts":    float,                  # epoch seconds
 "act":   "<action class>",       # §2 vocabulary
 "tool":  "<tool name>",          # pi/Claude tool name (bash, read, edit, Agent, …) — no args
 "err":   0|1,                    # tool returned an error
 "tests": {"ran": 0|1, "failed": int|null, "passed": int|null},   # parsed from test-runner output, else ran=0
 "out_b": int,                    # size bucket of the tool output: 0:<1k 1:<10k 2:<100k 3:≥100k chars
 "in_b":  int,                    # size bucket of the tool input (same buckets)
 "dt":    float|null,             # seconds since the previous step in the task
 "phase": "explore|edit|verify|deliver|other",  # §2
 "model": "<model id|null>",      # from telemetry join when available
 "spawn": 0|1                     # this step spawned a subagent (Agent/Task/Workflow)
}
```

The split boundaries (timestamps) are frozen in `manifest.json` on the first build and reused
on rebuilds (`--resplit` recomputes them), so the test split is scored once across rebuilds.
Subagents spawned by a `Workflow` call are joined to their task through the run id
(`wf_…`, the subagent directory name) found in the call's result — never by time alone.

`tasks.jsonl`: one line per task — `{task, sid, src, t0, t1, steps, outcome, outcome_src,
split}` where `outcome ∈ success|partial|fail|unknown` comes from `~/.apex-router/labels/labels.jsonl`
(`outcome_src = gold|weak|none`) and `split ∈ train|val|test` by **session start time** (first
70% of sessions train, next 10% val, last 20% test — never a random shuffle; subagent steps follow
their parent session's split).

## 2. Action classes and phases (`actions.py`)

Classes: `search read edit write run test build vcs remote plan delegate ask other`.
Bash commands are classified by scanning **segments** (split on `;`, `&&`, `||`, `|`, newlines)
and taking the highest-priority class found: `test` > `build` > `vcs` > `remote` > `run`;
`test` must catch pytest/jest/go test/cargo test/rspec through wrappers (`.venv/bin/pytest`,
`python -m pytest`, `uv run pytest`, `npm test`, `make test`). Leading `cd`, `timeout`, `env`,
`VAR=` prefixes are skipped. Non-bash tools map by tool name (Read/Glob/Grep → read/search,
Edit/Write/NotebookEdit → edit/write, Agent/Task/Workflow → delegate, AskUserQuestion → ask).

Rule details (fixed 2026-10-08 after a blind re-label): inspection commands are **read**
(`cat sed head tail wc nl less awk cut ls stat file realpath readlink shasum df plutil sysctl
openssl ps pgrep lsof type which command -v`); **search** is content or name lookup (`grep rg
git grep find fd mdfind`, WebSearch); interpreters fed a query or script are **run** (`sqlite3
log python node osascript`, model CLIs `codex exec`, `pi -p`); process or service control is
**run** (`kill pkill launchctl tmux open`); no-op segments add no class (`echo printf sleep date
pwd test [ set export unset umask`) and a command made only of them is **other**; shell file
mutation is **write** (`cat >`/`>>`, `tee`, `mkdir cp mv rm chmod rsync`), in-place rewrites are
**edit** (`sed -i`, `perl -pi`, `patch`); installs, compiles and lint are **build**; `claude plugin
test` and `python -m unittest` are **test**; `gh` is **vcs**; `curl dig kubectl doctl` and
WebFetch / browser MCP tools are **remote**; Skill/TodoWrite → plan; StructuredOutput /
SubagentHandback → ask; unknown tools → other.

Validation set: `tests/fixtures/worldmodel/actions_validation.jsonl` — 306 command shapes with a
class each (arguments and paths replaced by placeholders; never a literal path, token or
hostname), assembled by the implementer from the local transcripts. **Signed off 2026-10-08
(owner-delegated):** an independent blind re-label by the §2 rules agreed on 295/306 (96.4%);
the 11 disagreements were rule ambiguities, resolved in the paragraph above, not fixture
errors; the classifier agrees 306/306. Thin classes (remote 9, build 6, edit 4, other 3, ask 2,
delegate 1, plan 1) are the next thing to grow. Target agreement ≥ 95%.

Phase is **causal** — computed from steps ≤ t only (amended 2026-10-07 after the E3 review found
the tail rule leaked task length): `explore` (search/read before the first edit of the task so
far), `edit` (this step is edit/write), `verify` (test/build/run after an edit so far), `deliver`
(this step is vcs/ask), else `other`. Appending later steps never changes an earlier phase.

Tasks also carry `task_type` (one of `classify.py`'s classes, null if unclassifiable) and
`workflow` (`W2` if the task spawned a subagent, else `W0`; a recorded workflow wins).

## 3. Evaluation protocol (`protocol.py`) — shared by every track

- Time split by session (§1); tuning on train/val only; test scored once per model.
- Metrics: next-action cross-entropy (nats/step) and perplexity; outcome Brier on tasks
  (P(success) vs label); paired differences with a **session-cluster bootstrap** (resample
  sessions, 1,000 draws) giving a 95% CI; calibration error (10-bin ECE).
- Baseline set for G1: prior (unigram), Markov order 1 and 2 (Dirichlet α = 0.5 smoothing,
  hierarchical: per-task-type chain shrunk to the pooled chain), logistic regression on hand
  features (numpy/scipy only — no sklearn).
- Every number printed carries its n and CI; a metric on `outcome_src = weak` labels is marked
  **provisional** — G1 criteria 2 and 5 are scored on gold only.

## 4. Markov / Zeno layer (E2 `chains.py`, `progress.py`)

- `chains.py`: order-k transition counts with Dirichlet smoothing; BIC order test (1 vs 2 vs 3);
  absorbing chain over classes with SUCCESS / FAIL / ESCALATE / ABANDON absorbing states;
  fundamental matrix N = (I−Q)⁻¹, expected steps N·1, P(absorb) = N·R, spectral radius ρ(Q) on a
  sliding window; the 2-state burst chain stays in `markov.py` (import it, don't copy it).
- `progress.py`: progress signal v_t (proxy today: 1 − failing/initial-failing from `tests`, or
  1 − open-error share; later the value head); geometric-decay fit Δ_{t+1} ≈ r·Δ_t on a sliding
  window of ≥ 4 steps; limit **v_∞ = v_t + Δ_t/(1−r)** (the corrected formula) with a bootstrap
  CI; flag `converging_short` when the CI's upper bound is below the success threshold; a
  step-count and wall-time cutoff baseline; G1 criterion 5 scoring (flags before the baseline
  would, FPR on successful tasks ≤ 10%).
- Insights carried from `zeno report` 1b on this machine (2026-10-07): r1 = 0.445, P(fail|fail)
  46% vs stationary 3%, chain beats i.i.d. held out (Brier 0.116 vs 0.162) but under-predicts long
  sessions — between-session / day-level regime is unmodelled. As implemented (E2): the
  absorbing chain's states are action classes only (no error state; the 2-state burst chain
  stays in `zeno report` 1b); error state enters through the logistic features (last error,
  cumulative error share), the JEPA inputs (`err` flag over the lookback) and the progress
  signal (open-error share); the evaluation reports metrics per day as well as pooled. An
  error-conditioned chain state is a candidate for attempt 2, to be pre-declared.

## 5. JEPA (E3 `jepa.py`, `train.py`) — MLX

- Inputs per step: action one-hot, phase, buckets, flags, dt bucket, task-so-far counts; optional
  frozen 768-d embedding of the request (nomic-embed via ollama, float16, cached).
- Encoder → latent z (d = 64 default); predictor conditioned on the next action class; losses:
  k-step latent prediction (k = 1..4, stop-gradient targets, **no EMA teacher**), SIGReg
  (isotropic-Gaussian regulariser), value head (P(success | z) on tasks with a label), next-state
  head for G1 criterion 1. H-JEPA split: `z = [z_percept, z_control]`; the planner / value head
  read `z_control` only.
- Collapse diagnostics at every checkpoint: effective rank of z on val, SIGReg statistic; a
  checkpoint outside the preset bounds is reported, not silently kept.
- Macro-steps: an optional run-length merge of consecutive same-class steps as a config flag,
  no global cap (a cap that merges the minimum pair is non-causal); a macro-step is finalised at
  run end, so macro mode is offline evaluation only. Both granularities are evaluated.
- Step 0 is scored from a start prior fitted on train first actions (every model alike).
- Training: fixed seed, config JSON beside the checkpoint, ≤ 12M params, runs on this M3 Max
  within minutes; `mlx` installed into `.venv` only after dependency vetting (OSV, license,
  release recency) — report the vetting.
- Linear probes on z for: next action, outcome, phase, failing-tests bucket.

## 6. E4 (`evaluate.py`) — G1 scorecard

`apex-router worldmodel evaluate` prints one table with G1's five criteria, each PASS / FAIL /
INCONCLUSIVE (no gold yet) with the numbers, CI and n, plus the baselines' own scores, per-day
breakdown and collapse diagnostics. The expected result on today's data is FAIL on at least
one criterion — the report must say which and why, not round up.
