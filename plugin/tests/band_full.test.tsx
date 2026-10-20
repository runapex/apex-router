import { describe, expect, test } from 'claude-code/testing'

import type { Dispatch } from '../types/index.d.ts'
import { bandLine, statusText } from '../hooks/band.tsx'
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
    expect(line).toBe('apex AMBER · context 61% · $4.12/$10 · 2 of 7 subagents differ from advice')
  })

  test('the user must act: cause and action right after the level; the band fits 74 columns', () => {
    const m = (ctx: number, lim: number, cost: number) => ({ ctxPercent: ctx, limitKind: 'five_hour', limitPercent: lim, resetsAt: null, costUsd: cost })
    const ctx = bandLine({ m: m(88, 40, 6.4), s: EMPTY_SIGNALS, ds: [], b: EMPTY_BACKEND, budgetUsd: 0 })
    expect(ctx).toBe('apex GREEN · context 88% full: run /compact · 5h 40% · $6.40')
    const burn = bandLine({ m: m(45, 50, 9.1), s: { ...EMPTY_SIGNALS, level: 'AMBER', minutesToExhaust: 18 }, ds: [], b: EMPTY_BACKEND, budgetUsd: 10 })
    expect(burn).toBe('apex AMBER · $9.10/$10, empty in 18 min: slow down · 5h 50% · context 45%')
    expect(burn.length).toBeLessThanOrEqual(74)
    const red = bandLine({ m: m(61, 95, 4.12), s: { ...EMPTY_SIGNALS, level: 'RED' }, ds: [dispatch(null, 'claude-opus-5-5'), dispatch(null, 'claude-opus-5-5'), dispatch(null, 'claude-opus-5-5')], b: EMPTY_BACKEND, budgetUsd: 0 })
    expect(red).toBe('apex RED · 5h limit 95%: pause new subagents · context 61% · $4.12')
    // calm: no action words
    expect(bandLine({ m: m(30, 40, 1), s: EMPTY_SIGNALS, ds: [], b: EMPTY_BACKEND, budgetUsd: 10 })).toBe('apex GREEN · 5h 40% · context 30% · $1.00/$10')
  })

  test('no backend: no local, no conf', () => {
    expect(bandLine({ m: null, s: EMPTY_SIGNALS, ds: [dispatch(null, 'claude-opus-5-5')], b: EMPTY_BACKEND, budgetUsd: 0 })).toBe('apex GREEN · 1 subagent')
  })

  test('status text and token formatting', () => {
    expect(statusText('AMBER', 4.12)).toBe('apex AMBER · $4.12')
    expect(statusText('GREEN', null)).toBe('apex GREEN')
    expect(statusText('RED', 4.12, { kind: 'limit', cause: '5h limit 95%', action: 'pause new subagents' })).toBe('apex RED · 5h limit 95%: pause new subagents · $4.12')
    expect(statusText('AMBER', 9.1, { kind: 'budget', cause: '$9.10/$10', action: 'slow down' })).toBe('apex AMBER · $9.10/$10: slow down')
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
    expect((await ui.find({ key: 'datapce-band-line' }))?.text).toBe('apex RED · 5h limit 95%: pause new subagents · context 61% · $4.12')
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
    expect(world.statuses.at(-1)).toBe('apex RED · 5h limit 95%: pause new subagents · $4.12')
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
