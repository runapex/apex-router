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
import type { Runtime } from './runtime.ts'

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
export const RULE = 'rule: name the tier per task in the plan; datapce applies it at dispatch.'

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
  const bytes = utf8Bytes(appended)
  const base = { ev: 'inject', ts: now, site, skill: name, arm: rt.arm }
  if (rt.arm === 'holdout') {
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
  await host.publish.inject({ arm: rt.arm, bytes: rt.injectedBytes, sections: rt.injectedSections })
  return true
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
    return (await admit(host, rt, 'describe', tool, appended, now)) ? `${description}${appended}` : description
  } catch {
    return description
  }
}

/**
 * prompt.compose fires for every request: the decision (log, count) is taken once per distinct
 * text, then the admitted text is re-sent unchanged (cache-stable) until pressure changes.
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
    const offered = (e.tools ?? []).some(t => (DESCRIBE_TOOLS as readonly string[]).includes(t))
    if (text === null) {
      rt.composeDecision = null
      return sections
    }
    if (!offered) return sections
    if (rt.composeDecision?.text !== text) {
      if ((e.traits ?? []).includes('analysis')) return sections
      rt.composeDecision = { text, admitted: await admit(host, rt, 'compose', 'prompt', text, now) }
    }
    return rt.composeDecision.admitted ? [...sections, { id: COMPOSE_ID, text, scope: 'session' }] : sections
  } catch {
    return sections
  }
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
    host.every(60_000, () => {
      void (async () => {
        try {
          const now = await host.now()
          const text = describeText(rt.level.level)
          if (text !== last && now - lastInvalidate >= INVALIDATE_EVERY_MS) {
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
