// The structured handoff (spec §6: replaces the Stop-hook nudge; same structured-state instruction).
import type { Host } from './host.ts'
import { toastOnce, type Runtime } from './runtime.ts'

// BEGIN HANDOFF_STATE_TEMPLATE — byte-for-byte apex_router.handoff_state.render_template();
// tests/test_handoff_template_plugin.py guards it.
export const HANDOFF_TEMPLATE = `## Structured continuation state

Fill this in BEFORE stopping — once FILLED, it is the only context the fresh session
needs. Record current state, not history: discard reasoning, keep decisions and facts.
If any field is still _…_ the handoff is INCOMPLETE — do not paste it; fill it
first. Check with: python -m apex_router.handoff_state validate <this file>.
(SKILL.state handoff — schema: apex_router.handoff_state.FIELDS)

- **goal**: _…_ one sentence: what this session is trying to achieve
- **constraints**: _…_ hard requirements and the do-not-touch list
- **decisions**: _…_ decisions ALREADY made, each with its reason — do not re-litigate
- **files_touched**: _…_ paths created/modified, one per line, with what changed
- **open_issues**: _…_ unresolved problems, failing tests, blockers
- **next_action**: _…_ the single step the fresh session should take FIRST
`
// END HANDOFF_STATE_TEMPLATE

export const fmtTok = (t: number): string => (t >= 1e6 ? `${(t / 1e6).toFixed(1)}M` : `${Math.round(t / 1e3)}k`)

export const handoffDue = (cacheRead: number, threshold: number | null): boolean => threshold !== null && cacheRead >= threshold

export const handoffToast = (threshold: number): string =>
  `this session's cache reads passed ${fmtTok(threshold)} tokens (your handoff threshold) — run /apex handoff, fill the block, start fresh`

export function checkHandoff(host: Host, rt: Runtime, now: number): void {
  const threshold = rt.backend.handoffTokens
  if (threshold !== null && handoffDue(rt.cacheRead, threshold)) toastOnce(host, rt, 'handoff', handoffToast(threshold), now)
}
