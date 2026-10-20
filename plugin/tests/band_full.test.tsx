import { describe, expect, test } from 'claude-code/testing'

import type { Dispatch } from '../types/index.d.ts'
import { alertsOf, bandLine, bandParts, cols, statusText } from '../hooks/band.tsx'
import { fmtTok, handoffDue, HANDOFF_TEMPLATE } from '../hooks/handoff.ts'
import { EMPTY_BACKEND, EMPTY_SIGNALS } from '../hooks/state.ts'
import { BAND, MEASURE, SESSION, spawnInput, stepInput } from './fixtures/inputs.ts'
import { BACKEND, drain, worldOf } from './fixtures/world.ts'

const dispatch = (advised: Dispatch['advised'], resolved: string): Dispatch => ({
  toolUseId: 't', agentId: null, description: 'd', subagentType: 'Explore', taskType: 'explore', requested: 'inherit', resolved,
  advised, advisedState: advised === null ? null : 'READY', applied: false, basis: '', outcome: 'running', startedAt: 0,
  durationMs: null, tokens: null, level: 'GREEN',
})

describe('band line', () => {
  test('advice differs: a count with words, no action; long figures drop in order (match, then 5h < 80%)', () => {
    const line = bandLine({
      m: { ctxPercent: 61, limitKind: 'five_hour', limitPercent: 62, resetsAt: null, costUsd: 4.12 },
      s: { ...EMPTY_SIGNALS, level: 'AMBER' },
      ds: [
        dispatch('sonnet', 'claude-opus-5-5'), dispatch('haiku', 'claude-sonnet-5-5'), dispatch('sonnet', 'claude-sonnet-5-5'),
        dispatch(null, 'claude-opus-5-5'), dispatch(null, 'claude-opus-5-5'), dispatch(null, 'claude-opus-5-5'), dispatch(null, 'claude-opus-5-5'),
      ],
      b: { ...EMPTY_BACKEND, present: true, conformance: 0.86, lanes: { codegen: { n: 4, ok: 3, escalated: 1 } } },
      budgetUsd: 10,
    })
    expect(line).toBe('apex AMBER · 5h 62% · context 61% · $4.12/$10 · 2 of 7 differ from advice')
  })

  test('the user must act: cause and action right after the level; the band fits 74 columns', () => {
    const m = (ctx: number, lim: number, cost: number) => ({ ctxPercent: ctx, limitKind: 'five_hour', limitPercent: lim, resetsAt: null, costUsd: cost })
    const ctx = bandLine({ m: m(88, 40, 6.4), s: EMPTY_SIGNALS, ds: [], b: EMPTY_BACKEND, budgetUsd: 0 })
    expect(ctx).toBe('apex pressure GREEN · context 88% full · run /compact · 5h 40% · $6.40')
    const burn = bandLine({ m: m(45, 50, 9.1), s: { ...EMPTY_SIGNALS, level: 'AMBER', minutesToExhaust: 18 }, ds: [], b: EMPTY_BACKEND, budgetUsd: 10 })
    expect(burn).toBe('apex AMBER · $9.10/$10, empty ≈18 min · pause new subagents · context 45%')
    expect(burn.length).toBeLessThanOrEqual(74)
    const red = bandLine({ m: m(61, 95, 4.12), s: { ...EMPTY_SIGNALS, level: 'RED' }, ds: [dispatch(null, 'claude-opus-5-5'), dispatch(null, 'claude-opus-5-5'), dispatch(null, 'claude-opus-5-5')], b: EMPTY_BACKEND, budgetUsd: 0 })
    expect(red).toBe('apex RED · 5h limit 95% · pause new subagents · context 61% · $4.12')
    // calm: no action words
    expect(bandLine({ m: m(30, 40, 1), s: EMPTY_SIGNALS, ds: [], b: EMPTY_BACKEND, budgetUsd: 10 })).toBe('apex GREEN · 5h 40% · context 30% · $1.00/$10')
  })

  test('M2: RED names a quota cause only at 80% or more', () => {
    for (const limitPercent of [null, 0, 40, 79.9, 80, 95]) {
      const alerts = alertsOf({ m: { ctxPercent: 20, limitKind: 'five_hour', limitPercent, resetsAt: null, costUsd: 1 }, s: { ...EMPTY_SIGNALS, level: 'RED' }, budgetUsd: 0 })
      expect(alerts[0]?.cause).toBe(limitPercent !== null && limitPercent >= 80 ? `5h limit ${Math.round(limitPercent)}%` : 'pressure RED')
      expect(alerts[0]?.action).toBe('pause new subagents')
    }
  })

  test('M6: GREEN alerts say pressure GREEN, not all-clear', () => {
    for (const [ctxPercent, costUsd, budgetUsd] of [[88, 1, 0], [20, 9.1, 10]]) {
      const line = bandLine({ m: { ctxPercent, costUsd, limitKind: null, limitPercent: null, resetsAt: null }, s: EMPTY_SIGNALS, ds: [], b: EMPTY_BACKEND, budgetUsd })
      expect(line.startsWith('apex pressure GREEN · ')).toBe(true)
      expect(line).toContain(ctxPercent === 88 ? 'run /compact' : 'pause new subagents')
    }
  })

  test('M3: final width guard drops rank-99 figures and the pane names them', () => {
    const input = { m: { ctxPercent: 88, limitKind: 'five_hour', limitPercent: 95, resetsAt: null, costUsd: 123456.78 }, s: EMPTY_SIGNALS, ds: [], b: EMPTY_BACKEND, budgetUsd: 0 }
    const parts = bandParts(input)
    expect(parts.line).toBe('apex pressure GREEN · context 88% full · run /compact · 5h 95% · $123.5k')
    expect(parts.line.length).toBeLessThanOrEqual(74)
    expect(parts.dropped).toEqual([])
    const huge = bandParts({ ...input, m: { ...input.m, limitKind: 'quota'.repeat(20) }, ds: Array(12).fill(dispatch('haiku', 'claude-opus-5-5')) })
    expect(cols(huge.line)).toBeLessThanOrEqual(74)
    expect(huge.line.startsWith('apex pressure GREEN · context 88% full · run /compact')).toBe(true)
    expect(huge.dropped.length).toBeGreaterThan(0)
  })

  test('no backend: no local, no conf', () => {
    expect(bandLine({ m: null, s: EMPTY_SIGNALS, ds: [dispatch(null, 'claude-opus-5-5')], b: EMPTY_BACKEND, budgetUsd: 0 })).toBe('apex GREEN · 1 subagent')
  })

  test('a huge budget still fits 74 columns: short money, then a final cut', () => {
    const m = { ctxPercent: 40, limitKind: 'five_hour', limitPercent: 10, resetsAt: null, costUsd: 999_999 }
    const line = bandLine({ m, s: { ...EMPTY_SIGNALS, minutesToExhaust: 18 }, ds: [], b: EMPTY_BACKEND, budgetUsd: 1_000_000 })
    expect(line).toBe('apex pressure GREEN · $1M/$1M, empty ≈18 min · pause new subagents')
    expect(line.length).toBeLessThanOrEqual(74)
    for (const cost of [1e9, 123_456_789_012]) {
      const l = bandLine({ m: { ...m, costUsd: cost }, s: { ...EMPTY_SIGNALS, minutesToExhaust: 18 }, ds: [], b: EMPTY_BACKEND, budgetUsd: 1e12 })
      expect(l.length).toBeLessThanOrEqual(74)
      expect(l.endsWith(' · pause new subagents')).toBe(true)
    }
    // an unknown limit kind is untrusted: plain ASCII, short; the line fits in terminal columns
    const wide = bandLine({ m: { ...m, limitKind: '界'.repeat(40), limitPercent: 95 }, s: { ...EMPTY_SIGNALS, level: 'RED' }, ds: [], b: EMPTY_BACKEND, budgetUsd: 0 })
    expect(cols(wide)).toBeLessThanOrEqual(74)
    expect(wide.startsWith('apex RED · limit limit 95% · pause new subagents')).toBe(true)
    expect(cols('界a')).toBe(3)
    expect(bandLine({ m: { ...m, costUsd: 1e15 }, s: { ...EMPTY_SIGNALS, minutesToExhaust: 18 }, ds: [], b: EMPTY_BACKEND, budgetUsd: 1e15 })).toBe('apex pressure GREEN · $999T+/$999T+, empty ≈18 min · pause new subagents')
    // a cause too long even in short money is cut, the action stays whole
    const odd = bandLine({ m: { ...m, costUsd: 9e20 }, s: { ...EMPTY_SIGNALS, minutesToExhaust: 1e12 }, ds: [], b: EMPTY_BACKEND, budgetUsd: 9e20 })
    expect(odd.length).toBeLessThanOrEqual(74)
    expect(odd.endsWith(' · pause new subagents')).toBe(true)
  })

  test('a tiny or fractional budget never prints exponent notation', () => {
    const m = { ctxPercent: 40, limitKind: 'five_hour', limitPercent: 10, resetsAt: null, costUsd: 4.12 }
    const tiny = bandLine({ m, s: EMPTY_SIGNALS, ds: [], b: EMPTY_BACKEND, budgetUsd: 1e-7 })
    expect(tiny).not.toMatch(/e-?\d/)
    expect(tiny).toContain('$4.12/$0')
    expect(bandLine({ m: { ...m, costUsd: 1 }, s: EMPTY_SIGNALS, ds: [], b: EMPTY_BACKEND, budgetUsd: 12.5 })).toContain('$1.00/$12.5')
    // above $999T the figure stops growing: no exponent, ever
    for (const x of [1e15, 1e21, 1e33, Number.MAX_VALUE]) {
      const l = bandLine({ m: { ...m, costUsd: x }, s: EMPTY_SIGNALS, ds: [], b: EMPTY_BACKEND, budgetUsd: x })
      expect(l).not.toMatch(/e[+-]?\d/)
      expect(statusText('GREEN', x)).not.toMatch(/e[+-]?\d/)
    }
  })

  test('status text and token formatting', () => {
    expect(statusText('AMBER', 4.12)).toBe('apex AMBER · $4.12')
    expect(statusText('GREEN', null)).toBe('apex GREEN')
    expect(statusText('RED', 4.12, { kind: 'limit', cause: '5h limit 95%', action: 'pause new subagents' })).toBe('apex RED · 5h limit 95% · pause new subagents · $4.12')
    expect(statusText('AMBER', 9.1, { kind: 'budget', cause: '$9.10/$10', action: 'pause new subagents' })).toBe('apex AMBER · $9.10/$10 · pause new subagents')
    expect(fmtTok(43_860_328)).toBe('43.9M')
    expect(fmtTok(1000)).toBe('1k')
    expect(handoffDue(10, null)).toBe(false)
    expect(handoffDue(999, 1000)).toBe(false)
    expect(handoffDue(1000, 1000)).toBe(true)
  })

  test('the handoff template carries all six fields', () => {
    for (const f of ['goal', 'constraints', 'decisions', 'files_touched', 'open_issues', 'next_action']) expect(HANDOFF_TEMPLATE).toContain(`- **${f}**: _…_`)
  })
})

describe('band, status, handoff: hooks', () => {
  test('pressure reaches the band through the live tick', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.session.measure({ ...MEASURE, rateLimits: [{ kind: 'five_hour', percentUsed: 95, resetsAt: '2026-10-03T20:00:00Z' }] })
    await world.clock.advance(120_000)
    const ui = await $.ui.mount({ plugin: 'datapce', surface: 'terminal', ...BAND })
    expect((await ui.find({ key: 'datapce-band-line' }))?.text).toBe('apex RED · 5h limit 95% · pause new subagents · context 61% · $4.12')
    await ui.unmount()
  })

  test('a dispatch alone shows the band', async ($, on) => {
    worldOf(on)
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput())
    const ui = await $.ui.mount({ plugin: 'datapce', surface: 'desktop', ...BAND })
    expect((await ui.find({ key: 'datapce-band-line' }))?.text).toBe('apex GREEN · 1 subagent')
    await ui.unmount()
  })

  for (const surface of ['vscode', 'mobile'] as const) {
    test(`status where no band draws (${surface})`, async ($, on) => {
      const world = worldOf(on)
      await $.session.start({ ...SESSION, surface })
      await $.session.measure(MEASURE)
      expect(world.statuses).toEqual(['apex GREEN · $4.12'])
    })
  }

  test('status where no band draws carries the action when the user must act', async ($, on) => {
    const world = worldOf(on)
    await $.session.start({ ...SESSION, surface: 'vscode' })
    await $.session.measure({ ...MEASURE, rateLimits: [{ kind: 'five_hour', percentUsed: 95, resetsAt: '2026-10-03T20:00:00Z' }] })
    await world.clock.advance(120_000)
    expect(world.statuses.at(-1)).toBe('apex RED · 5h limit 95% · pause new subagents · $4.12')
  })

  test('M8: no-band status carries the first GREEN alert cause and action', { options: { budgetUsd: 10 } }, async ($, on) => {
    const world = worldOf(on)
    await $.session.start({ ...SESSION, surface: 'mobile' })
    await $.session.measure({ ...MEASURE, context: { ...MEASURE.context, percent: 88 }, cost: { usd: 9.1 } } as never)
    expect(world.statuses.at(-1)).toBe('apex pressure GREEN · $9.10/$10 · pause new subagents')
    await $.session.measure({ ...MEASURE, context: { ...MEASURE.context, percent: 88 } })
    expect(world.statuses.at(-1)).toBe('apex pressure GREEN · context 88% full · run /compact · $4.12')
  })

  for (const surface of ['terminal', 'desktop'] as const) {
    test(`${surface}: the band draws, so the status entry is cleared, never set`, async ($, on) => {
      const world = worldOf(on)
      await $.session.start({ ...SESSION, surface })
      await $.session.measure(MEASURE)
      expect(world.statuses).toEqual([undefined])
    })
  }

  test('band: false gets the status instead', { options: { band: false } }, async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.session.measure(MEASURE)
    expect(world.statuses).toEqual(['apex GREEN · $4.12'])
  })

  test('Hide hands the figures to the status entry at once, and they stay', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.session.measure(MEASURE)
    const ui = await $.ui.mount({ plugin: 'datapce', surface: 'terminal', ...BAND })
    await ui.press({ key: 'datapce-hide' })
    await ui.unmount()
    expect(world.statuses.at(-1)).toBe('apex GREEN · $4.12')
    await $.session.measure(MEASURE)
    expect(world.statuses.at(-1)).toBe('apex GREEN · $4.12')
  })

  test('API-key session: only the context cell and the level; budget stays off', { options: { budgetUsd: 0 } }, async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.session.measure({ context: { window: 200000, tokens: 20000, percent: 10 }, rateLimits: [], changed: ['context'] })
    const ui = await $.ui.mount({ plugin: 'datapce', surface: 'terminal', ...BAND })
    expect((await ui.find({ key: 'datapce-band-line' }))?.text).toBe('apex GREEN · context 10%')
    await ui.unmount()
    expect(world.statuses).toEqual([undefined])
  })

  test('band line never carries an enforce cell', () => {
    expect(bandLine({ m: null, s: EMPTY_SIGNALS, ds: [], b: EMPTY_BACKEND, budgetUsd: 0 })).not.toContain('enforce')
  })

  test('claude -p (no surface) gets no status', async ($, on) => {
    const world = worldOf(on)
    await $.session.start({ cwd: '/work', surface: null, isInteractive: false })
    await $.session.measure(MEASURE)
    expect(world.statuses).toEqual([])
  })

  test('crossing the backend handoff threshold toasts the handoff', async ($, on) => {
    const world = worldOf(on, { [`${BACKEND}/pressure.json`]: '{"families": {}}', [`${BACKEND}/handoff_threshold.json`]: '{"threshold_tokens": 1000}' })
    await $.session.start(SESSION)
    await new Promise(r => setTimeout(r, 0)) // the first backend poll finishes in the background
    await drain($.turn.step(stepInput()))
    await $.session.measure(MEASURE)
    expect(world.toasts).toEqual(["this session's cache reads passed 1k tokens (your handoff threshold) — run /apex handoff, fill the block, start fresh"])
  })
})
