# Security note — local model serving trust boundary

**Scope:** the local-model path — ollama on `127.0.0.1:11434` and the measure-only apex proxies on
localhost (`:8788` codex, `:8789` local-qwen) that front it. This note records ONE threat-model
assumption so it is explicit rather than silent. It is not a full security audit.

## The assumption this challenges

apex's local serving implicitly treats **"bound to localhost + isolated process credentials =
confidential."** A recent result (Weiss et al., *Detokenization Leaks: Reconstructing Local LLM
Outputs From Cache Traces*, 2026-09) shows that assumption is **insufficient against a co-resident
attacker**. The attack targets **CPU-side detokenization** — which runs after every generated token
**even when the model weights and KV cache live entirely on the GPU** — using Flush+Reload on the
shared tokenizer code to detect an imminent decode, then Prime+Probe cache-set profiling to
reconstruct the output text. It does **not** need shared model weights, CPU-offloaded weights, or a
particular MoE architecture — only shared CPU execution resources.

So the correct boundary statement is: **detokenization, streaming, and the tokenizer library are
inside the serving system's security boundary.** A localhost API and a separate process UID do not,
by themselves, keep local model *output* confidential from an untrusted workload sharing the CPU.

## What actually applies to this deployment (honest scoping)

The attack's preconditions are specific and **mostly absent on the primary deployment** (a
single-user developer workstation):

- **Co-residency + shared CPU / SMT with untrusted code.** On a single-user box running only the
  operator's own tools, there is no hostile co-tenant — the precondition largely fails. The risk
  becomes real on a **shared/multi-tenant host**, a box running **untrusted local workloads** (a
  sandbox, an untrusted container, a co-tenant VM), or anywhere an adversary can schedule code on the
  same physical core.
- **API access for profiling** — the attacker needs to query the same service instance to build its
  token→cache-trace profile, and continued access to the **same process instance**.
- The paper's strongest numbers (85–87% paragraph semantic-equivalence) are **controlled semantic
  reconstruction judged partly by an LLM — not exact token recovery**, and drop to ~30% ASR on a
  realistic end-to-end local-agent deployment. Treat it as a real but bounded confidentiality risk,
  not output exfiltration at rest.

## Controls (apply proportionally to the deployment)

Only warranted where the co-tenancy precondition holds — do not pay these costs on a single-user box:

1. **Isolate the inference service from untrusted code** — separate user, VM, or container; do not
   co-schedule the local model server with code you don't trust on the same host.
2. **Restrict local API access** — the ollama / proxy ports are `127.0.0.1`-bound; keep them so, and
   don't expose them to untrusted local processes that could drive the profiling queries.
3. **Avoid hostile co-tenancy** — don't run the local model server on a shared/multi-tenant host
   alongside an adversary who can schedule on the same physical core.
4. **Consider SMT isolation** for sensitive deployments — disabling SMT, cache partitioning, or
   rotating worker processes raises the attack cost. Each has a performance or operational cost, so
   this is a deliberate trade-off for a genuinely multi-tenant/sensitive host, not a default.

## Status

Documentation-only. No code or serving change is made here; the primary single-user topology does
not meet the attack's co-tenancy precondition. This note exists so that if apex's local serving is
ever moved onto a shared or untrusted-co-tenant host, the boundary and the controls are already
written down rather than rediscovered.
