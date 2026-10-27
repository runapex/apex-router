# pi auto-compaction: precision and cost (research, 2026-10-26)

Status: research. No code in pi or in `integrations/pi` changed. Data: `scripts/compaction_audit.py`
over all sessions in `~/.pi/agent/sessions` (read only).

## 1. Question

1. Does auto-compaction lose user instructions, so the agent seems to "repeat a corrected mistake"?
2. What does compaction cost, and how can apex-router make it cheaper and more precise?

## 2. How pi compacts (source: pi `docs/compaction.md`)

| Item | Value |
|---|---|
| Trigger | `contextTokens > contextWindow - reserveTokens`. Our `reserveTokens` = 49152. |
| Kept raw | Last `keepRecentTokens` (default 20000; not set here). |
| Summary input | Older messages serialized to text. Tool results cut to 2000 chars. Previous summary is passed in. |
| Summary call | Fresh routing session id; prompt-cache writes off. Same model as the session. |
| Summary form | Free text: Goal, Constraints, Progress, Decisions, Next Steps, Critical Context, file lists. |
| Hook | `session_before_compact` can cancel or return a custom summary + `details`. |

## 3. Results (39 compactions, 13 sessions, 2026-10-13 .. 2026-10-27 UTC)

Checked by xval (GPT, 2026-10-27). Its corrections are applied below.

| Metric | Value | Note |
|---|---|---|
| Compactions on Opus 5.5 / Sonnet 5.5 | 38 / 1 | |
| Total cost of summary calls | $195.55 | mean ~$5.0, max $12.94 (recorded estimate, not billing) |
| Input tokens to summary calls | 11.07 M | 154 k - 822 k per event (a split turn makes 2 calls) |
| Cache-read tokens in summary calls | **0** | every call pays full input price |
| User rules matched in summary + kept messages | 49 / 52 (94 %) | word overlap; a paraphrase does not match |
| Ids matched in summary + kept messages (sha, EYE-n, job id, #PR) | 80 / 113 (71 %) | exact string |
| Summary growth per re-compaction | median x1.07, max x1.74 | one session went 16 k -> 44 k chars |
| Tool error rate, 30 results before / after | 2.8 % / 3.5 % | small sample: no conclusion |

Hand check (xval pass 2 + own check):

| Rule (shortened) | Heuristic | Hand check |
|---|---|---|
| `7c1d60cd` "we only fix the sync problem ... shouldn't merge risky changes" | no match | Kept as paraphrase ("user explicitly rejected large or risky changes"). |
| `424e9b02` "merge instructions must be careful and precise when approvals exist" | no match | Kept as paraphrase (scope-lock rules for approved PRs). |
| `f076659c` "run 3 research agents ... majority of votes" | no match | Kept as paraphrase ("3 research agents", "majority votes"). |
| `a1eef098` "every move must be regressed ... no negative impact" | **match** (3/6 words) | **Really absent** at compaction `902e84a3`. Harm not proven: the PR was already merged. |

The heuristic is wrong in both directions: all 3 "no match" rules were kept, and the 1 rule we know
was dropped counted as "match". So the 94 % is not a loss rate. It is a word-overlap rate. A real
loss rate needs a semantic check (P2 with a judge, section 7).

## 4. Answer to the question

1. Compaction **can** cause "repeats a corrected mistake". We observed 1 dropped user rule; we do
   not know the true rate (the heuristic is not reliable, see above). ~29 % of ids were unmatched. A lost "do not" rule looks the same as a model
   that ignores the rule. We found no confirmed repeat in the transcripts.
2. Recall is not enforcement. A rule in the summary does not stop a tool call that breaks it.
3. Cost: the summary call re-reads 154-822 k tokens with no cache. That is ~$5 each time.

## 5. Causes

| # | Cause | Effect |
|---|---|---|
| C1 | User rules pass through an LLM paraphrase each time. | Rules get merged, shortened or dropped. |
| C2 | Ids are free text in the summary. | ~29 % of ids not matched; the agent can search again or use an old id. |
| C3 | Iterative summary of a summary. | Drift and growth (x1.74); old facts crowd out new ones. |
| C4 | Summary call is a new request (serialized text, new session id, `completeSummarization()` forces `cacheRetention: "none"`). | 0 cache reads, ~$5 per compaction. |
| C6 | Critical limits live only in text (prompt, summary). | A forgotten or ignored limit is not blocked. |
| C5 | Tool results cut to 2000 chars in the summary input. | The last error text (the "why it failed") is often cut. |

## 6. Options (ranked by value / risk)

| # | Option | Fixes | Expected gain | Risk |
|---|---|---|---|---|
| P0 | **Enforce critical limits with a `tool_call` hook**, not text: e.g. block `ssh devtest66`, `git push`, Medium/Jira/Slack posts while a "do not post" rule is active. Extends `apex-hangguard`. | C6 | a forgotten rule cannot cause the action | false blocks; rules need an owner and an end |
| P1 | **Pinned ledger**: extract user rules (verbatim, with source entry id), ids, open NO-GO findings and the last failing command + error deterministically. Store in `details`; the extension must render it into the summary text and carry it to the next compaction. | C1, C2, C5 | extracted items always reach the next context | extraction misses; stale or replaced rules stay: each item needs source, scope and a retire rule (superseded / done / `/forget`); cap ~3 k tokens |
| P2 | **Survival gate**: after the summary, check each ledger item and each id is present; append missing ones. Log the miss rate to telemetry. | C1, C2 | measured, not assumed | adds tokens; same limits as P1 |
| P3 | **Cache-prefix summary**: (a) in `before_provider_request`, keep a copy of the last provider payload (provider JSON: system, tools, messages). (b) In `session_before_compact`, call `ctx.modelRegistry.complete(model, <minimal Context>, { onPayload })`; `onPayload` replaces the built payload with the kept copy plus one appended user message "summarize". Same model, same tools, same `cacheRetention` as the session; not `completeSummarization`. Check `usage.cacheRead > 0`, else log a miss. | C4 | model: $196 -> $56 (-71 %) if every prefix hits cache; full miss ~$267 | not guaranteed: cache lifetime, routing, payload rewrites, cache-write premium; the payload copy must be the last one (no stale copy after a model switch); measure on 5 events first |
| P4 | **Fixed summary budget**: re-summarize from ledger + last summary into a fixed token budget, not an ever-growing one. | C3 | stops x1.74 growth | may drop old detail; the ledger keeps the critical part |
| P5 | Cheaper summary model (Sonnet 5.5). | C4 | 5x cheaper input ($3 vs $15 per M) | lower summary quality; only with P1+P2 |
| P6 | Raise `keepRecentTokens` to 40-60 k. | C1, C5 | more raw recent turns | more tokens per turn after compaction |

Recommended order: P0 for the few safety limits (shared boxes, push, public posts). Then P1 + P2 (one
pi extension, `integrations/pi/apex-compact.ts`). Then P3 as a measured experiment, then P4. P5 and
P6 only after P1+P2 measure well.

## 7. How to measure (R1-R5 of the owner rules)

1. Inside gate (must not fall): `scripts/compaction_audit.py` on a replay of the 39 recorded
   `preparation` inputs: rule survival, id survival, summary tokens, cost.
2. Outside score (decides): a judge of another model family (GPT via `xval`) answers probe
   questions from the summary + kept messages only. Gold answers come from the full transcript.
   Probes: "What did the user forbid?", "Which commit/job is current?", "What failed last and why?".
3. Hold out 10 of the 39 events. The extension author must not see them.
4. Outcome: in live use, count user messages that repeat an earlier correction after a compaction.

## 8. Limits of this study

- Survival uses word overlap; a paraphrased rule can count as lost, a shortened one as kept. A hand
  check of 3 "no match" rules found 2 paraphrases and 1 real loss.
- The tool-error numbers (33/1170 vs 40/1134) come from a separate one-off query, not from the script.
- P3 savings are a model (prefix at 10 % of the input price), not a measurement.
- 39 events from one user and 13 sessions. Sonnet 5.5 has 1 event.
- The tool-error-rate change (2.8 % -> 3.5 %) is inside noise.

## 9. Implementation (2026-10-27, `integrations/pi/apex-compact.ts`)

| Part | File | State |
|---|---|---|
| P1 ledger, P2 gate, P4 budget, P6 cut, P3 replay payload (pure) | `integrations/pi/lib/compact.ts` | tests: `compact.test.ts` |
| Order of paths: P3 -> P5 -> P4 -> pi default | `integrations/pi/lib/compactrun.ts` | tests: `compactrun.test.ts` |
| P0 rules, `/guard` command, self-protect of the rules file | `integrations/pi/lib/guardrules.ts` | tests: `guardrules.test.ts` |
| Hooks | `integrations/pi/apex-compact.ts` | `session_before_compact`, `before_provider_request`, `tool_call`, `/guard` |
| Example rules (copy to `~/.apex-router/pi-guardrules.json`) | `integrations/pi/guardrules.example.json` | none active by default |

| Env flag | Default | Effect |
|---|---|---|
| `APEX_COMPACT` | on | `0` turns off all compaction work (pi default) |
| `APEX_COMPACT_SUMMARY_TOKENS` | 6000 | P4 summary budget; the ledger is outside it |
| `APEX_COMPACT_LEDGER_TOKENS` | 3000 | P1 ledger cap |
| `APEX_COMPACT_CACHE` | off | `1` turns on P3 (cache-prefix replay); logs `p3_cache_read` |
| `APEX_COMPACT_CACHE_MAX_AGE_S` | 300 | P3 does not use an older payload copy |
| `APEX_COMPACT_MODEL` | off | P5: `provider/model` writes the summary text |
| `APEX_COMPACT_KEEP_TOKENS` | off | P6: keep more recent raw tokens (max half the window) |
| `APEX_GUARD` | on | `0` turns off P0 |
| `APEX_GUARDRULES_FILE` | `~/.apex-router/pi-guardrules.json` | No file = no rules |

Telemetry: `~/.apex-router/pi-compact/YYYY-MM-DD-<pid>.jsonl` and `~/.apex-router/pi-guard/...` (counts,
rule ids and reason codes only; no message text; mode 0600; 14 days).

Not done yet: P3 is not measured on a live provider. Before `APEX_COMPACT_CACHE=1` is a default,
measure 5 events (`p3_cache_read > 0`, cost per event, summary quality by the probe questions).
Known limits: a false "retire" by word overlap can unpin a rule (use `forget R<n>` labels); the
ledger does not make P0 rules by itself (owner decision); the audit id regex now also matches
14-digit pi job ids (no change: user messages held none).
