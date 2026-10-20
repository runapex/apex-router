# Runbook: review gate (`apex-router review-gate`)

The gate is advisory. It does not block any git command. It exits with code 0 after `check` and `report`.

## What the gate does

1. The gate reads review records for a loop.
2. The gate returns `ACCEPT` or `HOLD` for the latest revision.
3. The gate shows the reason codes for a `HOLD`.

## ACCEPT rules

`ACCEPT` needs all of these:

| # | Rule |
|---|------|
| 1 | One `GO` from a reviewer with a vendor that differs from the author vendor. |
| 2 | That reviewer has code access recorded (`--code-access`). |
| 3 | That reviewer is not cheap (not local, not haiku, mini, nano, flash, lite, small). |
| 4 | No reviewer has an open `NO-GO` on that revision. |
| 5 | Tests are recorded as passed for that revision. |
| 6 | Each confirmed finding of an earlier revision has a regression test recorded. |

`GO-WITH-CHANGES` is not a `GO`.
A cheap or same-vendor `GO` alone gives `HOLD`.
An unknown author vendor gives `HOLD`. Use `--author` to set it.

## Vendor from model name

| Model name contains | Vendor |
|---|---|
| `claude`, `opus`, `sonnet`, `haiku` | anthropic |
| `gpt`, `codex` | openai |
| `qwen`, `ornith` | local |
| none of these | unknown |

## Record a review loop

1. Record each review: `apex-router review-gate record --loop ID --rev N --reviewer MODEL --verdict GO|NO-GO|GO-WITH-CHANGES --author MODEL [--code-access] [--confirmed K] [--rejected K]`
2. Record the test result: `apex-router review-gate tests --loop ID --rev N --passed` (or `--failed`).
3. For each confirmed finding, record its test: `apex-router review-gate regress --loop ID --finding F --test NAME`
4. Check the loop: `apex-router review-gate check --loop ID [--json]`
5. After a `GO`, record defects found later: `apex-router review-gate post-go --loop ID --defects K`
6. Show all loops: `apex-router review-gate report [--json]`

`K` is a count (`3`) or a list of finding ids (`F1,F2`).
Use finding ids for `--confirmed`. Then `check` names each finding that has no regression test.
With a count, `check` only compares counts.

## Reason codes

| Code | Meaning | Action |
|---|---|---|
| `no_records` | The loop has no rows. | Record a review. |
| `no_review` | The revision has tests but no review. | Record a review. |
| `no_go` | No reviewer gave `GO`. | Fix the findings. Review again. |
| `no_qualified_go` | A `GO` exists, but it does not count. | Read the next code. |
| `same_vendor_go_only` | The `GO` reviewer has the author vendor. | Ask a reviewer of another vendor. |
| `cheap_go_only` | The `GO` reviewer is a cheap model. | Ask a stronger reviewer. |
| `no_code_access` | The `GO` reviewer did not read the code. | Ask for a code review. Add `--code-access`. |
| `reviewer_vendor_unknown` | The model name has no known vendor. | Use a known model name. |
| `author_unknown` | The author vendor is not recorded. | Record with `--author`. |
| `open_no_go` | A reviewer gave `NO-GO` on this revision. | Fix. Make a new revision. |
| `tests_missing` / `tests_failed` | No passed test run for this revision. | Run the tests. Record the result. |
| `regression_missing` | A confirmed finding has no regression test. | Add the test. Record it with `regress`. |

## Storage

| Item | Value |
|---|---|
| File | `~/.apex-router/labels/review_loops.jsonl` (`APEX_HOME` is respected) |
| Content | Ids, enums and counts. No text. |
| Write | Append only. An equal row is not written twice. |
| Read | For each slot, the latest row wins. |

A slot is: review = loop + rev + reviewer. tests = loop + rev. regress = loop + finding + test. post-go = loop.
The `post-go` value is a total. A new value replaces the old value.
The module rejects free text. Ids use letters, digits and `. _ : / -` only.

## Report

The report shows, for each loop: revisions, the revision that first reached `ACCEPT`, confirmed and rejected findings, and post-GO defects.
The report shows, for each reviewer model: GO rate and precision.
Precision = confirmed / (confirmed + rejected) findings of that reviewer.
