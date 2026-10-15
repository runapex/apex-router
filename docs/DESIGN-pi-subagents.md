# DESIGN — sub-agents for pi (spawn / status / wait), plus a hang guard

**Status:** P0, P1 and P2 BUILT (2026-10-15); P3 partly (agents dir, `xval` kind, worktree; no `/fanout` template); P4 not started. pi 0.85.1. Owner: integrations/pi. Results: "Build results" at the end.
Test procedure (STE100 rules): `TEST-pi-subagents-STE.md`.

## P0 results (2026-10-14, `integrations/pi/p0/p0_checks.py`, haiku-4-5)

| # | check | result | design impact |
|---|---|---|---|
| 1 | `pi.sendMessage(followUp, triggerTurn)` from a timer 3 s after the tool returned | PASS (agent answered GOT-PING in a 2nd agent run; no throw) | push-on-complete works; the disk fallback is not needed |
| 2 | `--mode json` liveness during a long bash call | PASS ticking (max gap 10 s, `tool_execution_update` per line); silent command: **60 s gap for a 60 s command** | a silent tool call emits nothing: `idle_s` must exceed the child bash timeout (confirms R2) |
| 3 | `--session-id` resume after SIGKILL mid-tool-call | PASS (resumed child recalled PELICAN-7) | resume design holds |
| 4 | SIGTERM to pi's process group, then SIGKILL | PASS, grandchild gone — **but not via the group**: pi's bash tool starts commands in their own process group/session; pi cleaned them up on SIGTERM | cancel must be SIGTERM-first |
| 5 | SIGKILL only | grandchild (and its bash shell) **survive as orphans** | cancel snapshots descendant pgids (`ps -A -o pid,ppid,pgid`) before signalling, SIGTERMs pi, waits 10 s, then SIGKILLs every snapshotted pgid; the watchdog never SIGKILLs first |


## Problem (measured, `~/.pi/agent/sessions`, 14,495 bash calls)

pi deliberately ships without sub-agents (`docs/usage.md`: "does not include … sub-agents … background
bash"). Today delegation is hand-rolled in bash, and it fails in three ways:

1. **Blind fan-out.** Children are started with `nohup pi -p … &` (30 background launches, 6 sessions). The parent
   then babysits with `sleep N; pgrep …; wc -l …` — **90 sleep-polls, 303 minutes of sleep** after launches. It
   can't tell "working" from "hung" from "dead": one fan-out read `alive=0 report=no` for three live children
   because its `pgrep` pattern was wrong (session 2026-10-14, `fan-a147177-*`), and results come back by
   convention (`${x}_report.md`) rather than as a tool result.
2. **Foreground hangs.** 311 foreground child-agent calls (`codex exec`, `apex-router xval`, `pi -p`); 10 aborted
   or timed out. Overall bash: 18 `timed out after N seconds`, 36 `Command aborted`; the top causes are ssh/expect,
   long `sleep`, `codex exec` (which waits on stdin unless given `< /dev/null`), and long python runs.
3. **No lifecycle.** No liveness watchdog, no hard deadline, no cancel, no concurrency cap, no pressure check,
   orphans survive the parent, and a killed child's work can't be resumed.

What Claude Code's Agent tool gives that pi lacks: an isolated-context child that returns a structured result
to the parent, runs in parallel or the background, can use a worktree, and is cancellable.

## What pi already offers (verified in the installed docs)

- `pi --mode json -p` streams every session event as JSONL (`message_end`, `tool_execution_*`, `agent_end`, usage
  per message), so a parent can see liveness and the final answer without scraping files.
- `--session-id <id>` (create-or-resume) and `--session <path>`: a killed child can be continued.
- `--tools`, `--model`, `--thinking`, `--append-system-prompt`, `-e <extension>`: per-child config.
- Extension API: `pi.registerTool`, `pi.sendMessage(…, {triggerTurn, deliverAs: "followUp"})` to push a message
  into the session, `pi.on("tool_call")` with **mutable `event.input`** and `{block, reason}`,
  `session_start` / `session_shutdown` for resource lifecycle.
- `examples/extensions/subagent/` (not installed here): spawns `pi --mode json -p --no-session` with
  `stdio: ["ignore", …]`, parses events, supports single / parallel (8 tasks, 4 concurrent) / chain, and agent
  definitions in `~/.pi/agent/agents/*.md`. It is **synchronous only**, with **no timeout or watchdog**: a hung
  child blocks the parent's tool call until the user aborts. Good base for the spawn/parse/render code; not for
  lifecycle.
- Known constraint (`apex-nudges.ts` header, docs §1267): a captured `ctx` is stale after the turn; deferred
  callbacks must not use it.

## Design

Two extensions in `integrations/pi/`, loaded in the parent and passed to every child with `-e`.

### 1. `apex-hangguard.ts` — fixes most of today's timeouts on its own (ship first)

`tool_call` handler on `bash`, mutating `event.input`:
- **Default timeout**: if `timeout` is unset, set `APEX_PI_BASH_TIMEOUT` (default 600 s). The model can still
  ask for more explicitly.
- **stdin**: append `< /dev/null` to `codex exec` / `apex-router xval` / `claude -p` / `pi -p` invocations that
  don't redirect stdin (the documented codex hang).
- **ssh**: add `-o BatchMode=yes -o ConnectTimeout=15` to `ssh`/`scp` when absent (prompts become errors, not hangs).
- **Polling**: block a bare `sleep N` with N > 120 when a sub-agent job is running, with
  `reason: "use agent_wait(job, timeout) instead of sleep-polling"`; otherwise allow.
- **Never blocks anything else.** Every rewrite is logged to `~/.apex-router/pi-hangguard.jsonl`
  (original → rewritten), so false positives are auditable.

### 2. `apex-agents.ts` — the sub-agent tools

| tool | behavior |
|---|---|
| `agent_spawn {agent?, task, cwd?, worktree?, model?, tools?, timeout_s?, idle_s?, background=true}` | starts a child, returns `job_id` at once (background) or waits (foreground, still with deadline + watchdog) |
| `agent_status {job_id?}` | per job: state (queued/running/done/failed/killed/timed_out), elapsed, turns, tokens/cost, last tool call, last-event age |
| `agent_wait {job_ids, timeout_s, mode: "all"\|"any"}` | blocks up to `timeout_s` (≤ 10 min per call), returns finished results; never an unbounded wait |
| `agent_cancel {job_id}` | snapshot descendant pgids → SIGTERM pi → 10 s → SIGKILL every snapshotted pgid (P0 #4/#5: a group kill alone misses the bash tool's commands) |
| `agent_result {job_id}` | final answer (capped 50 KB) + path to the full event log + resume command |

**Child process.** `pi --mode json -p --session-id apexjob-<id> -e apex-hangguard.ts [--model --tools
--thinking --append-system-prompt <agent prompt>] "Task: …"`, `stdio: ["ignore", pipe, pipe]`, `detached: true`
(its own process group; NOTE P0 #4: pi's bash tool puts each command in a further group, so cancel must use the
descendant-snapshot procedure in `agent_cancel`, not a bare group kill). The session id makes every child
resumable: `pi --session-id apexjob-<id> -p "continue"`.

**Job directory** `~/.apex-router/pi-jobs/<id>/`: `meta.json` (task, agent, cwd, pid, pgid, started, deadlines,
state), `events.jsonl` (raw stream), `result.md`. State lives on disk, not in the extension, so status survives a
parent turn, `/reload`, or a parent restart (`session_start` re-attaches to live pids and marks dead ones `failed`).

**Two deadlines, both enforced by the parent's monitor:**
- `idle_s` (default 600): no JSON event for that long → the child is hung → kill, state `timed_out(idle)`, result
  carries the last tool call it was stuck in. A child stuck in one long tool call emits nothing, so the
  hang guard's child-side bash timeout must stay below `idle_s` (600 s by default).
- `timeout_s` (default 3600): wall clock → kill, `timed_out(wall)`; the session id is kept for resume.

**Completion is pushed, not polled.** When a background job ends, the monitor calls `pi.sendMessage({customType:
"apex-agent-done", content: <summary + result path>}, {deliverAs: "followUp", triggerTurn: true})` — the parent
wakes up when there's something to read. If P0 shows `sendMessage` from a deferred callback is unsafe (stale-ctx
class of bug), fall back to the `apex-nudges` pattern: the monitor writes `done` to disk and a `turn_end` /
`before_agent_start` handler injects it synchronously.

**Concurrency and pressure.** Cap 4 running (config). Before each spawn read `~/.apex-router/pressure.json`:
AMBER → cap 2, RED → queue with state `queued(pressure)` and start when it clears. Matches the model-routing skill.

**Agent kinds.** `~/.pi/agent/agents/*.md` (the example's format: name, description, tools, model, prompt).
Ship `scout` (read-only, cheap model), `worker`, `reviewer` (read-only), and **`xval`**: runs
`apex-router xval -m it-entra-gpt-6-astra -s read-only … < /dev/null` as the child instead of pi, under the same
deadlines, so Codex reviews stop being a hand-rolled `nohup codex exec` + `grep VERDICT`.

**Worktree isolation.** `worktree: true` → `git worktree add -b apexjob/<id> <tmp> <base>` and run the child there
(what the fan-out sessions did by hand). Result includes the branch and `git diff --stat`. Worktrees are never
auto-deleted when they have commits.

**Shutdown.** `session_shutdown` leaves background children running (they're resumable and on disk) unless
`APEX_PI_AGENTS_KILL_ON_EXIT=1`; foreground children are always killed.

## Phases

| phase | scope | done when |
|---|---|---|
| **P0 spike (½ day)** — DONE | five checks, see "P0 results" | answers recorded above |
| **P1 hang guard (1 day)** — DONE | `apex-hangguard.ts` + tests | replaying the 54 historical aborted/timed-out bash commands through the rewrite: each is bounded or rewritten, none newly blocked that ran fine |
| **P2 agents core (2–3 days)** — DONE | spawn/status/wait/cancel/result, job dir, both deadlines, push-on-complete, cap + pressure, re-attach | fake-child tests pass: hangs → `timed_out(idle)` within idle_s+5 s; dies → `failed` with stderr; finishes → pushed result; parent restart re-attaches |
| **P3 kinds (1–2 days)** — PARTLY | agents dir, `xval` kind, worktree isolation, `/fanout` prompt template | the 2026-10-14 three-task fan-out reruns with zero `sleep`/`pgrep` in the parent |
| **P4 adopt + measure** | AGENTS.md / skills (model-routing, cross-validate-codex) say `agent_spawn` instead of `nohup`; rerun the session-log scan after 2 weeks | targets below |

**Targets** (same scan as the baseline): parent sleep-polls after launches 90 → 0; aborted/timed-out child-agent
calls 10/311 → 0 unbounded (every one ends in a typed state); no child alive past its `timeout_s`; zero
"report missing" fan-outs (results are tool results).

## Tests

TypeScript tests next to `plugin/tests` (same runner). A fake child (`tests/fixtures/fake-pi.mjs`) that emits
scripted JSONL — normal, slow-but-alive, silent hang, crash, huge output, never-exits-after-SIGTERM — so the
watchdog, deadlines, cap, cancel and re-attach are tested without a model. Hang-guard rewrites are table tests over
the historical commands pulled from the session logs.

## Risks / open questions

- **R1 deferred `sendMessage`** may hit the stale-object rule → fallback above (P0 decides).
- **R2 a long silent tool call is indistinguishable from a hang** at the JSON level; that's why the child-side
  bash timeout must stay below `idle_s`. Tools other than bash (custom ones) need their own bound.
- **R3 cost runaway** in background children: per-job `max_turns` and a cost cap from `message_end.usage`
  (kill at cap, state `killed(cost)`).
- **R4 the hang guard rewrites commands**: logged and opt-out per call (`# apex-hangguard: off` comment).
- **R5 upstream drift**: pi's extension API is young (0.85); pin the version in tests and keep the spawn code
  close to the upstream example so updates are a diff, not a rewrite.

## Build results (2026-10-15)

| Check | Result |
|---|---|
| Node tests (`node --test integrations/pi/tests/`) | 70 pass. Fake child: normal, model error, crash, silent hang → `timed_out(idle)`, slow → `timed_out(wall)`, cancel with a detached grandchild (also when the child ignores SIGTERM), cap 4 with queue, RED queue, cancel of a queued job, adopt after a parent restart, adopt when the owner ends later, one writer per job, no double count after adopt, notify retry, reused pid not killed, atomic meta. Hang-guard table tests. STE loader tests. |
| Hang-guard replay (14,659 old bash calls) | 43 command texts changed (41 stdin, 3 ssh; one call had two). `bash -n` accepts all 43. All other calls got only the default timeout. |
| Hang-guard replay, first design | 223 changes, with false positives: `ssh` in comments and heredoc text, `read scp`, `sshpass … ssh` (BatchMode breaks password auth), `\` continuation. The redesign changes text only when the scanner understands the whole command. |
| Live, rpc mode (`tests/live/push_rpc.py`) | PASS: spawn, parent turn ends, `apex-agent-done` message, parent answers in a new turn, no sleep |
| Live, `-p` mode, `wait_s` | PASS: one turn, no extra push message |
| Live cancel (`tests/live/cancel_real.ts`) | PASS: a real pi child runs `sleep 977`; cancel removes pi and the sleep |
| Depth limit | PASS: a child (`APEX_AGENT_DEPTH=1`) has no `agent_*` tools |

Differences from the plan:

1. The child writes stdout to `events.jsonl` directly (no pipe), so a parent exit does not stop the child.
2. Liveness is file growth, not a pipe read. Re-attach after a restart reads the same file.
3. Only the manager that owns a job sends its message: the job's parent process, or an adopter after that process ends.
4. In `-p` mode a push cannot arrive after the run ends. The tool result tells the model to call `agent_wait`.
5. Tests use `node --test` (Node runs `.ts` natively), not the `plugin/tests` harness.
6. One writer per job: `meta.json` has an `owner` token. Only the owner starts, reads events, watches, ends and notifies the job. Another manager adopts the job under a lock only when the owner process is gone.
7. Cancel sends SIGKILL only to processes whose start time still matches (no reused pid). It never sends SIGKILL to its own process group or to the group of its parent.

Codex cross-validation (`apex-router xval`, read-only):

| Pass | Result | Action |
|---|---|---|
| 1 | NO-GO, 9 findings: reused pid, two managers on one job, non-atomic meta, double count after re-attach, kill-on-exit of other sessions' jobs, lost message for queued jobs, notify marked before delivery, hang guard in a piped group and in `${…}`, quoted ssh options | All 9 accepted. Root fix: one writer per job, atomic meta, persisted event offset, pid start time. |
| 2 | NO-GO, 5 open: SIGKILL without identity check, retries used up in one tick, adopt only at start, result written after meta, subshell after a pipe | All 5 accepted and fixed, each with a test. |
| 3 | 6 of 6 FIXED. New P1: a child started during SIGTERM handling survived (the identity filter dropped its group). | Fixed: `killTargets` rule (b) adds new processes by parent or by first-tree group. Test `term-spawn` fails on the old rule and passes on the new rule. |
| 4 | Pass-3 P1 FIXED. Two new edge cases: a reused group in the 10 s grace period; a child that calls `setsid()` during SIGTERM handling and then loses its parent. | Reused group: fixed (a group whose leader pid is now another process is ignored), with a test. `setsid()` escape: accepted as a known limit (below). Not reviewed again. |

Known limit: a `ps` table cannot link a process to the job after it calls `setsid()` and its parent exits.
Such a process can survive a cancel. Only OS containment (Linux cgroups) can close this. macOS has none.
pi's own bash tool starts its groups before the cancel, so the cancel finds them.

