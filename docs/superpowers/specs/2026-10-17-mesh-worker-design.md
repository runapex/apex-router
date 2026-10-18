# Mesh worker: run agent jobs on another machine of the LAN mesh

Status: design, 2026-10-17. Extends [DESIGN-lan-mesh.md](../../DESIGN-lan-mesh.md) (v1). Default OFF.

## Goal

A node of the mesh (the **sender**, for example mac-work) gives an agent task to another node
(the **worker**, for example mac-home). The worker runs Claude Code on one of its allowlisted repos,
starts with Sonnet 5.5 at medium effort and escalates when the job check fails. The sender gets a
patch and a report back. Nothing is pushed.

## Owner decisions (2026-10-17)

| Item | Decision |
|---|---|
| Job kind | Full agent task: edit and bash in a fresh worktree of an allowlisted repo |
| Escalation | Automatic on a failed check, capped by the sender (`--max-effort`) |
| Runner | Claude Code headless (`claude -p`) through the local apex-router proxy. A GPT-only node uses `codex exec` instead (see "Codex runner"). The worker node chooses the runner, not the job. |
| Code and result | Repo from the worker's allowlist, ref fetched from its `origin`; result = `git format-patch` + report |
| Transport | Pull model: the mesh HTTP server stays GET-only |

## Architecture

```
mac-work (sender)                                mac-home (worker)
apex-router mesh submit  -> outbox/<id>.job      apex-router mesh worker
mesh up serves GET /mesh/v1/jobs?for=<worker>  <- polls alive peers every 5 s
                                                  takes a job, runs the ladder in a worktree
apex-router mesh result  -> GET /mesh/v1/results?for=<sender>   <- mesh up serves results/<id>
```

1. The mesh server keeps the v1 rule: no POST, no network write. Each node serves only what it
   wrote itself: its outbox (jobs it offers) and its results (jobs it ran).
2. The worker decides what it takes. A job that fails a check is never run.
3. `mesh up` must run on both nodes. `mesh worker` is a second process on the worker node.

## Job envelope

The sender signs one envelope of kind `job` with the mesh key (`protocol.encode`). Body:

```
{"id": "<16 hex>", "from": "<sender node_id>", "to": "<worker node_id>",
 "repo": "<name from the worker allowlist>", "ref": "<branch or 40-hex sha>",
 "task": "<text, max 32 KiB>", "check": "<shell command or empty, max 1 KiB>",
 "max_effort": "high" | "xhigh" | "max" | "opus", "created": <unix>, "expires": <unix, created + 3600 max>}
```

- `GET /mesh/v1/jobs?for=<node_id>` needs the v1 auth header (`to` = the server's node id). The
  response is a freshly signed `jobs` envelope: `{"jobs": [<job envelope bytes as str>, ...]}`,
  at most 8 jobs, only jobs with that `to`, not expired, not yet answered.
- The inner job keeps its own MAC. The worker checks it with `decode(..., guard=None)` and its own
  `expires` (the 30 s skew rule does not apply to an offered job; it applies to the response).
- The worker keeps a persistent seen-ledger (`worker/seen.jsonl`): a job id runs at most once.

## Worker checks (in order; any failure = reject, logged, never run)

1. Inner MAC valid; `to` equals this node; `from` is an alive peer; `now < expires`.
2. `id` not in the seen-ledger.
3. `repo` is in `~/.apex-router/mesh/worker.json` (`{"repos": {"apex-router": "/path"}}`). No other path.
4. `ref` matches `[A-Za-z0-9._/-]{1,200}` or a 40-hex sha; `git fetch origin <ref>` succeeds.
5. Sizes: task ≤ 32 KiB, check ≤ 1 KiB.
6. Caps: no job running now; jobs today < daily cap (default 20); `apex-router pressure --check` is not RED.
7. Not paused (`mesh worker --pause` writes `worker/paused`; `--resume` removes it).

## Run and escalation ladder

Effort steps (`claude --effort` levels): `medium` → `high` → `xhigh` → `max` → `opus` (Opus 5.5 at
max effort). The ladder stops at the
job's `max_effort`. Default `max_effort` = `high`.

For each step:

1. Make a fresh worktree from `origin/<ref>` under `<mesh home>-jobs/<id>/<step>` (detached HEAD).
   The worktrees live OUTSIDE the mesh home: the agent settings deny the mesh home (it holds
   `mesh.key`), and a worktree inside it was denied to the agent itself (every step BLOCKED).
2. Run `claude -p --model <sonnet | opus> --effort <level>` (aliases, so the worker's own deployment resolves the model) in it, with the
   worker settings file (below), the task wrapped as data with a fixed preamble (do the task, commit
   to the worktree, end with DONE or BLOCKED and one reason), and the wall-clock cap (default 30 min
   per job, all steps together).
3. If `check` is set, run it in the worktree (timeout 10 min). Exit 0 = pass.
   If `check` is empty, pass = the agent report ends with DONE.
4. Pass → stop. Fail → next step, or stop at the ceiling with status `failed`.

Each step writes one row to `worker/steps.jsonl` and one OffloadRecord (lane `mesh-worker`,
`gated=True` only when `check` is set): effort, model, tokens, cost from the proxy telemetry,
pass/fail, seconds.

## Permissions of the agent

`~/.apex-router/mesh/worker-settings.json` (Claude Code settings, passed with `--settings`):

- allow: Read, Edit, Write, Glob, Grep, Bash inside the worktree.
- deny: `git push`, `gh`, `ssh`, `scp`, `curl`, `wget`, `security`, reads of `~/.ssh`, `~/.kube`,
  `~/.config/certbot`, `~/.apex-router/mesh`, and any path outside the worktree.
- `--permission-mode dontAsk`: a tool use that the allow list does not cover is denied, never prompted
  (a headless run cannot answer a prompt).

WARNING: Claude Code permission rules for Bash are best-effort. A job can still run code on the
worker with the worker user's rights. The trust boundary is mesh membership (next section).

## Result

When the ladder ends, the worker writes `worker/results/<id>.json` and serves it at
`GET /mesh/v1/results?for=<sender node_id>` (v1 auth, freshly signed `results` envelope):

```
{"id", "status": "done" | "failed" | "rejected", "reason", "steps": [{"effort","model","pass","s","cost_usd"}],
 "patch_b64": "<git format-patch origin/<ref>..HEAD, max 512 KiB>", "report": "<agent report, max 64 KiB>"}
```

- The worker also serves a `running` status with the current step while a job runs.
- `mesh result <id> [--wait] [--apply]` on the sender fetches it, verifies the MAC, writes
  `<id>.patch` and `<id>.md`, and with `--apply` runs `git am` in the sender's current repo.
- Worktrees and results are deleted 24 h after the result is written.

## CLI

```
apex-router mesh submit --to NAME|NODE_ID --repo NAME --ref REF [--check CMD]
                        [--max-effort high|xhigh|max|opus] [--expires-min N] "TASK"   # prints the job id
apex-router mesh result ID [--wait] [--timeout S] [--apply] [--json]
apex-router mesh jobs [--json]                       # sender: offered jobs and their state
apex-router mesh worker [--daily-cap N] [--wall-min N] [--poll-s N] [--runner claude|codex]  # foreground
apex-router mesh worker --pause | --resume | --status
```

`mesh up --role worker` advertises the role; `mesh pick worker` keeps choosing the least-loaded worker.

## Security model

| Threat | Effect | Mitigation |
|---|---|---|
| Any mesh member can submit | It runs an agent with edit and bash on the worker | Trust = mesh membership. Rotate the secret before the worker goes live. Repo allowlist, caps, pause switch. |
| Shared HMAC key | `from` can be forged by any member | Known limit. Per-node keys need Ed25519 (no stdlib); out of scope for this version. |
| Prompt injection in the task or the repo | The agent tries to leak or push | Deny rules (best-effort), no push, result is a patch the sender reviews before `--apply`. |
| Replay of an old job | Runs twice | Seen-ledger + `expires` ≤ 1 h. |
| Huge job or result | Memory or disk use | Size caps; the v1 1 MiB response cap. |
| Plain HTTP on the LAN | A host on the LAN reads task text and patches | Known v1 limit. Do not send secrets in a task. |
| Runaway cost | Escalation to Opus max on every job | Default ceiling `high`; daily cap; pressure gate; every step logged with cost. |

## Testing

- Unit: job envelope encode/decode, expiry, seen-ledger, each worker check (one rejecting test each),
  ladder order and ceiling, result size caps, patch round trip.
- Fake runner: a `claude` stand-in script that commits a file and prints DONE or BLOCKED by task
  text; a check command that passes only at a given step. Test that the ladder stops at the first pass
  and at the ceiling.
- Two nodes on 127.0.0.1 (two mesh homes, distinct ports): submit → worker takes → result → apply.
- Negative: wrong `to`, expired job, unknown repo, bad ref, paused worker, RED pressure, second job
  while one runs, replayed id.
- No test runs the real `claude` or touches the live mesh home (tests/conftest.py sandbox).

## Out of scope

Encryption on the wire, per-node keys, more than one job at a time, bundle upload of unpushed code,
a launchd agent for `mesh worker` (tmux first; a plist later after a week of use).

## Implementation notes (2026-10-18)

- **Crash recovery fails closed.** A worker crash can leave a process of the job running (the
  agent, a shell it started, the check). Node does not pass an inherited fd to its children, so the
  worker cannot prove that every such process is gone, and killing by pid or cwd risks unrelated
  processes. So at start the worker marks the stale job `failed` (`worker_crashed`), KEEPS
  `worker/running.json`, and rejects every new job as `busy`. Run `apex-router mesh worker --recover`
  after you make sure no process of the job runs (the command lists processes whose cwd is in the
  job folder). It also refuses while the agent lock (`worker/agent.lock`, inherited by the direct
  step processes) is held.
- **Results response.** `GET /mesh/v1/results?for=<sender>` fits a 900 KiB budget; results that do
  not fit come back as stubs (`"truncated": true`, no patch/report). `?for=<sender>&id=<job id>`
  returns that one result in full. `mesh result ID` always asks by id, and never caches a stub.
  The sender response cap is 2 MiB.
- **Tokens and cost** come from `claude --output-format json`; the prompt goes to `claude` on stdin.
- **Worktree location (fix 2026-10-18).** `<mesh home>-jobs/<id>/<step>`, a sibling of the mesh
  home. Before, `worker/jobs/` sat inside the denied mesh home: the agent could run commands but not
  read its own files, so each step ended BLOCKED and the ladder escalated to Opus for nothing.
- **Ctrl-C / SIGTERM** ends the running job as `failed` (`interrupted`) with no further ladder step.
  Killing only the `claude` process makes the step fail, and the ladder goes on; stop the worker
  (or `--pause` and wait) instead.
- **Watch a job:** the agent transcripts are in `~/.claude/projects/*mesh-worker-jobs-<id>-<step>/`
  on the worker (never sent to the sender).

## Codex runner (2026-10-17)

A GPT-only node runs the same jobs with Codex CLI (`codex exec`). The job envelope does not change.
The worker node chooses the runner. A codex node never runs Claude.

Config in `worker.json` (read once when the worker starts):

| Key | Values | Default |
|---|---|---|
| `runner` | `"claude"` or `"codex"` | `"claude"` |
| `codex_search` | `true` or `false` | `false` |
| `codex_models` | `{step: [model, effort]}`, effort `minimal`, `low`, `medium`, `high` or `xhigh` | table below |

`mesh worker --runner claude|codex` overrides `runner`. A bad value stops the worker (exit 2).
`mesh worker --status` shows `runner` (and the codex values on a codex node).

Default ladder for codex:

| Step | Model | `model_reasoning_effort` |
|---|---|---|
| `medium` | `gpt-5.6-terra` | `medium` |
| `high` | `gpt-5.6-terra` | `high` |
| `xhigh` | `gpt-5.6-sol` | `high` |
| `max` | `gpt-5.6-sol` | `xhigh` |
| `opus` | `gpt-5.6-sol` | `xhigh` |

Each step runs, with the prompt on stdin:

```
codex exec -m MODEL -c model_reasoning_effort="EFFORT" -c web_search="disabled|live"
  -c sandbox_workspace_write.network_access=false -s workspace-write -C <worktree>
  --add-dir <repo>/.git/worktrees/<name> --add-dir <repo>/.git/objects
  --ephemeral --ignore-rules --json -o <job dir>/<step>.last -
```

- `web_search="live"` only when `codex_search` is true. Codex defaults to `"cached"`, so the
  worker sets `"disabled"` explicitly.
- A git worktree keeps its index and HEAD in `<repo>/.git/worktrees/<name>` and its objects in
  `<repo>/.git/objects`. The sandbox blocks writes there, so `git commit` fails. The two
  `--add-dir` values make only these dirs writable. The rest of `.git` stays read-only.
- `--ignore-rules`: user execpolicy rules cannot approve a command outside the sandbox.
- The worker never uses `--dangerously-bypass-approvals-and-sandbox` or `--approve-for-me`.
- No `worker-settings.json`: that file is for Claude only. A bad settings file does not reject
  codex jobs.
- Report: the `-o` file (if empty, the last `agent_message` event), capped like the Claude report.
- Pass rules are the same as Claude: no check means the report must end with DONE; with a check,
  the check must pass. The agent fails when the exit code is not 0 or the last turn event is not
  `turn.completed`.
- Tokens: the sum of `turn.completed` `usage` (`input_tokens`, `output_tokens`,
  `cached_input_tokens`). `cost_usd` is null (subscription).
- Reason codes: `agent_error` and `agent_failed` for codex. Claude keeps `claude_error` and
  `claude_failed`.
- `worker/steps.jsonl`, `running.json`, the `start` log row and the OffloadRecord record `runner`.

WARNING: the `workspace-write` sandbox blocks network and writes outside the worktree. It does not
block reads. A codex agent can read `~/.ssh` or `mesh.key` and commit a copy into the patch. This
is the same trust boundary as the Claude runner (its Bash rule is best-effort). A Codex permission
profile with `"deny"` entries blocks these reads; it is not used yet.
