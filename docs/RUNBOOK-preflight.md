# Runbook: pre-analysis before a build (`apex-router preflight`)

Owner order 2026-10-27: do a pre-analysis before every build, so that we do not make a known mistake again.

## What it does

`preflight` reads data this machine already has. It writes nothing.

| Source | What it gives |
|---|---|
| `~/.apex-router/pi-jobs/*/result.md` of xval jobs | P0/P1 findings sorted into 11 fault types, for the given files and for all files |
| `~/.apex-router/pi-jobs/*/meta.json` | earlier jobs on the same files and how they ended; money lost at the cost limit |
| `git log` (60 days) | commits that fix an xval finding, per file |
| `cost_advice` | p75/p90 cost of done jobs for the agent/model/thinking cell |

The output is a brief block: SUCCESS, THREAT SCOPE, KNOWN FAULT TYPES, TESTS TO WRITE FIRST, COST, ROUNDS.
It shows only fault-type names, counts, job ids and repo paths. It never shows finding text.

## Use

1. Run `apex-router preflight --files <paths the change touches> --model <worker model>`.
2. Fill in SUCCESS and THREAT SCOPE before you start the build.
3. Paste the block into the worker brief.
4. Give the same THREAT SCOPE to every review round.
5. After review round 3, send an open finding to the owner as one question.

## Evidence (2026-10-27)

Backtest on 46 earlier reviews with findings: `PYTHONPATH=src python3 scripts/preflight_backtest.py`.
For each review, the script uses only earlier jobs.

| Ranking of the top 6 fault types | Recall of the real fault types |
|---|---|
| 2 x share in these files + share over all files (used) | 75 % |
| share over all files only | 73 % |
| these files, else all files | 64 % |
| random 6 of 11 | 56 % |

The gain over "all files only" is small. Most of the value is the list of fault types itself.
The sample is small. Do not tune the rank order on it again; rotate in new reviews first (RSI R5).

## agent_spawn gates (owner order 2026-10-27)

The pi tool `agent_spawn` (`integrations/pi/apex-agents.ts`) runs these gates before it starts a job.
The gate code is in `integrations/pi/lib/agents.ts` (`spawnGate`). Tests: `integrations/pi/tests/gates.test.ts`.

### Gates and thresholds

| Gate | Applies to | Refused when | Refusal names |
|---|---|---|---|
| Pending branches | agent `worker`, `coordinator` | 3 or more pending branches in the repo of the job cwd | the branches |
| Open decisions | agent `worker`, `coordinator` | 5 or more open rows in `~/.apex-router/decisions.jsonl` | id, topic, text |
| Review rounds | kind `xval` | 3 or more review rounds on the same repo and branch that ended in `NO-GO`. One round = one reviewed commit (HEAD): parallel part-reviews of one commit count once. The count goes back to the newest `GO` on that branch, inside 7 days, and only when the scan is complete | the job ids, and "send the open finding to the owner as one question" |

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
| `decisions.jsonl` read | Last 1 MiB. A cut file gives a warning, never a silent pass. |
| `decisions.jsonl` size | `add` and `close` run under a flock. Above 256 KiB a writer rewrites the file from a full read: every open row, the newest 200 closed rows of 30 days. No rewrite if the full read fails. |
