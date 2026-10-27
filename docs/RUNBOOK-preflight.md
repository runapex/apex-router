# Runbook: preflight gates

## agent_spawn gates (owner order 2026-10-27)

The pi tool `agent_spawn` (`integrations/pi/apex-agents.ts`) runs these gates before it starts a job.
The gate code is in `integrations/pi/lib/agents.ts` (`spawnGate`). Tests: `integrations/pi/tests/gates.test.ts`.

### Gates and thresholds

| Gate | Applies to | Refused when | Refusal names |
|---|---|---|---|
| Pending branches | agent `worker`, `coordinator` | 3 or more pending branches in the repo of the job cwd | the branches |
| Open decisions | agent `worker`, `coordinator` | 5 or more open rows in `~/.apex-router/decisions.jsonl` | id, topic, text |
| Review rounds | kind `xval` | 3 or more earlier xval jobs on the same repo and branch whose `result.md` ends in `NO-GO` | the job ids, and "send the open finding to the owner as one question" |

`scout`, `reviewer` and `xval` are never refused by the branch and decision gates.

A branch counts (it waits for review or merge) when it has all of these:

1. The name is `apexjob/*`.
2. It has a live worktree (the folder exists and git does not mark it `prunable`).
3. It has at least 1 commit that is not in the base branch (`main`, else `master`).
4. Its last commit is less than 7 days old.

An old `apexjob/*` branch without a worktree does not count.

### Evidence rule

Every xval task gets this line at the end:

`A P1 counts only with evidence: a line number and a concrete input or a failing test. A GO lists the claims you checked.`

An xval job records `repo` (git common dir), `branch` and `head` of its cwd in `meta.json`.

### Override

1. Give `override` to `agent_spawn` with a reason of 10 characters or more.
2. The override passes one spawn only.
3. The reason goes into `meta.json` as `gate_override` (max 300 characters).
4. A shorter reason does not pass.

### Owner decisions

| Command | Result |
|---|---|
| `apex-router decisions add TEXT [--topic T]` | Adds an open row. Prints the id. |
| `apex-router decisions close ID` | Adds a closed row for that id. |
| `apex-router decisions list [--all] [--json]` | Shows the open rows (`--all`: also closed). |

Row: `{id, ts, topic, text, state}`. The file is append-only. The latest row per id wins.
Write only the owner question in TEXT. Do not write task text or secrets.

### Failures and limits

| Item | Value |
|---|---|
| Time budget for all gates | 800 ms; every git call has a timeout from this budget |
| Failed check (git error, timeout, bad file) | The spawn is allowed. The result shows one warning line. |
| Bad row in `decisions.jsonl` | Skipped and counted in one warning line. Never fatal. |
| Job scan | Newest 2000 job names; `meta.json` max 256 KiB; last 4 KiB of `result.md` |
| `decisions.jsonl` read | Last 1 MiB |
