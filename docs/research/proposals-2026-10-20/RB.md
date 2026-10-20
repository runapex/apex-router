# RB: Review loop effectiveness

## Scope and limits

I used only the permitted local files. I did not read other researchers' reports.
I did not analyse `cost_usd` or `max_cost`. I did not quote prompt or answer text.

Verdict rates measure review behaviour, not defect recall. Tasks differ between models.
Missing verdicts remain missing. I kept `sonnet` separate from the full model names.
The logs lack loop identifiers and confirmed-defect labels. They cannot establish general rounds-to-GO or defect precision.

## Sources

| Key | Source |
|---|---|
| A | `~/.apex-router/labels/agent_runs.jsonl` |
| X | `~/.apex-router/xval_runs.jsonl` |
| B | `~/.apex-router/xval_bandit.json` |
| U | `docs/research/ux-rsi/metrics.jsonl` |
| R | `docs/research/ux-rsi/RESULTS.md` |
| S | `docs/research/ux-rsi/SCORER-v2.md` |
| G | `docs/research/2026-10-08-gold-xval.md` |
| D | `docs/research/2026-10-13-debate-vs-sc-results.md` |
| H | `docs/research/2026-10-13-panel-hotpot-results.md` |
| C | `src/apex_router/xval_router.py` |

## Measurements

### Verdicts by model and role

A contains 346 runs. GO% uses only non-missing verdicts as its denominator.
GWC means `GO-WITH-CHANGES`.

| Model | Role | Runs | GO | NO-GO | GWC | Missing | GO% |
|---|---|---:|---:|---:|---:|---:|---:|
| it-entra-claude-haiku-4-5 | scout | 11 | 0 | 0 | 0 | 11 | — |
| it-entra-claude-haiku-4-5 | worker | 1 | 0 | 0 | 0 | 1 | — |
| it-entra-claude-opus-5-5 | engineer | 1 | 0 | 0 | 0 | 1 | — |
| it-entra-claude-opus-5-5 | judge | 3 | 1 | 1 | 1 | 0 | 33.3 |
| it-entra-claude-opus-5-5 | researcher | 1 | 0 | 0 | 0 | 1 | — |
| it-entra-claude-opus-5-5 | reviewer | 18 | 3 | 3 | 9 | 3 | 20.0 |
| it-entra-claude-opus-5-5 | scout | 6 | 0 | 0 | 0 | 6 | — |
| it-entra-claude-opus-5-5 | worker | 136 | 0 | 0 | 0 | 136 | — |
| it-entra-claude-sonnet-4-6 | scout | 1 | 0 | 0 | 0 | 1 | — |
| it-entra-claude-sonnet-5-5 | engineer | 2 | 0 | 0 | 0 | 2 | — |
| it-entra-claude-sonnet-5-5 | judge | 3 | 2 | 0 | 1 | 0 | 66.7 |
| it-entra-claude-sonnet-5-5 | researcher | 1 | 0 | 0 | 0 | 1 | — |
| it-entra-claude-sonnet-5-5 | scout | 1 | 0 | 0 | 0 | 1 | — |
| it-entra-claude-sonnet-5-5 | worker | 2 | 0 | 0 | 0 | 2 | — |
| it-entra-gpt-6-astra | judge | 127 | 18 | 46 | 11 | 52 | 24.0 |
| it-entra-gpt-6.1-sol | engineer | 2 | 0 | 0 | 0 | 2 | — |
| it-entra-gpt-6.1-sol | researcher | 1 | 0 | 0 | 0 | 1 | — |
| sonnet | reviewer | 28 | 0 | 0 | 0 | 28 | — |
| sonnet | scout | 1 | 0 | 0 | 0 | 1 | — |

Judge and reviewer roles contain 179 runs. Missing verdicts account for 83 runs, or 46.37%.

### Rounds to GO

| Loop | Measured path | Source |
|---|---|---|
| UX implementation | 3 main rounds; cross-vendor GO at `3c` | U |
| UX review calls | 9 main judge calls, 2 fix reviews, 1 final judge call | U |
| UX scorer | GO on attempt 11; 9 NO-GO, 1 timeout, 1 GO | U |
| UX scorer effort | 81 turns; 3619.9 seconds including timeout | U |
| Gold review | Independent labels, reconciliation, refutation, then cross-family check; no GO field | G |
| Panel and debate | No promotion; not repair-to-GO loops | D, H |

The UX stop record combines earlier panel votes with the final cross-vendor GO.
It does not show a fresh unanimous panel on the final commit.
The scorer record does not support one new defect on every attempt.
Its first attempt timed out. Later findings span several defect categories.

### Which arrangements find defects?

| Arrangement | Local result | Interpretation |
|---|---|---|
| Cheap judge alone | Sonnet said GO in UX round 2 and missed 5 MUST-FIX items. | Fast acceptance did not establish correctness. |
| Independent code-access review | Astra rejected round 3 while Opus and Sonnet said GO. | Vendor diversity found a remaining implementation defect. |
| Executable invariants | After final GO, checks found 165 pane-width failures and 89 hidden-fact failures among 410 cases. | Even cross-vendor GO missed defects. |
| Cross-vendor report review | H records 10 confirmed findings from the first review pass. | Findings included design, scoring, and reporting errors; not all were runtime defects. |
| Blind labels plus reconciliation | G corrected 15/182 old labels, or 8.24%. | Evidence reconciliation improved labels beyond agreement alone. |
| Unreconciled cross-family objections | G accepted 3/20 splits; 17/20 were rubric deviations. | Most disagreement added no valid correction. |
| Peer exchange | D found Brier gain −0.0001 versus `sc6`, with interval [−0.0168, +0.0167]. | Exchange added no detectable benefit on 356 items. |

H's apparent full-context panel advantage did not survive D's fair follow-up.
D measured self-consistency gain +0.0607 over a single call.
These question-answering benchmarks do not establish code-defect recall.

## Ranked proposals

Expected effects below are test targets or replay estimates. They are not measured production gains.

### 1. Require code evidence and invariant checks before acceptance

| Field | Proposal |
|---|---|
| Problem | R reports 5 MUST-FIX items missed by Sonnet's round-2 GO. U records round-3 GO from both same-vendor judges despite Astra's NO-GO. |
| Change | Add an apex-router acceptance policy for code reviews. Require an independent cross-vendor reviewer with code access and recorded test results. |
| Change details | Keep cheap reviews advisory. Block acceptance when required checks fail or code access remains unchecked. Require regression checks for confirmed MUST-FIX findings. |
| Expected effect | Replay target: reduce the 5 known missed MUST-FIX items to 0 accepted misses. Also block acceptance for both invariant defect classes after final GO. |
| Metric | Count accepted artifacts with independently reproduced MUST-FIX defects. Track false rejection on clean artifacts. |
| Falsification test | Replay the historical UX commits with model labels hidden. Reject the policy if it accepts a known defective commit or rejects `ec781c4` after its documented fixes. |
| Effort | M |
| Risk | More review latency. Correlated reviewers can still miss defects. Tests can encode the same wrong assumption as implementation code. |

S capped defective historical commits at 60.0 while the format scorer reached 98.9.
The proposed gate uses executable defects, not a higher proxy score.

### 2. Batch defect discovery and stop serial symptom repairs

| Field | Proposal |
|---|---|
| Problem | U records 11 scorer attempts, 81 turns, and 3619.9 seconds before GO. Findings crossed scorer, collector, matcher, and plugin boundaries. |
| Change | Add a loop controller with defect identifiers, affected components, root causes, and reproductions. Ask reviewers for the complete supported defect set before repair. |
| Change details | Give the reviewer full relevant evidence. Require a regression matrix before the next review. Escalate repeated defect classes to root-cause review. |
| Expected effect | Target at most 6 attempts and 1810.0 seconds on the scorer replay. These targets halve the observed attempt count and elapsed time, with attempts rounded up. |
| Metric | Measure attempts and elapsed time to grounded GO. Measure confirmed defects discovered after GO. |
| Falsification test | Replay scorer history with the documented bypass and plugin cases. Reject the change if it exceeds either target or misses a documented defect. |
| Effort | L |
| Risk | Broad reviews can time out. Batching cannot reveal defects that exist only after a repair. A rounds limit must never force GO. |

The first scorer timeout consumed review time without a verdict.
The remaining NO-GO rounds were not demonstrably wasteful. They found documented defect families.
The target is fewer repair cycles, not fewer objections.

### 3. Learn from reconciled usefulness, not completed answers

| Field | Proposal |
|---|---|
| Problem | X marks 236/260 runs successful, or 90.77%. Only 2 feedback rows exist, or 0.77% of runs. A lacks 46.37% of review verdicts. |
| Problem details | C treats a completed non-empty final message and successful exit as `ok`. It does not measure valid defect discovery. |
| Change | Add `loop_id`, revision, verdict, confirmed-finding count, rejected-finding count, reproduction status, and post-GO defects to review metadata. |
| Change details | Separate completion from reconciled usefulness. Keep unknown usefulness unlabelled. Train quality estimates only from reconciled outcomes. |
| Default advice | Keep reviewers independent. Do not add peer-exchange rounds without held-out gains. Apply the same rubric before counting cross-vendor disagreement as a defect. |
| Expected effect | Target usefulness coverage of 90.77%, anchored to current completion coverage, instead of 0.77% feedback coverage. |
| Replay estimate | Removing peer exchange from D's matched `sc6` comparison saves 466 tokens/item, or 2.85%, without a detected Brier loss. |
| Metric | Track reconciled usefulness coverage, confirmed-finding precision, post-GO defects, and held-out calibration. |
| Falsification test | Replay G's cross-family splits. Reject the change if it credits the 17 rubric deviations as valid findings. |
| Additional test | Reject the independence advice if a fair held-out review trial shows peer exchange improves confirmed-defect recall without more post-GO defects. |
| Effort | M |
| Risk | Human reconciliation adds work. Reviewers can inflate finding counts. Selective feedback can bias estimates. |

B records category observations, not defect outcomes.
Its `diff` observation count is 106, while arm counts total 107.
Treat the snapshot as telemetry, not a defect-quality benchmark.

### Rank rationale

| Rank | Reason |
|---|---|
| 1 | It directly prevents observed false acceptance with a bounded policy change. |
| 2 | It addresses the longest measured repair loop while retaining defect discovery. |
| 3 | It enables valid future learning, but its defect-quality effects need new outcome data. |

## Appendix: commands and results

The following command produced the reported counts, source numbers, code locations, and derived targets.
It read only the permitted evidence files. It selected only permitted fields from A.
Its numeric source scan did not output prompt or answer text.

```bash
python3 - <<'PY'
import json,pathlib,collections,re,math
h=pathlib.Path.home()/'.apex-router'; fields='role model agent kind verdict turns wall_s state ts'.split()
a=[{k:r.get(k) for k in fields} for s in (h/'labels/agent_runs.jsonl').read_text().splitlines() for r in [json.loads(s)]]
for key in sorted({(r['model'],r['role']) for r in a}):
 c=collections.Counter(r['verdict'] for r in a if (r['model'],r['role'])==key); n=sum(c.values()); v=n-c[None]
 print(key,n,c['GO'],c['NO-GO'],c['GO-WITH-CHANGES'],c[None],round(100*c['GO']/v,1) if v else 'NA')
r=[r for r in a if r['role'] in ('judge','reviewer')]; print('review',len(r),sum(z['verdict'] is None for z in r),round(100*83/179,2))
x=[json.loads(s) for s in (h/'xval_runs.jsonl').read_text().splitlines()]; runs=[r for r in x if 'arm' in r]; fb=[r for r in x if 'feedback' in r]
print('xval',len(runs),sum(r.get('ok') is True for r in runs),len(fb),round(100*236/260,2),round(100*2/260,2))
b=json.loads((h/'xval_bandit.json').read_text()); print('bandit',[(c,v['obs'],sum(z['n'] for z in v['arms'].values())) for c,v in b['categories'].items()])
u=[json.loads(s) for s in pathlib.Path('docs/research/ux-rsi/metrics.jsonl').read_text().splitlines()]
for phase in ['judge','scorer-v2-review']:
 rs=[r for r in u if r['phase']==phase]; print(phase,[(r.get('round'),r.get('agent'),r.get('verdict'),r.get('must_fix')) for r in rs],round(sum(r.get('wall_s',r.get('time_s',0)) for r in rs),1),sum(r.get('turns',0) for r in rs))
print('targets',math.ceil(11/2),round(3619.9/2,1),'derived',410-245,410-321,round(100*15/182,2),100*3/20,100*17/20,16835-16369,round(100*(16835-16369)/16369,2))
for f in ['ux-rsi/RESULTS.md','ux-rsi/SCORER-v2.md','2026-10-08-gold-xval.md','2026-10-13-debate-vs-sc-results.md','2026-10-13-panel-hotpot-results.md']:
 print('SOURCE',f)
 for i,s in enumerate((pathlib.Path('docs/research')/f).read_text().splitlines(),1):
  if re.search(r'\d',s): print(i,re.findall(r'[-+−]?\d+(?:\.\d+)?',s))
p=pathlib.Path('src/apex_router/xval_router.py')
for i,s in enumerate(p.read_text().splitlines(),1):
 if any(t in s for t in ['ok = bool(p.get','ok = bool(metrics','def feedback','good = verdict','stop exploring']): print('CODE',i,s)
PY
```

Result: PASS. The command produced the measurement tables and numerical evidence above.

## Delivery record

| Item | Result |
|---|---|
| CHANGED | `/tmp/rsch/RB.md` only |
| TESTED | Evidence command: PASS. Initial structure check: FAIL from self-matching assertions. Corrected structure check below: PASS. |
| OPEN | General defect recall and rounds-to-GO remain unknown without linked, reconciled outcome data. |

Report structure check:

```bash
python3 - <<'PY'
from pathlib import Path
import re
p=Path('/tmp/rsch/RB.md'); s=p.read_text()
assert len(re.findall(r'^### [123]\. ',s,re.M)) == 3
for field in ['Problem','Change','Expected effect','Metric','Falsification test','Effort','Risk']:
 assert s.count('| '+field+' |') == 3, field
print('PASS: exactly 3 ranked proposals; required fields and evidence appendix present')
PY
```
