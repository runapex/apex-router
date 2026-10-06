# Zeno frontier: where the last bit of reliability goes

**As of 2026-10-06 · local-machine study · `apex-router zeno report`**

Zeno: to arrive you cover half the distance, then half of what is left, forever. Model progress
often looks like that — each generation closes a fraction of the remaining gap,

```
gap_{n+1} = alpha · gap_n,   0 < alpha < 1        (gap converges: sum = gap_0 / (1 - alpha))
C_{n+1}   > C_n                                    (cost need not converge at all)
```

`src/apex_router/zeno.py` turns this into three measurable lenses over this machine's model runs.

| Lens | Question | Function | Number it gives |
|---|---|---|---|
| Engineering Zeno | Does each extra nine cost more than the last? | `frontier()`, `ladder()` | alpha per segment, elasticity d ln(fail)/d ln(cost), **decades of cost per nine** |
| Horizon Zeno | How do per-step failures compound over a long task? | `horizon()`, `compounding()` | p^n table, max steps at a target, observed clean sessions vs p^n |
| Epistemic Zeno | How much of the failure space are we not measuring? | `discovery()`, `coverage()` | Good-Turing P(next failure is a new kind), Chao1 kinds, unlabeled share, per-stratum Wilson bounds vs the aggregate |

**Scope.** The proxy records whether a call *finished* (transport/upstream faults); xval records
whether a review *reached a verdict*. Neither is answer correctness. They are reliability floors
under quality, and the report says so on its first line.

## The worked example (engineering)

"10× compute → 30% fewer failures, another 10× → 15% fewer, another 10× → 7% fewer":

```
apex-router zeno frontier 1:1 10:0.7 100:0.595 1000:0.55335
alpha 0.70 → 0.85 → 0.93;  decades of cost per nine 6.5 → 14.2 → 31.7;  verdict: zeno
```

Decades-per-nine = log10(cost ratio) / −log10(failure ratio): how many 10× of cost one more 10× cut
in failures takes at the local rate. A constant power law gives a flat value ("steady"); rising
values are the Zeno signature. A zero observed failure rate is a bound (Wilson upper), never a point
on the curve.

## What the live data said (2026-09-30 → 2026-10-06, 6,824 proxy requests)

**Horizon.** Per-call completion p = 96.8% (1.5 nines). At that p, 10 calls finish clean 72% of the
time, 100 calls 3.9%; only 3 calls stay above 90%; 100 calls need p ≥ 99.895%. Sessions carrying a
session id (claude-code; codex rows mostly lack one) have their own p = 99.6%. Against p^n:

| calls/session | sessions | observed clean | predicted p^n |
|---|---|---|---|
| 1–10 | 55 | 100% | 99% |
| 11–50 | 7 | 100% | 87% |
| 51–200 | 6 | 83% | 73% |
| 201+ | 5 | **20%** | 33% |

Short sessions beat independence; the longest do worse than it. One p with independent steps does
not describe these sessions (clustering, per-session p, or outcome-dependent length — the ratio is a
model check, not a cause). Samples per long bucket are small (Wilson 4–62% for 201+).

**Epistemic — the measurement blind spot.** 218 failed calls; **56% had no recorded cause**. Five
named kinds (ConnectError 67, ReadError 23, ReadTimeout 4, WriteTimeout 1, ConnectTimeout 1),
Good-Turing P(next is new) ≈ 2%, Chao1 ≈ 6 kinds. The unnamed half was the largest failure class.
Cause: both proxy handlers set `is_error` when a stream broke *after* the response began, but never
an `error_cause` (only the upstream-raise path and `http_<status>` were labeled). The doctor files
them under `unlabeled(pre-v5)` though they are v6–v9 rows.

**Fixed in this change:** such rows now get `error_cause = midstream_<Exception>`, set in `finally`
after any `http_<status>` label so a 429 keeps its label. `pressure._classify` returns `None` for
`midstream_*` — exactly what it returned for those rows before — so pressure levels are unchanged
until the new label has data showing whether these are upstream faults or client cancels.

**Coverage — what the aggregate hides.** Aggregate failure 3.2%. By client: claude-code 0.40%,
codex **4.55%** (Wilson lower 3.99% > aggregate upper 3.65% → "worse"); by context stratum `m` 5.4%
is "worse", `xs`/`s` show 0 failures but only bound to ≤2.0% / ≤3.6%. Caveat: 121 of codex's 209 failures
are the unlabeled mid-stream breaks; its labeled rate alone is ~1.9%. Whether codex is truly less
reliable, or just the client whose streams break, is the first question the new label answers.

**Engineering.** xval has 13 runs over 5 arms (≤5 each, 1 failure): verdict `insufficient` — needs
≥3 arms with ≥20 runs and ≥1 failure. Re-run after 1–2 weeks of xval use.

## Read it again

```
apex-router zeno report [--since-days 7] [--json]
apex-router zeno horizon --p 0.999 --steps 1,10,100
apex-router zeno ladder --alpha 0.5 --growth 10
apex-router zeno frontier cost:fail ...
```

Next measurements: (1) the `midstream_*` breakdown after a week — does it go into pressure as
`transport`, or stay out as client cancels; (2) codex vs claude-code with the labels in; (3) the xval
frontier once arms reach 20 runs; (4) answer-quality labels (datapce v1.1) — the gap this report
cannot see.
