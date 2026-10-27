# Runbook: pre-analysis before a build (`apex-router preflight`)

Owner order 2026-10-27: do a pre-analysis before every build, so a known mistake is not made again.

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
Only earlier jobs are used for each review.

| Ranking of the top 6 fault types | Recall of the real fault types |
|---|---|
| 2 x share in these files + share over all files (used) | 75 % |
| share over all files only | 73 % |
| these files, else all files | 64 % |
| random 6 of 11 | 56 % |

The gain over "all files only" is small. Most of the value is the list of fault types itself.
The sample is small. Do not tune the ranking on it again; rotate in new reviews first (RSI R5).
