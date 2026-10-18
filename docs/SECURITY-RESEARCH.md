# Security research and hardening

apex-router is a public, MIT-licensed Claude Code plugin. The security maintainer is Juri Kern ([SECURITY.md](../SECURITY.md)). This page is dated 2026-10-17. Code and test line numbers refer to commit `ce08fb9`.

**R** in this page means the research log [docs/research/2026-10-12-datapce-problem-search.md](research/2026-10-12-datapce-problem-search.md). "R:279" means line 279 of that file.

## Summary

- 9 public coding-agent data-destruction incidents are mapped to candidate checks. An adversarial re-score found about 4 path checks, 3 credential or scope cases and 2 cases that no pre-call check catches. No check was run against the incidents.
- Claude Code's OTel `tool_decision` event does not say which permission source matched. One automatic accept cannot be tied to one rule.
- Published auto-mode miss rates are 17% (vendor, n = 52) and 81% (independent stress test, n = 253). The two studies measure different things.
- The repo ships tested controls: a signed mesh, an outbound-text preflight, same-origin redirect credentials, injection markers, a hygiene test and a secrets canary. Dependency triage with OSV and OpenVEX (track T2) started on 2026-10-17. Two research tracks (T1, T3) are planned. They have no results yet.

## Findings

### 1. Nine incidents and the check each one needed

Source: [Adversa, AI coding agent incidents](https://adversa.ai/blog/ai-coding-agent-incidents/), read 2026-10-12 (R:43-45, R:279-298) and re-read 2026-10-17.

| # | Agent, date | Guardrails | Candidate check (retrospective judgment) | Re-score class (R:448-451) |
|---|---|---|---|---|
| 1 | Cursor YOLO, 2025-06 | off | delete target resolves outside the working tree | path |
| 2 | Replit, 2025-07-18 | freeze instruction ignored | destructive DB operation while a freeze flag is set | credential/scope |
| 3 | Claude Code #10077, 2025-10-21 | on | expanded path set includes `/` or `$HOME` | path |
| 4 | Gemini CLI, 2025-11 | exit code misread | destination exists before each move | not catchable before the call |
| 5 | Antigravity Turbo, 2025-11 | off | resolved target is a drive root or outside the workspace | path |
| 6 | Cursor Plan Mode, 2025-12 | on, mode bug | any shell execution while the mode is plan-only | not catchable before the call |
| 7 | Claude Code, 2025-12-08 | not stated | expanded `rm` argument equals `$HOME` | path |
| 8 | Amazon Kiro, 2025-12 | two-person gate bypassed | production target and fewer than 2 approvals | credential/scope |
| 9 | Claude Code (Opus 5), 2026-07 | autonomous run | reset database URL matches a production pattern | credential/scope |

The first pass called 7 of 9 "resolution" checks (R:293-298). A fresh adversarial reviewer re-scored this to about 4 path checks, 3 credential or scope failures and 2 that no pre-call check catches (R:448-451). For the 3 credential cases, credential scoping is the simpler fix. Claude Code never auto-approves `rm` on critical paths: no allow rule or hook can approve it (R:435-438). The gap is narrower than first claimed.

### 2. The `tool_decision` gap

Source: [Claude Code monitoring docs](https://code.claude.com/docs/en/monitoring-usage), re-read 2026-10-17.

- `source: "config"` covers project settings, personal allow and deny rules, managed policy, CLI flags, the active permission mode, session grants and inherently safe tools.
- The docs state: "The event doesn't indicate which of these sources matched."
- The event has no reason field and no matched-rule field.
- R:507-510 (an agent's reading, 2026-10-12) puts classifier verdicts under `config` too. `tool_use_id` joins a decision to its `tool_result`.

Effect: an org can count automatic accepts. It cannot tell from this event whether a rule, a managed policy or the classifier allowed a given call.

### 3. Auto-mode miss rates

| Source | Dataset | n | Miss rate (FNR) | False-positive rate | Evidence |
|---|---|---|---|---|---|
| [Anthropic engineering post](https://www.anthropic.com/engineering/claude-code-auto-mode) | real overeager actions, curated | 52 | 17% | n/a | R:497, R:1033 |
| same | real internal traffic | 10,000 | n/a | 0.4% | R:496-497 |
| same | synthetic data exfiltration | 1,000 | 5.7% | n/a | R:498 |
| [arXiv 2604.04978v2](https://arxiv.org/abs/2604.04978) | ambiguous scope-escalation actions, all | 253 | 81.0% (95% CI 73.8-87.4) | 19.8% | R:503-505, R:1311-1315 |
| same | actions the classifier evaluates (Tier 3) | 160 | n/a here | 31.9% | R:1034, R:1313 |

Do not compare the rows directly. The arXiv study uses deliberately ambiguous actions and an ex-ante list of authorised targets (R:681). In that study, 93 of 253 actions (36.8%) were in-project edits that the classifier does not see (R:505, R:1315-1316). Real overeager actions are rare in the data: Anthropic curated only 52 over months (R:673-675).

### 4. The maintainer's own transcripts

Source: a script over one machine's Claude Code transcripts, one auto-mode user, 2026-10-12 (R:575-592).

| Quantity | Value |
|---|---|
| session files | 46 |
| Bash tool calls | 3,526 |
| `rm -rf` calls | 41 (relative 28, `/tmp` 7, absolute non-tmp 4) |
| `rm -rf` followed within two rows by a rejection or classifier denial | 10 of 41 |
| absolute targets outside the tree and outside scratch that were not denied | 2 |

An earlier scan found 3 classifier denials in 5,040 tool calls. All 3 were false positives on scratch-directory cleanups that the session itself made (R:263-275). These counts show that the features can be computed from local transcripts. They are not rates.

## Hardening in this repo

| Control | Code | Test |
|---|---|---|
| Signed LAN mesh. HMAC-SHA256 envelope; constant-time MAC compare; 30 s skew limit; replay guard records a nonce only after MAC and skew pass. | [`mesh/protocol.py`](../src/apex_router/mesh/protocol.py):1-5, 27, 61-67, 139-170 | [`test_mesh_core.py`](../tests/test_mesh_core.py):203, 219, 246, 256, 266, 278; [`test_mesh_review_fixes.py`](../tests/test_mesh_review_fixes.py):118, 150 |
| DeepSeek-lane preflight. `check_text` runs before any network call. It refuses secrets, e-mail addresses and overlay deny patterns. A broken deny rule fails closed. | [`remote/deepseek_client.py`](../src/apex_router/remote/deepseek_client.py):28-31, 112, 159 | [`test_remote_deepseek.py`](../tests/test_remote_deepseek.py):71, 136, 174, 227 |
| Redirect credentials. Auth headers follow a redirect only to the same origin (scheme, host, port). Fixed in `ce08fb9`. | [`codeqa/judge.py`](../src/apex_router/codeqa/judge.py):131-160 | [`test_codeqa_judge.py`](../tests/test_codeqa_judge.py):339, 362 |
| No agentic grading of untrusted code. The judge is opt-in and HTTP-only. | [README](../README.md#security-posture) | [`test_codeqa_judge.py`](../tests/test_codeqa_judge.py):269, 296 |
| Injection markers. A deterministic scan flags reader-directed text in a diff before the model call. Instruction-like findings are stripped and counted. | [`ornith/review_preread.py`](../src/apex_router/ornith/review_preread.py):15-21, 106 | [`test_review_preread.py`](../tests/test_review_preread.py):111, 268, 282 |
| Public hygiene. Every tracked file is scanned for banned identifiers (stored only as SHA-256 digests) and personal home paths. | n/a | [`test_public_hygiene.py`](../tests/test_public_hygiene.py) |
| Secrets canary. A sentinel credential must not reach telemetry, the 502 body, logs or stderr, on success or on upstream error. | proxy passthrough and shadow handlers | [`test_secrets_canary.py`](../tests/proxy_engine/test_secrets_canary.py):219, 224, 230 |
| Threat-model note for local model serving (co-resident cache side channel on CPU detokenization). | [`SECURITY-local-serving.md`](SECURITY-local-serving.md) | n/a (design note) |

## Disclosure

- Policy: [SECURITY.md](../SECURITY.md). Report privately with GitHub's "Report a vulnerability" button. Targets: first reply in 3 working days, triage in 10, fix in 30 days or an agreed date.
- Fix `ce08fb9` (2026-10-17). The `codeqa` judge redirect handler compared only the network location (host and port). A same-host redirect from `https` to `http` kept the credential header and could send it in plain text. The handler now compares scheme, host and port, with default ports filled in. Scope: only the opt-in judge path, when `CODEQA_JUDGE_BASE` is set.
- Fix `b3bdded` (2026-10-17). The same redirect gap existed in three more clients that send a key: the DeepSeek client and both tuner drivers used the stdlib redirect handler, which copies every header to any redirect target. All four clients now use one opener, `safe_http`, which strips credential headers when the origin changes. A guard test fails if a source file builds a credentialed request and calls the stdlib `urlopen`. The DeepSeek preflight now also refuses AWS, GitHub and Slack tokens, bearer tokens and JWTs.
- Advisory for `ce08fb9`: drafted, publication pending. This page will link the GHSA ID when it is public.

## Planned research

**T1 and T3 are PLANNED; no results exist for them. T2 started on 2026-10-17: see [security/README.md](../security/README.md) (11 advisories triaged, 4 fixed in `64e3034`).**

| Track | Goal | Measure |
|---|---|---|
| T1 | Detect agent-targeted prompt-injection text in public packages and repos. Extends `find_injection_markers`. | precision and recall on a labelled set |
| T2 | Triage advisories for this project's own dependencies and publish OpenVEX statements. | statements per advisory, with reasons |
| T3 | Behaviour detectors over agent-harness logs for hostile packages that drive local agent CLIs. | detection on recorded logs |

Harm guards for all three tracks:

1. Read samples statically. Never run them.
2. Publish indicators and defanged excerpts only.
3. Write no new payloads.
4. Do not probe third-party systems.

## Method and limits

- Own-log numbers come from one machine and one user, the maintainer. They are self-reported and small. They are not rates.
- The incident table relies on one roundup. This page did not check each primary incident report.
- The check mapping is a retrospective judgment by the maintainer and one adversarial model reviewer (R:432-451). No check was run against the incidents.
- The `tool_decision` field list at R:507-510 came from an agent's reading. Only the "which source matched" sentence and the attribute list were re-read on the docs page on 2026-10-17.
- Vendor and arXiv numbers are quoted, not reproduced. The Anthropic sample sizes (n = 52, 10,000, 1,000) were re-read on the post on 2026-10-17.
- Line numbers refer to commit `ce08fb9`. They can move in later commits.
