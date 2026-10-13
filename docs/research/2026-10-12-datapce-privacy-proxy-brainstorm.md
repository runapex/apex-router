# datapce as a privacy service: semantic-preserving pseudonymisation in the proxy

Status: REJECTED by the owner 2026-10-12 ("not a good fit"; datapce = data + policy computation engine). Kept as a record. Not a spec. Nothing here is built. Owner approval of the
design is required before a spec is written (`docs/superpowers/specs/`), and the spec before
a plan.

## 1. The idea as stated, and what is assumed

Said by the owner (2026-10-12): datapce as a service that lets a customer use *any* model
safely. Everything routed into the model is obfuscated but semantically equivalent; when the
response comes back the information is restored. The model never sees the sensitive values;
the customer sees no difference; quality is not affected.

Assumed (correct me):

- A1. "Service" means the local-first proxy the customer already runs (datapce :8789 /
  apex :8788), optionally team-hosted. Not a datapce-operated cloud relay, which would make
  datapce the party that sees everything.
- A2. "Sensitive" means secrets (keys, tokens, passwords, connection strings), direct
  identifiers (emails, phones, people, account and card numbers), and infrastructure or
  business identifiers (internal hosts, IPs, domains, repo and customer names, codenames).
  Not: quantities the model must compute on, and not code identifiers.
- A3. The primary client is a coding agent (Claude Code, Codex). That means most tokens are
  `tool_result` bodies (file contents, shell output), and the model's output is *executed*
  (patches, commands), so restoration must be exact in `tool_use` inputs, not just in prose.
- A4. "Quality not affected" is a measured claim, gated like every other datapce claim, not
  a promise. The Evidence pillar is where it lives.

## 2. Where this sits in datapce (Policy · Capacity · Evidence)

- Policy: a classification policy says which classes exist, how each is transformed, and
  which model tiers may receive which class (e.g. secrets: never; PII: pseudonymised to
  cloud, raw to local). The site's own Policy example, "customer email never goes to a cloud
  model", becomes real code.
- Capacity: pseudonymisation is what *unlocks* routes. A request that today may only go to a
  local model because it holds customer data may, once pseudonymised, go to any model. The
  business argument is "privacy expands the capacity pool", not "privacy as a tax".
- Evidence: a per-request ledger (spans per class, bytes, restored count, *unrestored*
  pseudonyms that leaked into the client, detector latency) and a paired quality gate
  (with vs without rewrite on a frozen task slice), reported in the digest.

## 3. Prior art and the gap

- PasteGuard (Apache 2.0, TypeScript): proxy for Claude Code / Codex / Cursor; regex +
  checksums + GLiNER NER; opaque placeholders ("providers see placeholders"); restores
  placeholders in responses including streaming; route-mode to a local model. Session
  consistency and tool_use restoration are not documented.
- Microsoft PII Shield (Presidio): the "anonymise → LLM → de-anonymise" sandwich with
  `{{PERSON_1}}` placeholders, mapping in a session store, longest-placeholder-first restore.
- kiji-proxy (Dataiku, Go), privacy-filter (Go), clawshield (Go): same shape, OpenAI wire.
- Research: "LLM Anonymization Against Agentic Re-Identification" (arXiv 2605.30848) and
  the participant re-identification paper (arXiv 2601.05918) show that
  placeholder-level anonymisation is reversible by an agentic LLM from context. Treat as
  PLAUSIBLE (abstracts only). USPTO 12032720 claims the generic proxy flow (prior art
  exists, so the flow itself is not novel; the semantic-equivalence and evidence parts are
  where to differentiate).

The gap nobody fills: (a) replacements that are *semantically equivalent* (same type, shape,
ordering and cross-reference structure) rather than placeholders, so the model reasons and
writes code exactly as it would on the real values; (b) correctness on the coding-agent
loop (tool_result in, tool_use out, every turn, prompt-cache stable); (c) a measured quality
and leak ledger instead of a claim.

## 4. Threat model (honest version)

Protects against: the provider, its logs, its trainers and its breach seeing the *values* in
the listed classes, for detected spans.

Does not protect against: inference from context (a model can often infer who the customer
is from unredacted facts); detector misses (recall is never 1.0); quasi-identifier
combinations; values the model derives (it may compute a real quantity from pseudonymised
inputs if the policy left the operands raw); anything in images or PDFs; the client machine
itself, which keeps everything in plain text.

Therefore the public claim must be "reduces exposure of these classes, measured per request",
never "the model never sees sensitive information". The ledger is what makes the claim
honest.

## 5. Approaches

A. Deterministic, format-preserving pseudonymisation in the proxy (recommended first).
   Detect with regex + checksums + customer dictionaries; replace with same-shape fakes
   derived by HMAC so the same value always maps the same way; restore on the response,
   streaming included. No ML in the path. Predictable latency, testable, measurable.
   Limitation: free-text names and addresses are only caught when they are in a dictionary.

B. A plus a local NER layer (GLiNER-class) for free-text PII, scoped to *new* content only
   (the proxy already hashes the unchanged message prefix, so only the new turn is scanned).
   More recall, more false positives, latency that must be measured. Phase 2, gated.

C. Semantic abstraction: a local model rewrites the prompt into a de-contextualised
   equivalent and re-grounds the answer (Hide-and-Seek / Papillon style). Strongest
   protection, but restoration is lossy, quality cost is large and hard to bound, and it
   breaks the "executed output" requirement of coding agents. Not now.

D. Route mode (sensitive requests go to a local model only). Already datapce's Policy
   pillar; keep it as the per-class "never leaves" option, complementary to A.

Recommendation: A, then B, with D as a policy fallback. C only if a customer needs
free-text abstraction and accepts a measured quality cost.

## 6. Design sketch for A

### 6.1 Classes and transforms (semantic equivalence is per class)

| class | detect | replacement | why the model is unaffected |
|---|---|---|---|
| secret (API key, JWT, PEM, password, conn-string) | known prefixes, entropy, checksums | same prefix, same length, random body | the model never needs the value, only its shape |
| email | regex | fake local part + fake domain of the same kind (keep TLD, keep internal/external) | parsing, grouping, domain logic survive |
| phone, IBAN, card, account id | regex + checksum | checksum-valid fake of the same format | validation code and regexes still pass |
| person name | dictionary (phase 1), NER (phase 2) | fake name with same token count and casing | prose and code read naturally |
| host, IP, URL, domain | regex + dictionary | same structure, structural words kept by policy (`db`, `prod`) | the model still knows it is a prod database |
| repo, customer, codename | dictionary (auto-seeded from git remotes, `.env`, customer list) | fake of the same casing and length | cross-references hold |
| quantities, dates | not transformed by default | raw | ordering and arithmetic must not break |
| code identifiers | never | raw | patches must apply |

Rules: one value, one pseudonym, everywhere, for the life of the mapping; pseudonyms never
collide with each other or with real strings in the request; replacement is longest-span
first; a value nested inside another detected span is handled once.

### 6.2 Mapping and vault

Forward map: pseudonym = generator(class, HMAC(install_key, canonical_value)). No store is
needed to compute it, so every turn and every session re-maps identically and the provider
prompt cache stays warm. Reverse map: stored per proxy session in `~/.apex/vault.db` (0600),
garbage-collected on the same 14-day schedule as `state.db`. Trade-off to decide: an
install-scoped key gives consistency across sessions but a long-lived mapping is a bigger
asset if the vault leaks; a session-scoped key is safer and still cache-stable within a
session.

### 6.3 Request rewriter

One insertion point exists: `Upstream.prepare_body(client_kind, raw_path, body)`
(`proxy_engine/proxy/upstream.py:286`), already the "only place the proxy edits request
bytes". The datapce copy removed it and must get it back. Walk the same shapes the shadow
decomposer already parses (`pipeline/shadow.py` `decompose`, `_block_text_and_tool`,
`_responses_item_blocks`): Anthropic `system`, `messages[].content` blocks (`text`,
`tool_result.content`, `tool_use.input`), `tools[]`; OpenAI chat `messages`; Responses
`input`. Re-serialise. Cache rewritten blocks by message hash (the chain in
`session/matcher.chain_of`) so an unchanged prefix is not re-scanned. Images and documents:
policy says block or pass; default pass with a ledger flag.

### 6.4 Response rewriter (the part that does not exist today)

The passthrough yields raw, still content-encoded chunks (`passthrough.py:204`). For the
rewrite path: send `accept-encoding: identity` upstream so the body is plain; parse SSE; for
Anthropic `text_delta` and `input_json_delta`, OpenAI `delta.content` and
`tool_calls[].function.arguments`, Responses `output_text.delta` and
`function_call_arguments.delta`, run a streaming replacer with a hold-back buffer of
(longest pseudonym − 1) characters, flushed at block boundaries. Inside JSON deltas the
restored value must be JSON-escaped. Non-streaming responses: parse, walk, replace. The
existing `usage.py` `_Decoder` and SSE line scanner are reusable.

Robustness: models mutate pseudonyms (case, underscore/hyphen, URL-encoding, pluralisation).
Choose pseudonyms from a mutation-resistant alphabet, match restoration over the common
transforms, and count every pseudonym that survives into the client as `unrestored` in the
ledger. That count is the leak-out metric.

### 6.5 Detectors and latency budget

Tier 0 (always): regex + checksum for secrets, emails, phones, IPs, IBAN, cards, JWT, PEM,
URLs. Tier 1 (always): dictionaries auto-seeded from `git config`, git remotes, `.env*`,
`~/.ssh/config` hosts, plus a customer-supplied list; a value in `.env` becomes a protected
value everywhere. Tier 2 (phase 2, opt-in): local NER on new content only. Budget: tool
results reach hundreds of KB; regex over 500 KB is tens of milliseconds; NER must be
measured before it is allowed in the path.

### 6.6 Evidence and gates

- Telemetry schema v13: `privacy_rewrite` with counts per class, bytes, restored and
  unrestored counts, latency. Never values.
- Canary tests, extending `tests/proxy_engine/test_secrets_canary.py`: a planted email, key
  and hostname must never reach a fake upstream, in either wire, streaming or not, and must
  come back restored in `tool_use` inputs.
- Quality gate: paired A/B on a frozen task slice with and without rewrite, using the
  existing bench seams (`bench.py` `run_bench`, `skillopt_bench`). Pre-declared: adopt only
  if the success delta's CI lower bound is above −3 points and the unrestored rate is
  below 0.5 % of pseudonyms. Kill: either bound violated after 100 tasks.
- Pseudonym-survival study: how often each model family echoes a pseudonym unchanged in
  prose and in tool arguments. This is the cheapest kill test for the whole idea.

## 7. Open questions that change the design

1. Scope of "sensitive": A2 (secrets, identifiers) only, or also business quantities and
   dates? Transforming quantities costs quality; leaving them raw leaks facts.
2. Pseudonym style: realistic same-shape fakes (recommended, semantic equivalence) or typed
   placeholders (more robust to restore, visibly redacted)? Could be per class.
3. Mapping key scope: install (cross-session consistency, cache-friendly) or session (safer).
4. Product shape: local-first proxy (assumed) or a hosted relay. A hosted relay makes
   datapce the trusted party and changes the pitch and the liability.
5. Images and documents: pass with a flag, or block when a strict policy is on.

## 8. Proposed first step (spike, about a day, throwaway code)

Question: do frontier and local models echo same-shape pseudonyms unchanged, especially in
`tool_use` arguments, and does task success move? Probe: hand-pseudonymise 30 tasks from the
existing gold set with fake emails, hosts and names; run each with and without the rewrite
through the current bench seams against two cloud models and one local tier; measure
survival rate and success delta. Kill the realistic-fake design if survival is below 95 %
or success drops more than the gate allows; fall back to typed placeholders.

## Sources

- https://github.com/sgasser/pasteguard
- https://techcommunity.microsoft.com/blog/azuredevcommunityblog/introducing-pii-shield-a-privacy-proxy-for-every-llm-call/4514726
- https://arxiv.org/pdf/2605.30848 (LLM Anonymization Against Agentic Re-Identification)
- https://www.red-gate.com/simple-talk/data-security-privacy-compliance/how-to-anonymize-pii-in-llm-pipelines-5-key-techniques-explained/
- https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/12032720
- https://web3.arxiv.org/pdf/2511.15744 (AnonLFI 2.0)
