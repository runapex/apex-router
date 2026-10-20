import type { On, SessionMeasureInput } from 'claude-code'
import { atom, read, update } from 'claude-code'

import type { BackendView, Dispatch, Level, Measure, SignalsView } from '../types/index.d.ts'
import type { Host } from './host.ts'
import type { Runtime } from './runtime.ts'
import { EMPTY_BACKEND, EMPTY_SIGNALS } from './state.ts'
import { tierOf } from './tiers.ts'

const measure = atom({ plugin: 'datapce', key: 'measure' } as const, null as Measure | null)
const signals = atom({ plugin: 'datapce', key: 'signals' } as const, EMPTY_SIGNALS)
const dispatches = atom({ plugin: 'datapce', key: 'dispatches' } as const, [] as Dispatch[])
const backend = atom({ plugin: 'datapce', key: 'backend' } as const, EMPTY_BACKEND)
const bandHidden = atom({ plugin: 'datapce', key: 'bandHidden' } as const, false)

const LIMIT_LABEL: Record<string, string> = { five_hour: '5h', seven_day: '7d', spend_limit: 'spend' }

/** The figures the band draws, from one session.measure: the tightest window, context, cost. */
export function measureOf(e: SessionMeasureInput): Measure {
  let tight: SessionMeasureInput['rateLimits'][number] | undefined
  for (const w of e.rateLimits) if (tight === undefined || w.percentUsed > tight.percentUsed) tight = w
  return {
    ctxPercent: e.context.percent ?? null,
    limitKind: tight?.kind ?? null,
    limitPercent: tight?.percentUsed ?? null,
    resetsAt: tight?.resetsAt ?? null,
    costUsd: e.cost?.usd ?? null,
  }
}

export function meter(percent: number, width = 8): string {
  const clamped = Math.max(0, Math.min(100, percent))
  const full = Math.round((clamped / 100) * width)
  return '▇'.repeat(full) + '░'.repeat(width - full)
}

/** Exact money, two decimals. From $1e15 up (toFixed would print an exponent from 1e21) it is the short form. */
export const usd = (x: number): string => (x < 1e15 ? `$${x.toFixed(2)}` : usdShort(x))

/** A limit kind the host did not document is shown as plain ASCII, at most 12 characters (it is untrusted text). */
const limitName = (m: Measure): string => {
  const known = LIMIT_LABEL[m.limitKind ?? '']
  if (known !== undefined) return known
  const plain = (m.limitKind ?? '').replace(/[^A-Za-z0-9_ -]/g, '').slice(0, 12).trim()
  return plain === '' ? 'limit' : plain
}

/** Terminal columns: East Asian wide and fullwidth code points take 2. */
export function cols(s: string): number {
  let n = 0
  for (const ch of s) {
    const c = ch.codePointAt(0) ?? 0
    n += (c >= 0x1100 && c <= 0x115f) || (c >= 0x2e80 && c <= 0xa4cf) || (c >= 0xac00 && c <= 0xd7a3) || (c >= 0xf900 && c <= 0xfaff) ||
      (c >= 0xfe30 && c <= 0xfe4f) || (c >= 0xff00 && c <= 0xff60) || (c >= 0xffe0 && c <= 0xffe6) || (c >= 0x1f300 && c <= 0x1faff) || (c >= 0x20000 && c <= 0x3fffd)
      ? 2 : 1
  }
  return n
}

const limitCell = (m: Measure): string | null => (m.limitPercent === null ? null : `${limitName(m)} ${Math.round(m.limitPercent)}%`)

export function measureCells(m: Measure): string[] {
  const out: string[] = []
  const lim = limitCell(m)
  if (lim !== null) out.push(lim)
  if (m.ctxPercent !== null) out.push(`context ${Math.round(m.ctxPercent)}%`)
  if (m.costUsd !== null) out.push(usd(m.costUsd))
  return out
}

export type BandInput = {
  m: Measure | null
  s: SignalsView
  ds: readonly Dispatch[]
  b: BackendView
  budgetUsd: number
}

/** Context at or above this share: the user must act (compact). */
export const CONTEXT_FULL = 80
/** Budget alert: spend at or above this share of the budget, or empty within BUDGET_MINUTES. */
const BUDGET_SHARE = 0.9
const BUDGET_MINUTES = 60

/** A condition the user must act on: what is wrong and what to do. The user decides; nothing changes the model. */
export type Alert = { kind: 'limit' | 'budget' | 'context'; cause: string; action: string }

/** Short money for the band: $9.10 below $1,000; then $12.3k, $1.2M (the pane keeps exact figures). */
export const usdShort = (x: number): string =>
  x < 1000 ? `$${x.toFixed(2)}` : x < 999_950 ? `$${+(x / 1e3).toFixed(1)}k` : x < 999_950_000 ? `$${+(x / 1e6).toFixed(1)}M` : x < 999_950_000_000 ? `$${+(x / 1e9).toFixed(1)}B` : x < 999.5e12 ? `$${Math.round(x / 1e12)}T` : '$999T+'

/** Budget below $1,000: whole dollars stay whole (`$10`), else two decimals (`$12.50`); never exponent notation. */
const budgetShort = (b: number): string => (b < 1000 ? `$${Number.isInteger(b) ? b : +b.toFixed(2)}` : usdShort(b))

const budgetCell = (cost: number, budget: number): string => `${usdShort(cost)}/${budgetShort(budget)}`

/** Every condition that needs the user now, most urgent first. Calm states give an empty list. */
export function alertsOf(i: Pick<BandInput, 'm' | 's' | 'budgetUsd'>): Alert[] {
  const out: Alert[] = []
  const m = i.m
  if (i.s.level === 'RED') {
    const cause = m !== null && m.limitPercent !== null && m.limitPercent >= 80 ? `${limitName(m)} limit ${Math.round(m.limitPercent)}%` : 'pressure RED'
    out.push({ kind: 'limit', cause, action: 'pause new subagents' })
  }
  if (m !== null && m.costUsd !== null && i.budgetUsd > 0) {
    const mins = i.s.minutesToExhaust
    const soon = mins !== null && mins <= BUDGET_MINUTES
    if (soon || m.costUsd >= BUDGET_SHARE * i.budgetUsd) {
      const when = mins !== null ? `, empty ≈${Math.round(mins)} min` : ''
      out.push({ kind: 'budget', cause: `${budgetCell(m.costUsd, i.budgetUsd)}${when}`, action: 'pause new subagents' })
    }
  }
  if (m !== null && m.ctxPercent !== null && m.ctxPercent >= CONTEXT_FULL) {
    out.push({ kind: 'context', cause: `context ${Math.round(m.ctxPercent)}% full`, action: 'run /compact' })
  }
  return out
}

/** Subagents whose model tier differs from the advice. */
export const differsOf = (ds: readonly Dispatch[]): number => ds.filter(d => d.advised !== null && d.advised !== tierOf(d.resolved)).length

/** Band target width: 80 columns less the Hide button. */
export const BAND_WIDTH = 74

/**
 * §6 (advise-only): `apex LEVEL[ · cause · action] · figures`. The action shows only when the user
 * must act, right after the level, so a narrow terminal cuts figures, never the action. When the line
 * is long, figures go in a fixed order: the tier-asked rate, the advice count, the limit below 80%.
 */
export const bandLine = (i: BandInput): string => bandParts(i).line

/** The band line and the figures it dropped to fit (the pane shows those). */
export function bandParts(i: BandInput): { line: string; dropped: string[] } {
  const alerts = alertsOf(i)
  const first = alerts[0]
  const level = `apex ${first !== undefined && i.s.level === 'GREEN' ? 'pressure ' : ''}${i.s.level}`
  const head = [level]
  if (first !== undefined) {
    // Odd inputs can make the cause too long. Cut the cause, never the level or the action.
    const room = BAND_WIDTH - cols(level) - cols(first.action) - 6
    head.push(`${cols(first.cause) <= room ? first.cause : `${first.cause.slice(0, Math.max(1, room - 1))}…`} · ${first.action}`)
  }
  const m = i.m
  // Optional figures carry a drop rank (lower drops first); 99 drops only in the final width guard.
  const figs: { text: string; drop: number }[] = []
  if (m !== null && m.limitPercent !== null && !(first?.kind === 'limit' && first.cause.includes(' limit '))) figs.push({ text: limitCell(m)!, drop: m.limitPercent < 80 ? 3 : 99 })
  if (m !== null && m.ctxPercent !== null && first?.kind !== 'context') figs.push({ text: `context ${Math.round(m.ctxPercent)}%`, drop: 4 })
  if (m !== null && m.costUsd !== null && first?.kind !== 'budget') figs.push({ text: i.budgetUsd > 0 ? budgetCell(m.costUsd, i.budgetUsd) : usdShort(m.costUsd), drop: 99 })
  if (i.ds.length > 0) {
    const differs = differsOf(i.ds)
    const n = `${i.ds.length} subagent${i.ds.length === 1 ? '' : 's'}`
    figs.push({ text: differs > 0 ? `${differs} of ${i.ds.length} differ from advice` : n, drop: 2 })
  }
  if (i.b.conformance !== null) figs.push({ text: `ran tier asked ${Math.round(100 * i.b.conformance)}%`, drop: 1 })
  const line = (fs: typeof figs): string => [...head, ...fs.map(f => f.text)].join(' · ')
  let kept = figs
  for (const rank of [1, 2, 3, 4]) {
    if (cols(line(kept)) <= BAND_WIDTH) break
    kept = kept.filter(f => f.drop !== rank)
  }
  while (cols(line(kept)) > BAND_WIDTH && kept.length > 0) kept = kept.slice(0, -1)
  return { line: line(kept), dropped: figs.filter(f => !kept.includes(f)).map(f => f.text) }
}

/** The status entry (no band on this surface): the level, the action when one is due, the cost. */
export const statusText = (level: Level, costUsd: number | null, alert?: Alert): string =>
  `apex ${alert !== undefined && level === 'GREEN' ? 'pressure ' : ''}${level}${alert === undefined ? '' : ` · ${alert.cause} · ${alert.action}`}${costUsd === null || alert?.kind === 'budget' ? '' : ` · ${usd(costUsd)}`}`

/** The last signals each runtime published (the status entry reads the budget forecast from them). */
const lastSignals = new WeakMap<Runtime, SignalsView>()

const statusOf = (rt: Runtime, m: Measure | null): string => {
  const s = { ...(lastSignals.get(rt) ?? EMPTY_SIGNALS), level: rt.level.level }
  return statusText(rt.level.level, m?.costUsd ?? null, alertsOf({ m, s, budgetUsd: rt.options.budgetUsd })[0])
}

/** Surfaces that raise AbovePrompt: only there can the band draw. */
const BAND_SURFACES = new Set(['terminal', 'desktop'])

/** True while the band can show this session; the status entry would only repeat it. */
const bandShows = (rt: Runtime, hidden: boolean): boolean =>
  rt.options.band && !hidden && rt.surface !== null && BAND_SURFACES.has(rt.surface)

/**
 * §12: the status entry is for surfaces without a band (vscode, mobile, band off or hidden); where
 * the band shows, the entry is cleared (one set earlier, e.g. by an older version, would linger).
 * `claude -p` (surface null) gets none.
 */
export async function refreshStatus(host: Host, rt: Runtime, m: Measure | null, s?: SignalsView): Promise<void> {
  if (s !== undefined) lastSignals.set(rt, s)
  if (rt.surface === null) return
  host.status(bandShows(rt, await host.bandHidden()) ? undefined : statusOf(rt, m))
}

export function install(on: On, rt: Runtime): void {
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!rt.options.band || e.props.hasSurvey) return next(e)
    const m = await read($, measure)
    const ds = await read($, dispatches)
    if ((m === null && ds.length === 0) || (await read($, bandHidden))) return next(e)
    const line = bandLine({ m, s: await read($, signals), ds, b: await read($, backend), budgetUsd: rt.options.budgetUsd })
    const { Box, Text, Button } = $.ui.resolve(e)
    return (
      <Box key="datapce-band" flexDirection="row" gap={1}>
        <Box key="datapce-band-line">
          <Text wrap="truncate-end">{line}</Text>
        </Box>
        <Button
          key="datapce-hide"
          label="Hide"
          plain
          onPress={async () => {
            await update($, bandHidden, () => true)
            // The figures move to the status entry now, not at the next measure or tick.
            $.ui.status(statusOf(rt, await read($, measure)))
          }}
        />
      </Box>
    )
  })
}
