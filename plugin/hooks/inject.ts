// §5 injection under the §17 contract, as amended by pivot P3: only at decision time, only what
// changes the decision, nothing when there is nothing to say — and every injection measured by the
// session's arm (evidence 80% / holdout 20%; the holdout never injects).
//   skill.prompt   — the evidence table, only READY rows, only when at least one exists (none in v1).
//   tool.describe  — Agent/Workflow: one pressure line, only when pressure ≠ GREEN.
//   prompt.compose — ≤ 4 lines of pressure, only when pressure ≠ GREEN and Agent/Workflow is offered.
import type { PromptComposeInput, PromptComposeSection } from 'claude-code'

import type { Arm, Level } from '../types/index.d.ts'
import { evidenceRows, kTok, type EvidenceRow } from './evidence.ts'
import type { Host } from './host.ts'
import { record } from './observe.ts'
import { everyOnce, type Runtime } from './runtime.ts'

export const INJECT_SKILLS: ReadonlySet<string> = new Set([
  'writing-plans',
  'subagent-driven-development',
  'dispatching-parallel-agents',
  'executing-plans',
  'datapce',
])
export const DESCRIBE_TOOLS = ['Agent', 'Workflow'] as const
export const MAX_SECTION_LINES = 25
export const MAX_ROWS = 18
export const MAX_DESCRIBE_LINES = 2
export const MAX_COMPOSE_LINES = 4
export const SESSION_CAP_BYTES = 2048
export const HOLDOUT_PCT = 20
export const INVALIDATE_EVERY_MS = 600_000
export const COMPOSE_ID = 'datapce:pressure'
export const RULE = 'rule: advice only — name the tier per task in the plan; your choice of model is never changed.'

export const bareSkill = (skill: string): string => skill.slice(skill.lastIndexOf(':') + 1)

export function fnv1a(s: string): number {
  let h = 0x811c9dc5
  for (const b of new TextEncoder().encode(s)) {
    h ^= b
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h
}

export const armOf = (sessionId: string): Arm => (fnv1a(sessionId) % 100 < HOLDOUT_PCT ? 'holdout' : 'evidence')

/** The arm a route row is stamped with: from the session id, never from a default; 'unknown' before identify (hot reload). */
export const armStamp = (rt: Pick<Runtime, 'sessionId'>): Arm | 'unknown' => (rt.sessionId !== '' ? armOf(rt.sessionId) : 'unknown')

export const utf8Bytes = (s: string): number => new TextEncoder().encode(s).length

export const budgetLeft = (rt: Runtime): number | null =>
  rt.options.budgetUsd > 0 && rt.lastCostUsd !== null ? Math.max(0, rt.options.budgetUsd - rt.lastCostUsd) : null

/** The evidence table: READY rows only (never WARMING/COLD/DRIFTING); no READY row → no text at all. */
export function evidenceSection(rows: readonly EvidenceRow[], level: Level, budgetLeftUsd: number | null): string | null {
  const ready = rows.filter(r => r.state === 'READY')
  if (ready.length === 0) return null
  const lines = [
    '## datapce evidence (this machine)',
    `pressure ${level}${budgetLeftUsd === null ? '' : ` · budget $${budgetLeftUsd.toFixed(2)} left`}`,
    '| task_type | tier | n pass% | tok μ | state |',
    '|---|---|---|---|---|',
    ...ready.slice(0, MAX_ROWS).map(r => `| ${r.taskType} | ${r.tier} | ${r.n} ${r.passPct}% | ${kTok(r.tokMean)} | READY |`),
    RULE,
  ]
  return lines.slice(0, MAX_SECTION_LINES).join('\n')
}

const ADVICE: Record<Exclude<Level, 'GREEN'>, string> = {
  AMBER: 'limit parallel fan-out',
  RED: 'serialize heavy fan-out',
}

/** Agent/Workflow description: the pressure line, only when not GREEN (P3: no tiers line). */
export function describeText(level: Level): string | null {
  if (level === 'GREEN') return null
  return [`datapce: pressure ${level} — ${ADVICE[level]}`].slice(0, MAX_DESCRIBE_LINES).join('\n')
}

/** prompt.compose section: ≤ 4 lines, only when not GREEN. Depends on the level alone (cache-stable). */
export function composeText(level: Level): string | null {
  if (level === 'GREEN') return null
  return [
    `## datapce: pressure ${level}`,
    `Rate limits or the backend are under pressure: ${ADVICE[level]} (Agent/Workflow); prefer the cheapest tier that fits each task.`,
  ]
    .slice(0, MAX_COMPOSE_LINES)
    .join('\n')
}

const rowsNow = (rt: Runtime): EvidenceRow[] => evidenceRows(rt.cells, rt.stats, rt.level.level)

/**
 * One injection decision: arm, cap, log, publish. `appended` is exactly what would be added.
 * Returns true when it may be appended. No identified session (a hot reload skipped session.start)
 * means no arm is known, so nothing is injected and nothing logged.
 */
async function admit(host: Host, rt: Runtime, site: string, name: string, appended: string, now: number): Promise<boolean> {
  if (rt.sessionId === '') return false
  // The arm comes from the session id at every use, never from rt.arm: if a session.start step
  // before injectStart threw, rt.arm would still hold its default and a holdout would fail open.
  const arm = armOf(rt.sessionId)
  const bytes = utf8Bytes(appended)
  const base = { ev: 'inject', ts: now, site, skill: name, arm }
  if (arm === 'holdout') {
    record(rt, { ...base, bytes: 0, withheld: true })
    return false
  }
  if (rt.injectedBytes + bytes > SESSION_CAP_BYTES) {
    record(rt, { ...base, bytes: 0, capped: true })
    return false
  }
  rt.injectedBytes += bytes
  rt.injectedSections += 1
  record(rt, { ...base, bytes })
  await host.publish.inject({ arm, bytes: rt.injectedBytes, sections: rt.injectedSections })
  return true
}

/**
 * describe/compose: the engine re-renders these, so each distinct text is decided (logged, debited)
 * once per session and the admitted text is re-sent unchanged — a re-render never re-debits the cap,
 * never drops a line it showed before, and a level that flaps back re-uses its earlier decision.
 */
const IN_FLIGHT = new WeakMap<Runtime, Map<string, Promise<boolean>>>()

async function admitOnce(host: Host, rt: Runtime, site: string, name: string, appended: string, now: number): Promise<boolean> {
  const key = `${site}|${name}|${appended}`
  const known = rt.injectDecisions.get(key)
  if (known !== undefined) return known
  if (rt.sessionId === '') return false
  // Concurrent renders of the same text share one in-flight decision: the memo is claimed before awaiting.
  let flights = IN_FLIGHT.get(rt)
  if (flights === undefined) IN_FLIGHT.set(rt, (flights = new Map()))
  const pending = flights.get(key)
  if (pending !== undefined) return pending
  const decision = admit(host, rt, site, name, appended, now).then(
    admitted => {
      rt.injectDecisions.set(key, admitted)
      flights.delete(key)
      return admitted
    },
    () => {
      flights.delete(key)
      return false
    },
  )
  flights.set(key, decision)
  return decision
}

export async function skillSection(host: Host, rt: Runtime, skill: string, text: string, now: number): Promise<string> {
  try {
    if (!INJECT_SKILLS.has(bareSkill(skill))) return text
    const section = evidenceSection(rowsNow(rt), rt.level.level, budgetLeft(rt))
    if (section === null) return text
    const appended = `\n\n${section}`
    return (await admit(host, rt, 'skill', skill, appended, now)) ? `${text}${appended}` : text
  } catch {
    return text
  }
}

export async function describe(host: Host, rt: Runtime, tool: string, description: string, now: number): Promise<string> {
  try {
    const line = describeText(rt.level.level)
    if (line === null) return description
    const appended = `\n${line}`
    return (await admitOnce(host, rt, 'describe', tool, appended, now)) ? `${description}${appended}` : description
  } catch {
    return description
  }
}

/**
 * prompt.compose fires for every request: decided once per distinct text (admitOnce), then re-sent
 * unchanged (cache-stable). The text follows the live level, which already has hysteresis (enter
 * after 2 windows, exit after 3); a flap back to a level re-uses its decision without a new debit.
 * A render that only measures the prompt (`analysis`) never takes a decision.
 */
export async function composeSections(
  host: Host,
  rt: Runtime,
  e: Pick<PromptComposeInput, 'tools' | 'traits'>,
  sections: readonly PromptComposeSection[],
  now: number,
): Promise<readonly PromptComposeSection[]> {
  try {
    const text = composeText(rt.level.level)
    if (text === null) return sections
    if (!(e.tools ?? []).some(t => (DESCRIBE_TOOLS as readonly string[]).includes(t))) return sections
    const known = rt.injectDecisions.get(`compose|prompt|${text}`)
    if (known === undefined && (e.traits ?? []).includes('analysis')) return sections
    return (await admitOnce(host, rt, 'compose', 'prompt', text, now)) ? [...sections, { id: COMPOSE_ID, text, scope: 'session' }] : sections
  } catch {
    return sections
  }
}

/** A capped session would only re-render the description to show the same thing: skip the re-price. */
function wouldCap(rt: Runtime, text: string | null): boolean {
  if (text === null) return false
  const appended = `\n${text}`
  const known = DESCRIBE_TOOLS.map(t => rt.injectDecisions.get(`describe|${t}|${appended}`))
  if (known.every(k => k !== undefined)) return !known.some(k => k === true)
  return rt.injectedBytes + utf8Bytes(appended) > SESSION_CAP_BYTES
}

/**
 * session.start (after identify and the router loaded cells): the arm and the describe refresh.
 * The arm is logged where it is measured — on every route row (inject_arm) and every inject row —
 * not as a row of its own (a session with nothing to say writes nothing).
 */
export async function start(host: Host, rt: Runtime): Promise<void> {
  try {
    rt.arm = armOf(rt.sessionId)
    await host.publish.inject({ arm: rt.arm, bytes: rt.injectedBytes, sections: rt.injectedSections })
    if (rt.arm === 'holdout') return
    // tool.describe is cached by the engine: re-render it only when its text changed, and at most
    // once per 10 minutes (each re-render re-prices the prompt cache).
    let last = describeText(rt.level.level)
    let lastInvalidate = await host.now()
    everyOnce(host, rt, 'inject.refresh', 60_000, () => {
      void (async () => {
        try {
          const now = await host.now()
          const text = describeText(rt.level.level)
          if (text !== last && now - lastInvalidate >= INVALIDATE_EVERY_MS && !wouldCap(rt, text)) {
            last = text
            lastInvalidate = now
            host.invalidate('tool.describe')
          }
        } catch {
          // a missed refresh keeps the old description
        }
      })()
    })
  } catch {
    // no arm, no refresh: the default arm stands and nothing is scheduled
  }
}
