# Changelog

Notable changes to apex-router. Dates are the day the change landed on `main`.
Version numbers follow `pyproject.toml`; between tags, the heading is the version the
next tag will carry.

## 0.4.2 — unreleased

### Added
- `apex-router labels judge-bench [--models] [--contexts clip,export] [--prompts v1,v2] [--out]`
  scores outcome-judge variants (model × context × prompt) on every gold task. It reports vote
  accuracy with a Wilson CI, 4-class accuracy, a confusion matrix, abstain / unknown / error
  rates, latency, tokens and confidence calibration. Calls are deterministic and cached per
  (variant, task, prompt hash). Nothing written holds transcript text. The judge default is now
  the bench winner: `ornith:35b` on the export-review context with the v2 decision-rule prompt,
  voting at confidence ≥ 0.9. Each setting can be overridden with `APEX_LABEL_JUDGE`,
  `APEX_LABEL_JUDGE_CONTEXT`, `APEX_LABEL_JUDGE_PROMPT` or `APEX_LABEL_JUDGE_MIN_CONF`.
  Judge accuracy on gold went from 0.46 to 0.90 [0.81, 0.95] at the old 0.6 threshold. Gold is
  model-made from the same context and the result is in-sample
  (`docs/research/2026-10-08-label-judge-bench.md`). `labels build --rejudge` re-judges every
  task whose vote came from another judge config. After a rejudge, coverage went from 20% to
  67% and weak-emitted labels from 19 to 273.
- `apex-router worldmodel snapshot [--home] [--dry-run] [--json]` mirrors pi, Claude Code
  (main sessions + `subagents/**`) and Codex transcripts into `~/.apex-router/transcripts/`
  (dir 0700, files 0600; copy only when missing/smaller/older; never deletes or truncates; tmp
  + rename; skips files > 512 MB; fail-open per file). `labels.transcripts()` and the P6
  `worldmodel build` walkers now read the live dirs UNION the mirror, one file per session
  (larger wins), so retention pruning no longer shrinks the dataset (main-session steps had
  fallen from ~13k to ~7k). `apex-router watch install-snapshot` / `uninstall-snapshot` adds
  the opt-in daily job `com.apex-router.snapshot` (03:17, RunAtLoad false; systemd timer on
  Linux). The mirror holds the owner's own prompt text locally; the proxy and widget never
  read it, and the P6 builder still stores no text.
- `zeno report` section **1c**, a regime model (`regime.py`, stdlib). It fits a 2-state hidden
  Markov model (normal/degraded) by Baum–Welch to the time-ordered proxy call stream, in two
  forms: plain, and with a burst chain per state. Viterbi decoding lists the degraded days and
  hours, and the stationary degraded share is reported. An EM-free day mixture serves as the
  baseline. Held-out scoring uses the 1b protocol (log-lik and Brier of P(clean), paired
  session-bootstrap CIs against iid and the chain); the filtered variant is labelled as using
  more information. Verdicts: `insufficient` (< 200 pairs), `bursts only`, `regimes found`.
  Sections 1 and 1b are unchanged. On live data, the one slow regime it finds (08-24 → 10-02)
  is the v8 transport-retry deploy, not an upstream outage. After v8 the stream is bursts only
  (`docs/research/2026-10-08-l1-regime-and-retry-ab.md`).
- Retry-action A/B instrument. Default behaviour is unchanged. `APEX_RETRY_POLICY` takes
  `immediate` (default, today's backoff), `wait` (sleep at least `APEX_RETRY_WAIT_MS`, default
  1000, capped at 2000 ms) or `switch` (logs the arm only; there is one upstream per wire).
  `APEX_RETRY_AB=1` draws the arm once per request, at its first retry, over
  `APEX_RETRY_AB_ARMS` (default `immediate,wait`). Telemetry **v11** adds `retry_policy`,
  `retry_arm` and `retry_propensity` on rows that retried. The doctor accepts v11.
  `apex-router retry-ab report` gives per-arm P(retry recovers) with Wilson CIs and the arm −
  immediate difference with a Newcombe CI. It reports `INCONCLUSIVE` below 30 randomised rows
  per arm, and shows planning numbers (retries/day, days to the floor, minimum detectable
  difference).
- `apex-router labels export-review --k N --out FILE` and `labels import-gold FILE --by NAME`:
  a non-interactive path for the gold set beside `labels review`. `export-review` writes the
  same stratified sample `review` shows to a 0600 file, one JSON line per task (request, last
  assistant message and next user message, clipped to 1500/2000/800 chars; tool summary; weak
  votes, judge vote and rule signals). It holds transcript text for the reviewer only and stores
  nothing under `~/.apex-router/labels/`; delete it after the import. `import-gold` reads
  `{id, outcome, reason?}` lines, rejects the whole file on an unknown task id, an outcome
  outside success|partial|fail|unknown, a duplicate, or a reason over 120 characters, appends
  to `gold.jsonl` with `by=NAME`, and relabels. The no-transcript-quotes rule for `reason` is
  enforced by the length cap only. `labels report` now splits gold by author (`user` vs model)
  and shows weak-emitted vs gold-derived labels. Gold made by a model (for example
  `by=model:claude-opus-5-5`) is marked as such and is not the owner's judgement; `review`
  writes `by=user`. `export-review --ids A,B` exports exactly those tasks, gold or not, and
  includes the gold row currently in force for each. Gold is append-only. To correct a label,
  import a line with `supersedes=<ts of the row in force>`; the newest row per id wins in
  `relabel`, `report` and the sampler. A correction without `supersedes`, or with a stale one,
  is rejected.
- `apex-router worldmodel train [--synthetic N] [--config JSON] [--epochs E]` and
  `worldmodel probe <run_id>` (P6 track E3): a small action-conditioned JEPA over agent tool
  steps, in MLX. A causal transformer encodes each task's steps (classes, phases, size and time
  buckets, error and test flags, task-so-far counts; never text) into a 64-d latent split into a
  48-d perceptual and a 16-d control part. Losses: latent prediction 1–4 steps ahead conditioned
  on the next action (stop-gradient targets, no EMA teacher), SIGReg (Epps–Pulley statistic over
  random projections; a VICReg-style term as an ablation flag), next-action cross-entropy, and a
  value head P(success | control latent) on labelled tasks. ~1.75M parameters by default (ceiling
  12M). Every epoch logs collapse diagnostics (effective rank, SIGReg statistic, mean cosine)
  against preset bounds; the best checkpoint gets numpy linear probes (next action, outcome,
  phase, failing-tests bucket; phase and bucket are inputs, so those two are near-tautological
  and shown beside a raw-feature reference). `WorldModel.load(run).predict(steps)` returns the
  latents, next-action probabilities and P(success) for E2/E4; it skips malformed step records.
  Windows overlap (stride L/2) and each step is scored from the window where it has at least
  L/2 steps of context: on synthetic data the next-action CE on steps right after a former hard
  cut went from 1.765 to 1.634 nats (n = 151). Step 0 is scored from a start prior fitted on
  train first actions. The SIGReg weight is picked by `--sweep-w-reg` (1, 3 or 10; a setting
  outside the collapse bounds is rejected): 1 won on synthetic data. `--device cpu` gives
  bit-exact reruns for a seed; the GPU default does not (val CE differs by up to ~0.01).
  Optional macro-step merge (plain run-length, offline evaluation only, since a macro-step
  closes one step late) and a cached, fail-open nomic-embed request embedding (off by
  default). Runs go to
  `~/.apex-router/worldmodel/runs/<run_id>/` (0700/0600). No mlx import outside the training and
  inference functions, so nothing else in apex-router needs it.
- `apex-router worldmodel evaluate [--run ID … | --train [--seeds K] [--sweep]] [--synthetic N]
  [--view streams|task] [--json]` (P6 track E4): the G1 scorecard. Scores the JEPA against E2's
  baselines (prior, Markov 1/2, logistic) on the test split in one pass per model, with step 0 of
  every sequence scored from one train start prior and every choice (best baseline, w_reg) made
  on val. Prints one line per G1 criterion (PASS / FAIL / INCONCLUSIVE, numbers, n,
  session-bootstrap CI, and the rule), the overall verdict (G1 passes only if all five pass; with
  several seeds a criterion passes only if it passes on every seed), what blocks the
  INCONCLUSIVE ones, linear probes on test z, a per-day breakdown, the seed spread and a CPU
  replay run. Criteria 2 and 5 count gold labels only (weak labels print a provisional line).
  Sequences are (task, agent) streams by default, since a step's `i` counts within its stream
  (`--view task` keeps the interleaved order). The scorecard is saved to
  `~/.apex-router/worldmodel/eval/<ts>-<run>.json` (0600) with the git sha (and whether `src/`
  was dirty), the hashes of the manifest, `steps.jsonl` and `tasks.jsonl`, the manifest's split
  bounds, run ids and seeds. Each scoring of the real test split also adds a line to the
  append-only `eval/ledger.jsonl`, and the card prints how many times the split has been scored
  for this manifest (`--backfill-ledger` seeds the ledger from saved cards). A run trained on
  another view than `--view` is refused before any test pass. A run that never saw a labelled
  train task has an untrained value head, so criteria 2, 3 and 5 are INCONCLUSIVE for it, with
  that reason. The val CE column scores step 0 the same way for every model.
  `worldmodel train` now trains on the stream view by default (`--view task` keeps the old
  per-task order) and records the view in `summary.json`. The first real attempt (2026-10-07; ~23.6k steps — the count grows with the transcripts, no
  labels yet) is G1 FAIL: on criterion 1 the JEPA is about 7% worse than the logistic baseline on test CE
  (stream view; +0.7% with a CI spanning 0 in the task view), and criterion 4 fails because
  the early epochs sit outside the SIGReg bound. Criteria 2, 3 and 5 are INCONCLUSIVE until
  gold labels exist.
- Optional extra `worldmodel` (`mlx>=0.32.3` on Apple Silicon only, plus numpy). mlx 0.32.3 and
  its required `mlx-metal` were vetted before the install: from PyPI, MIT (LICENSE checked in
  the wheel), released 2026-09-29, and the OSV query returned no advisories for either (`{}`).
- `apex-router worldmodel build|stats` (P6 E1): a step dataset for the world-model experiment.
  Every tool call in pi sessions, Claude Code sessions and their subagents becomes one record —
  action class, tool name, error flag, parsed test counts, size buckets, time gap, workflow phase,
  model — grouped into tasks (the same user turns `labels` uses, with their outcome label) and
  split train / val / test by session start time. It is written to `~/.apex-router/worldmodel/`
  with a manifest, stores no prompt, command or file text, and rebuilds idempotently. The action
  classifier reads a bash command segment by segment, so `cd`, `echo`, `timeout` and `VAR=`
  prefixes no longer hide the real command, and `.venv/bin/pytest`, `python -m pytest`,
  `uv run`, `npm test`, `make test`, `go test` and `cargo test` count as tests. It agrees with a
  300-command validation set that the implementer labeled; the owner has not yet reviewed those
  labels.
- `apex-router snapshot [--json|--menubar]`: one read-only readout for a menu bar widget —
  upstream pressure and errors in the last 15 min, the newest 5h/7d limit meter and its age, the
  Claude Code / pi / Codex sessions active (< 5 min) or idle (< 60 min) by log mtime, the local
  worker and its queue, and whether the proxy answers. It writes nothing (not even
  `pressure.json`) and every part fails open. `--menubar` prints a SwiftBar plugin: a dot that is
  green, orange or red for GREEN/AMBER/RED and gray when the sample is too small to tell, plus the
  count of active agents. `integrations/swiftbar/` has the plugin script. Other programs can add a
  menu section by dropping `{title, rows, ts}` JSON into an `adapters/` directory.
- `apex-router snapshot`: which agent is using what. Each Claude Code session is tied to its
  process through `~/.claude/sessions/<pid>.json` (stale files for dead or reused pids are
  ignored); pi and Codex sessions through the process's working directory, only when that match is
  unambiguous. Each agent row shows, in fixed-width columns, Claude's own busy/idle status, the
  physical footprint of the session process plus everything it started, its cpu % and disk MB/s
  right now (two samples ~250 ms apart; lifetime io, cpu time, uptime and resident size are in the
  tooltip), its requests and output tokens through the proxy in the last 60 min, and the context
  size of its latest request. Its submenu has the token split (uncached input, cache reads, cache
  writes, output) and cache share summed over the main thread and every subagent, the main thread,
  up to 8 subagents (running, then erroring, then by output; each with its run time, time since
  its last write and its context size), the busiest child processes by real name (the basename of
  a node/python script, e.g. `pyright-langserver`; nothing else from the command line is kept) and
  the models it called. Subagents and sessions past the caps (8 active sessions, 8 subagents per
  session, about 80 menu lines) become one `… N more (x req, out y)` line, so no total is lost.
  The context is shown against its window from a model-family table (Opus / Sonnet 4.6+ and
  Fable 1M, Haiku 4.5 200k, `[1m]` 1M): `ctx 283k/1M 28%`; any other model shows the size alone
  unless a successful request of the same thread and model passed 200k. Active / idle for a
  Claude session is Claude's own busy / idle status (log mtime only as the fallback), and each
  row adds `r5 N/min` (requests per minute over 5 min) and the age of its newest request.
  The bar reads `● N ⚠` when a subagent has run for more than 20 min, an agent had an error in
  the last 5 min or a 60-min error rate of 5 % or more, or a context is 85 % of a known window;
  the dot colour is still the pressure rule. Lines stay within 110 characters (`--graph`: 120,
  wrapped) and control characters are stripped from every line.
  The widget shows no dollar amounts. A refresh run inside a session does not count itself. The
  System section shows GPU load and memory (system-wide: macOS gives no per-process GPU figure
  without root), the load average, and ollama once, with each model's VRAM and when it unloads.
  All subprocess and network calls, the sampling window and the subagent log scan share a
  0.45 s deadline; a source that would start later is skipped and listed as unavailable. Subagent
  logs are read only for the (at most 8) active sessions the menu shows. `--graph` prints who-spawned-what / who-runs-what /
  who-calls-which-model as a text tree; `--json` carries the same data under `system`, `graph`
  and each agent's `res`. Subagents run inside their session's process, so memory, cpu and io are
  per session; a subagent's load is its proxy traffic. Still read-only and fail-open. A Claude
  session that is only waiting on a working subagent now counts as active (it used to drop into
  the idle fold after 5 min).
- `apex-router zeno`: where the last bit of reliability goes. `zeno report` reads the proxy
  telemetry and xval runs and shows how per-call failures compound over long sessions (p^n against
  the clean-session rate actually seen), whether each extra "nine" costs more than the last (cost
  per nine across xval output caps), how much of the failure space has no name yet (failures with
  no recorded cause, chance the next failure is a new kind), and which clients or context sizes the
  overall rate hides. It measures whether calls finished, not whether answers were right.
  `zeno horizon`, `zeno ladder` and `zeno frontier` are the same math on numbers you give it. See
  docs/research/2026-10-06-zeno-frontier.md.
- `zeno report`: a Markov column beside p^n (section 1b). On this machine a failed call is far
  more likely right after another failure (lag-1 autocorrelation ≈ 0.45), so p^n badly
  under-predicts how many long sessions finish clean. The new section fits a two-state chain
  (P(ok|ok), P(fail|fail), mean failure-burst length) and shows its P(clean) per session length next
  to the observed rate and the p^n prediction, plus a held-out check (fit on the first 70% of
  sessions, score the rest). It is a second, independent estimate; zeno's p^n numbers are unchanged.
  The chain is closer than p^n but still under-predicts long sessions: it treats every session
  alike and ignores between-session and over-time differences. Also in `--json` as `markov`.
- `apex-router xval <codex exec args…>`: a drop-in for `codex exec` in cross-validation that
  controls GPT cost per run. It sorts the review into diff / report / files / investigate and
  picks a per-command output cap (2k–10k tokens) plus an optional "evidence budget" hint. Each
  category learns which setting to use, by picking the better of two candidates sampled from
  running probabilities that fade older runs. The cost comes from the Codex session log; success
  means the run finished with a verdict. Repeated command output is ~54% of GPT review tokens, and
  ~40% of report reviews died before giving a verdict. `xval stats` shows the probabilities;
  `xval feedback <run_id> ok|bad` corrects a run's result. The cross-validate-codex skill now uses it.
- Proxy: opt-in stable Codex cache key (`APEX_CODEX_CACHE_KEY=1`, shards via
  `APEX_CODEX_CACHE_KEY_SHARDS`, default 4). Codex starts every run and subagent with a new cache
  key, so the identical ~15k-token start of each run was never reused. The proxy now gives runs with
  the same start (model, instructions, tools, permissions, repo and date) one shared key, split over
  a few shards by the first task prompt. It's the only change the proxy makes to request bytes, it's
  off by default, and each row records it (`cache_key_rewrite`, telemetry schema 9). Measured: a new
  run's first call went from 0 to 14,919 of 15,122 tokens cached.
- pi: `>>review` — the independent cross-validation reviewer, GPT-6.1 Sol through the proxy's Azure
  GPT path (a different vendor than the Claude families that write the code). It needs a
  `foundry-gpt` provider in `~/.pi/agent/models.json` and an `api-version` pin in `auth.json`; see
  RUNBOOK-pi-integration.md.

- pi on a subscription setup (Claude Pro/Max OAuth, ChatGPT/Codex subscription, no Foundry):
  `integrations/pi/registry-overlay.subscription.json` moves pi's Claude-named families,
  `>>review` and `/learn` to `openai-codex`. Anthropic now bills third-party clients on
  subscription OAuth to extra usage (`400 … Third-party apps now draw from your extra usage`), so
  Claude stays in Claude Code. The `learn` spec accepts explicit `validate`/`explain` ids. See
  RUNBOOK-pi-integration.md.
- `apex-router worldmodel baseline | chains | progress [--synthetic N] [--json]` (P6 track E2,
  needs numpy + scipy): the baselines and the Markov/Zeno layer the world model has to beat.
  `baseline` scores a unigram prior, Markov order 1–3 (Dirichlet α = 0.5 backed off to the lower
  order, per-task-type rows shrunk to the pooled chain) and a logistic regression on hand features
  on held-out sessions: next-action cross-entropy and perplexity, outcome Brier and 10-bin ECE
  after the first 8 steps, each with n and a session-bootstrap 95% CI, plus a BIC order test and
  a per-day breakdown. `chains` fits an absorbing chain over action classes (SUCCESS / FAIL /
  ESCALATE / ABANDON) per task type: expected steps, absorption odds, ρ(Q), and a workflow
  ranking, with model vs observed task-length spread as a misspecification check. `progress` runs
  the Zeno detector (geometric fit of failing-test progress over the last 4 test runs, limit
  v_∞ = v_t + Δ_t/(1 − r) with a residual-bootstrap CI widened by the signal's resolution; flag
  when the CI stays below all-tests-passing; plateaus and regressions never flag), a separate
  stalled (stuck) detector and a sliding-window ρ(Q) detector, each against step-count and
  wall-time cutoffs (G1 criterion 5: wins vs losses, FPR, steps saved — rule pending owner
  sign-off). The best baseline for each G1 bar is chosen on val. Outcome numbers are marked gold,
  provisional (weak labels) or inconclusive. Reads `~/.apex-router/worldmodel/` read-only;
  `--synthetic N` generates data with known (planted) dynamics. The commands are exposed as
  `worldmodel.readout.COMMANDS` for the `worldmodel` dispatcher.

### Fixed
- Route log on the subscription setup: pi's Claude-named families run GPT models there, but
  `tier_of` knew only the Claude tiers, so those rows had no tier and were mixed into the
  per-task-type rate with the Claude rows. GPT ids now map to GPT tiers (`gpt-luna` <
  `gpt-terra` < `gpt-sol` < `gpt-sol-6.1`), and a gpt id never maps to a Claude tier.
  `read_rates` and `route-join` add per-tier `by_tier` rows. The headline rate that
  `route-advise` prices at Claude cost leaves GPT rows out. `CHEAP_START_TIERS` adds
  `gpt-luna` and `gpt-terra`, and escalation is ranked within one family only. A move between
  Claude and GPT is marked `cross_family` and left out of every rate. `route-log --family`
  (an additive field) records the cued family next to the model that ran, and the pi
  extension passes it. `expected_models(..., surface="pi")` reads a pi family's model from the
  active registry. `route-check --record` uses it for a pi row sent without a `matched`
  verdict, so a remapped family is not drift. Rows from the extension already carry
  `matched` (the resolved id against the family's id), so they were never counted as drift.
- `labels`: task boundaries skip what the harness injects as a user record. Claude Code records
  flagged `isMeta` (skill bodies such as Claude in Chrome and `/update-config`, image metadata
  written after a screenshot tool result) or `isCompactSummary` are no longer requests, and
  known prefixes catch the same bodies in transcripts without the flags. A pasted image's own
  record (`[Image #N] …`) now counts as a request. `worldmodel steps` cuts tasks the same way.
  `lf_repeated` drops `[Image …]` placeholders before comparing, so two screenshots in a row are
  not a repeat. `lf_next_negative` (and the negative check inside `lf_commit`) no longer fires
  on a "no" that turns down something the agent offered at the end of its message ("no more
  pushes" after "say push when you want them there"). "no, still broken" still counts. Task ids
  are positional, so `labels build` now matches earlier rows by session and request timestamp.
  Gold labels follow their request to its new id (`id_was` keeps the old one). Gold on a record
  that is no longer a task becomes `orphan:<id>` and is shown in `labels report`. A judge vote is
  kept only if the votes and call count it saw are unchanged. On the live data this went from
  536 to 527 tasks; 85 gold rows kept their id, 11 moved and 4 were orphaned.
- Menu bar widget: a loaded machine no longer misreports live services. The shared refresh
  deadline is 1.2 s (was 0.45 s); with endpoint security slowing every spawn, the old budget
  skipped launchctl / ioreg / ollama / `/healthz` on about half the refreshes, and a skipped
  worker lookup read `not running`. A skipped source now says `not checked`. GPU memory is
  ioreg's `Alloc system memory` (29.8GB beside a 26GB ollama model; `In use` read 1.4GB), and a
  pinned ollama model (`keep_alive -1`) reads `pinned`, not `unloads 106751d`.
- Menu bar widget: fewer, non-redundant lines (44 -> 31 on a one-session machine). No `errors 15m:
  0`, no family levels equal to the overall level, no limit section without a meter, no `Σ` line
  for a session without subagents (it repeated the row), one `model` line for a single model, no
  plugin `Refresh` (SwiftBar has one); proxy, worker, GPU/load and ollama share one System section.
- Menu bar widget: a `Quality · 24h` section — routing classification and escalations
  (`route_log`), tier conformance (`conformance`), requests / retries / failures with causes (proxy
  telemetry), and verification (xval verdicts, codeqa citation grounding). Read-only, fail-open,
  ~40 ms.
- Proxy telemetry v10: `bytes_up` / `bytes_down` per request (forwarded body / raw response bytes).
- Menu bar widget: network traffic — per agent from the proxy's wire bytes, total for the Mac from
  the physical interfaces' counters between refreshes (the Stats app's method). Anthropic palette
  colours on every line (lines with neither colour nor action were greyed out by macOS); SwiftBar's
  own submenu hidden; GPU sparkline, %, memory and load on one line; one ollama line; proxy only
  when down; worker memory/cpu and disk io out of the way unless they say something.
- Proxy: Codex traffic is now attributed to its session. codex-cli sends its thread id in a
  `session-id` header (the same uuid as its rollout file); the proxy read only
  `x-claude-code-session-id`, so every Codex row had `session_id` null and the widget showed a
  Codex session at `0 req · out 0`. Claude Code's header still wins. Rows logged before this
  stay unattributed.
- Menu bar widget: one `--menubar` collect is bounded to 3 s wall clock (SIGALRM); the shared
  deadline only caps when a source starts, so a peer trickling bytes could hold a refresh. The
  timeout shows the gray dot and writes no history sample. GPU memory is read per accelerator.
- Proxy: a response stream that breaks after it started now records why
  (`error_cause = midstream_<error>`, e.g. `midstream_ReadError`). Before, these rows were marked as
  errors with no cause — 56% of all error rows on the test machine. A rejected request keeps its
  `http_<status>` label. Pressure levels are unchanged: they ignore `midstream_*` as they ignored
  the unlabeled rows.
- The datapce plugin no longer repeats the band as a status entry (`⚠ datapce: apex ●GREEN $0.91`)
  in the terminal and desktop app. The status entry now appears only where the band can't draw:
  VS Code, mobile, `band: false`, or after Hide (at once, with the current cost). A status entry left
  by an earlier version is cleared on the next update.
- pi: `>>sonnet`, `>>frontier`, `>>opus`, `>>deep` and `>>gpt-sol` no longer fail with "model not
  found". pi's built-in model list has no `claude-sonnet-5-5`, `claude-opus-5-5` or `gpt-6.1-sol`, so
  the Claude shortcuts (and `>>haiku`) now go through the `foundry` provider (`it-entra-claude-*`, via
  the proxy), and `>>gpt-sol` is back on `gpt-5.6-sol`. `>>fable` is unchanged. New registry key
  `provider_id_prefix` maps a provider to the prefix its model ids carry (`foundry` → `it-entra-`).
- Registry: an overlay pi family that pins an `id` no longer keeps the default family's `tier`
  (deep merge made the stale tier win, so the Python side kept resolving the Claude id). The
  resolution key (`id`/`tier`/`source`) an overlay names replaces the default's; `provider` and
  `effort` still merge. A family carrying both `id` and `tier` resolves the `id` (Python and pi).
- pi `>>auto` dispatches a resolved tier id through that tier's family, so a family remap
  (foundry, subscription overlay) applies to it; before, it looked the raw Claude id up under
  `anthropic` first. If that family's switch fails it stays put instead of trying `anthropic`.
- pi `/learn` now validates with Sonnet 5.5 and explains with Opus 5.5 on `foundry`; it asked for
  `anthropic/claude-sonnet-5`, which your setup has no credentials for.
- Menu bar widget: a hung subagent no longer looks done. While its session is `busy`, the
  subagent whose log went quiet (> 60 s) last, with nothing written since in the main log or any
  other subagent, is shown as `⧗ waiting?`; after 10 min it raises the session's `⚠`. A heuristic
  (a long tool call looks the same), documented as such in the plugin README.
- Menu bar widget: a live `pi` / `codex` process with no session log in the last hour is listed
  under idle (`pi · <cwd name> · quiet · up 10d · 159MB`) and counted in the Agents memory; before,
  such processes were invisible (five on the test machine, ~620MB).

### Changed
- `agent_resources` is a package (`base`, `procs`, `sessions`, `telemetry`, `subagents`,
  `system`, `graph`, `text`); `from apex_router import agent_resources` and every name it had
  keep working. The duplicated helpers have one implementation each (`fmt_age` = `fmt_dur`,
  `fmt_mem` = `fmt_mb` with `?MB` for a missing value, one `_num` / `_err`, `_status_word` =
  `display_state` esc()'d). Output is unchanged: a new test compares `snapshot --json`,
  `--menubar` and `--graph` on a fixed fake HOME byte for byte with a committed golden.

## 0.4.1 — 2026-10-04

### Security
- 0.4.0 stored an unsalted repo hash that one list of candidate paths, hashed once, could match
  on any machine; 0.4.1 salts it per install and drops old values. The token is the first 16 hex
  of HMAC-SHA-256 of the path, keyed with a random salt drawn once and kept in the local plugin
  store, so tokens can't be matched across machines or against precomputed hashes; anyone who can
  read the local plugin store can still test candidate paths against them. Rows also keep the
  dispatch description (up to 120 characters, may name a repo or path) and the session id, which
  Claude Code's local transcript folders map back to a directory. Nothing leaves the machine. The
  repos-seen count restarts once; other counts are kept. Restart open sessions after upgrading: a
  session still running 0.4.0 keeps writing unsalted values until the next 0.4.1 session start
  drops them.

### Fixed
- The test suite no longer writes to the live install; a guard fails the run if it does.

### Changed
- Hook retirement moves from 0.4.1 to 0.4.2; the deprecated hooks keep running beside the plugin
  until then.

## 0.4.0 — 2026-10-04

One product, one install: datapce, a Claude Code plugin (alias apex-router), with the pip package
as its optional backend. Advise-only: it never changes the model you chose.

### Added
- `plugin/` — the datapce hooks module: a status band and the `/apex` pane with upstream pressure,
  session cost and every Agent and Workflow dispatch (tier requested and run, outcome, duration,
  tokens = input + output + cache reads + cache writes); `/apex handoff` and the handoff toast; a
  ≤ 2-line pressure note in the Agent and Workflow tool descriptions only when pressure is not
  GREEN; the one condensed `datapce` skill. One route row per dispatch (no prompt text, file
  contents or command text), finished at `turn.complete` as `ok` or `error` (`async` only for
  agents still running at session end). /clear and /resume re-identify the session on the next
  turn. Marketplace: `.claude-plugin/marketplace.json`.
- A ledger per task type × tier of completion and cost (n, ok %, error kind, `tok(all) μ`, `dur μ`).
  It is not answer quality: `QUALITY_LABELS` is off in v1, so no tier advice and no evidence table
  reach the planning skills; cells keep counting for v1.1's quality label.
- `apex_router.core` (pce-core): Welford, EWMA (float and Q16), Jacobi PCA, Q/T² with the χ²
  limit, Page CUSUM, the penalty and cell state machines, OLS cost and budget burn, with a
  TypeScript mirror held to generated parity fixtures (`python -m apex_router.core.fixtures`).
- `route-join` keeps one row per `(session_id, tool_use_id)` (finished over async, plugin over
  hook; `dispatch_deduped`) and reports `writer_parity`: plugin vs hook dispatches per UTC day,
  compared by `(session_id, tool_use_id)` key and filed under the plugin row's day, so a dispatch
  spanning 00:00 UTC still pairs (`parity_span_days`; `parity_days`, the days with plugin rows
  where the hook never disagreed, i.e. every hook dispatch also has a plugin row; `parity_until`;
  `gate_open`; a Workflow-only day is quiet, neither a break nor a count).
- A repository hygiene test.
- `python -m apex_router.ornith.ornith_client --probe-thinking` — proves `reasoning_effort:
  none` is honoured. Exit 0 = thinking off; 1 = evidence of thinking (reasoning present, an
  unterminated inline `<think>`, or an empty answer); 2 = inconclusive (busy, down, other error).

### Changed
- `apex_router.stats` re-exports from `apex_router.core.stats`; `bradley_terry` removed (no caller).
- `install.sh` adds the datapce marketplace from the install directory and installs
  `datapce@datapce`; `apex-router-skills` is no longer added by default (opt in with
  `--skills-marketplace runapex/apex-router-skills`).

### Deprecated
- `hooks/agent-route-log.sh` and `hooks/cache-handoff-nudge.sh`: they keep running beside the
  plugin (route-join counts each dispatch once). `--agent-route-log-hook` and
  `--cache-handoff-hook` still wire them, with a warning. Both are retired in 0.4.2, once
  `apex-router route-join --json` shows `stats.writer_parity.parity_span_days` ≥ 14
  and `parity_days` ≥ 10 and `parity_until` (the last streak day) is within the last 2 days
  (`stats.writer_parity.gate_open`).

### Fixed
- ornith: inference lock is bounded (`ORNITH_LOCK_TIMEOUT_SECS`, 120 s) → `OrnithBusy`
  escalates instead of hanging a turn.
- apex-ornith review: thinking OFF, 1024-token budget, bounded `git diff`; a busy or failed
  local tier now exits 3 (unavailable) / advisory 0 instead of a traceback, and truncated
  reviews keep their partial findings.
- pressure: connect-retried-then-succeeded rows no longer count as transport faults (they
  pushed flaky-link boxes to AMBER/RED and shed work for no upstream reason).
- connect-retry backoff test no longer depends on wall-clock timing.
- handoff nudge: threshold is the clamped median (25M–100M) of per-session cache reads,
  not a p80 that rose with every long session.

## 0.3.0 — 2026-10-02

The theme of this release is closing the loop between what the proxy measures and how an
agent dispatches work: a pressure signal before a fan-out, a local pre-read before a heavy
review, and a label stream for Claude Code subagent dispatches that the outcome router was
blind to.

### Added
- `apex-router pressure [--check]`: GREEN / AMBER / RED / UNKNOWN from the last 15 minutes
  of proxy telemetry, with an exit-code contract (0/1/2/3, 4 = usage error), a sample
  floor, a `retry-after` override, and an atomically written `~/.apex-router/pressure.json`.
- `apex-router review-preread`: the local Ornith tier reads a diff and returns claims to
  verify for an independent heavy reviewer. Nonce-delimited untrusted input, deterministic
  injection-marker scan, own telemetry lane (`preread`), explicit exit codes (0/2/3).
- `hooks/agent-route-log.sh` + `install.sh --agent-route-log-hook`: a fail-safe
  `PostToolUse` (matcher `Agent`) hook that appends one label-pending route-log row per
  Claude Code subagent dispatch. `apex-router route-join` infers escalations offline and
  joins proxy telemetry on `(session_id, agent_id)`; nightly runs the join.
- Proxy: bounded transient-transport retries before the first client byte, and after the
  body was sent only on a fast-fail (`APEX_RETRY_FAST_FAIL_MS`, default 3000) against a
  stateless completion endpoint. Hard caps on retry count and cumulative backoff.
- Telemetry schema 8 (`upstream_rejected`, `connect_retries`), schema 7 (upstream error
  bodies + rate-limit headers), schema 5 (`error_cause`, pricing aliases incl. Foundry and
  GPT-6). Shared `apex_router.telemetry_path` resolution (`APEX_TELEMETRY` →
  `APEX_HOME/telemetry.jsonl` → `~/.apex/telemetry.jsonl`).
- `scripts/change_classifier.py`: a multi-model panel that classifies a diff on change
  class, requirement fit, and blast radius, and reports where members disagree. Ships a
  working four-member, three-vendor example panel; validates panel results and exposes
  incomplete evidence.
- codeqa: `CODEQA_JUDGE_MODE=screen` by default when a judge is pinned (cheap tier screens,
  the pinned judge adjudicates only struck or uncertain claims).
- skill-bench: nightly over-time automation with disjoint-window slicing; SkillOpt's
  quality axis behind apex's gate (measure-only).
- Pi: per-tier cues (`>>sonnet`, `>>opus`, `>>fable`, `>>auto`), escalation outcomes
  auto-captured on every turn, GPT-5.6 tier aliases, within-Kimi-family routing.
- Tuner: break-even / minimum-value gate primitive (un-wired by decision, see
  `docs/DESIGN-breakeven-gate-wiring.md`); SKILL.state driver, A/B benches, drift and
  transferability experiments (shipped default-off, see `docs/DESIGN-skill-state.md`).
- Route conformance log and `apex-router route-check` readout (agent surface is
  intent-only and never counts toward drift).
- Installer: version-controlled cold-load-stall fix (keepwarm agent + guard debounce);
  `--ornith-serve` persistent local stack.

### Fixed
- store: hardened concurrent WAL initialization.
- session: heal `state.db` schema drift that silently disabled the matcher.
- doctor: schema v5 was missing from `SUPPORTED_SCHEMA`, dropping every fresh row.
- compile: stale validators path after the `apex` → `proxy_engine` rename.
- proxy: preflight port bind on `serve` with a single-owner message.
- ornith: debounced version guard stops drain restart thrash.
- metrics: Codex cached tokens are a subset of input (corrected downshift eligibility and
  cost ratio); Kimi pricing.

### Docs
- Runbooks: pressure, review-preread, route-conformance, pressure-aware pi integration.
- Phase-0 outcome-router report (`docs/phase0-outcome-router/`) and the cache-routing
  research note. Design records for the shadow-A/B labeler, break-even gate wiring,
  SKILL.state, and the learn chain. Local-serving threat-model note.
- Public landing page (`docs/index.html`, GitHub Pages).

### Companion
- [apex-router-skills](https://github.com/runapex/apex-router-skills) 0.7.0: `model-routing`
  checks `pressure` before a fan-out; `cross-validate` takes the `review-preread` claims
  list and records pre-read recall; new `evidence-labels`, `unattended-loop`, and
  `dependency-vetting` skills (0.7.0).

## 0.2.0 — 2026-08-23

- Toolkit migration: codeqa + ornith offload subsystem merged into apex-router; shared
  model registry; live `resolve` consumer; nightly adaptivity pass; measuring proxy as an
  optional `[proxy]` extra.
