# RUNBOOK: review-preread (local "System 1" pre-read for a heavy reviewer)

`apex-router review-preread` has the local Ornith tier (from `~/.apex-router/ornith.env`) read a
unified diff and list **claims to verify**. An independent heavy reviewer then confirms or refutes
each claim. The pre-read comes from a different producer than the code's author, so handing it to
the reviewer does not leak the producer's reasoning. It is advisory only: local review precision
has measured at about 1 in 5, so the reviewer still reviews the whole diff.

## Command

```bash
git diff HEAD~1 | apex-router review-preread                       # JSON to stdout (stdin diff)
apex-router review-preread change.diff --requirements req.md --max-findings 8
apex-router review-preread change.diff --markdown                  # "Claims to verify" list
#   --telemetry PATH | --no-telemetry   (default sink: ~/.apex/offload_telemetry.jsonl)
```

| Exit | Meaning |
|------|---------|
| 0 | The model answered and its answer parsed. `findings` may be empty; zero findings is a valid answer. |
| 2 | The diff (or `--requirements` file) was empty or unreadable. Nothing is printed to stdout. |
| 3 | The model call failed (including failing to construct the local client) or the answer did not parse. The output is still printed, with `findings: []` plus `error` or `parse_error`. |

Callers must treat 3 as "no pre-read", not as "no findings". On the 35B-A3B tier a 4-hunk diff
took about 34 s and used about 1.0k completion tokens.

## Output (`schema: review-preread/1`)

`model, diff_sha256, n_hunks, findings[{id, file, line_hint, severity: low|med|high, claim, why,
how_to_verify}], injection_markers[{file, line_hint, excerpt}], elapsed_ms, prompt_tokens,
completion_tokens`, plus diagnostics:
`n_findings_raw`, `n_stripped_instructions`, `truncated_findings`, `finish_reason`, and
`diff_cut_at_chars` when the diff exceeds 60k characters.

The diff is treated as untrusted data. It sits between nonce delimiters and the model is told to
ignore any instructions inside it. The model is not asked to report injection attempts. Instead,
before the model call, the diff is scanned deterministically for text aimed at a reviewer or
model. Each hit appears in `injection_markers` (excerpt of at most 80 characters, with the
new-file line where known) and in a markdown section headed "Injection markers in diff (verify
by hand)".

The same narrow matcher drops findings whose own text is aimed at the reader. It matches only
these phrases: "ignore (all/the) previous/prior/above instructions", "approve this/the
PR/pull request/change/diff", "LGTM", "system prompt", "do not flag/report", and "you/reviewer
must/should approve/accept/ignore/skip". Ordinary defect wording such as "the model should be
loaded first" or "the flag can skip the checks" is kept. Dropped findings are counted in
`n_stripped_instructions`, and the markdown footer shows both `n_findings_raw` and
`n_stripped_instructions`.

## Handing it to a reviewer

Pass the `--markdown` output next to the artifact, requirements and evidence, as one more piece
of evidence and never as a verdict. Ask the reviewer to return, for each `P<n>`, one of
`confirmed`, `refuted` or `unclear` with a one-line reason, and to list separately any defects
they found that are not in the list. Keep the JSON (keyed by `diff_sha256`) for scoring.

## Measuring recall (the only number that can justify the cost)

For each reviewed diff, let R be the set of defects the reviewer confirmed (P-items marked
`confirmed` plus their own extra findings):

- **pre-read recall** = |confirmed P-items| / |R|. This is the share of real defects the
  pre-read surfaced.
- **pre-read precision** = |confirmed P-items| / |P-items|. The historical baseline is about 0.2.
- **lift**: on a held-out set of diffs, run the reviewer with and without the list (blind it to
  which arm it is in) and compare |R|. Recall shows that the pre-read overlaps the reviewer.
  Only lift shows that it adds defects the reviewer would otherwise miss.

Cost comes from the telemetry rows (`lane="preread"`, `purpose="preread"`, `n_findings`). They
have their own lane, so they never change the queue review lane's numbers. Otherwise they are
booked like that lane (`gated=false`, `escalated=true`), so `offload_report`, `cache_report` and
nightly economics count them as pure cost (`escalated_completion_tokens`) until the lift
measurement shows a benefit. `ok=true` means the model answered and the answer parsed, even with
zero findings. `ok=false` means a call or parse failure.
