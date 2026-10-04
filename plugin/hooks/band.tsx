import type { On, SessionMeasureInput } from 'claude-code'
import { atom, read, update } from 'claude-code'

import type { Measure } from '../types/index.d.ts'
import type { Runtime } from './runtime.ts'

const measure = atom({ plugin: 'datapce', key: 'measure' } as const, null as Measure | null)
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

export function measureCells(m: Measure): string[] {
  const out: string[] = []
  if (m.limitPercent !== null) out.push(`${LIMIT_LABEL[m.limitKind ?? ''] ?? m.limitKind}:${Math.round(m.limitPercent)}%`)
  if (m.ctxPercent !== null) out.push(`ctx ${meter(m.ctxPercent)} ${Math.round(m.ctxPercent)}%`)
  if (m.costUsd !== null) out.push(usd(m.costUsd))
  return out
}

export function install(on: On, rt: Runtime): void {
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!rt.options.band || e.props.hasSurvey) return next(e)
    const m = await read($, measure)
    if (m === null || (await read($, bandHidden))) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const line = ['apex', ...measureCells(m)].join('  ')
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
