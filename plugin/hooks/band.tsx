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

export const usd = (x: number): string => `$${x.toFixed(2)}`

const limitCell = (m: Measure): string | null =>
  m.limitPercent === null ? null : `${LIMIT_LABEL[m.limitKind ?? ''] ?? m.limitKind}:${Math.round(m.limitPercent)}%`

export function measureCells(m: Measure): string[] {
  const out: string[] = []
  const lim = limitCell(m)
  if (lim !== null) out.push(lim)
  if (m.ctxPercent !== null) out.push(`ctx ${meter(m.ctxPercent)} ${Math.round(m.ctxPercent)}%`)
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

/** §6 (advise-only, pivot P1/P6): apex ●LEVEL 5h:62%  ctx ▇▇▇▇▇░░░ 61%  $4.12/10  routes 7 (local 4 ✓3 ↑1) ▲2  conf 86% */
export function bandLine(i: BandInput): string {
  const lim = i.m === null ? null : limitCell(i.m)
  const out = [`apex ●${i.s.level}${lim === null ? '' : ` ${lim}`}`]
  if (i.m !== null && i.m.ctxPercent !== null) out.push(`ctx ${meter(i.m.ctxPercent)} ${Math.round(i.m.ctxPercent)}%`)
  if (i.m !== null && i.m.costUsd !== null) out.push(i.budgetUsd > 0 ? `${usd(i.m.costUsd)}/${i.budgetUsd}` : usd(i.m.costUsd))
  if (i.ds.length > 0 || i.b.lanes !== null) {
    let local = ''
    if (i.b.lanes !== null) {
      let n = 0
      let ok = 0
      let esc = 0
      for (const lane of Object.values(i.b.lanes)) {
        n += lane.n
        ok += lane.ok
        esc += lane.escalated
      }
      local = ` (local ${n} ✓${ok} ↑${esc})`
    }
    const differs = i.ds.filter(d => d.advised !== null && d.advised !== tierOf(d.resolved)).length
    out.push(`routes ${i.ds.length}${local}${differs > 0 ? ` ▲${differs}` : ''}`)
  }
  if (i.b.conformance !== null) out.push(`conf ${Math.round(100 * i.b.conformance)}%`)
  return out.join('  ')
}

export const statusText = (level: Level, costUsd: number | null): string => `apex ●${level}${costUsd === null ? '' : ` ${usd(costUsd)}`}`

/** §12: every drawing surface gets the status entry; `claude -p` (surface null) does not. */
export function refreshStatus(host: Host, rt: Runtime, m: Measure | null): void {
  if (rt.surface !== null) host.status(statusText(rt.level.level, m?.costUsd ?? null))
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
        <Button key="datapce-hide" label="Hide" plain onPress={() => update($, bandHidden, () => true)} />
      </Box>
    )
  })
}
