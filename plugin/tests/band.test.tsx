import { describe, expect, test } from 'claude-code/testing'

import { measureCells, measureOf, meter } from '../hooks/band.tsx'
import { optionsOf } from '../hooks/runtime.ts'
import { BAND, MEASURE, SESSION } from './fixtures/inputs.ts'
import { worldOf } from './fixtures/world.ts'

const SURFACES = ['terminal', 'desktop'] as const

describe('options', () => {
  test('userConfig defaults; malformed values fall back', () => {
    const d = { pane: 'command', enforce: false, budgetUsd: 0, band: true, backendDir: '~/.apex-router' }
    expect(optionsOf({})).toEqual(d)
    expect(optionsOf({ pane: 'nope', budgetUsd: -3, backendDir: '  ' })).toEqual(d)
    expect(optionsOf({ pane: 'auto', enforce: true, budgetUsd: 10, band: false, backendDir: '/x' })).toEqual({
      pane: 'auto', enforce: true, budgetUsd: 10, band: false, backendDir: '/x',
    })
  })
})

describe('band from session.measure', () => {
  test('meter clamps and rounds', () => {
    expect(meter(61)).toBe('▇▇▇▇▇░░░')
    expect(meter(0)).toBe('░░░░░░░░')
    expect(meter(140)).toBe('▇▇▇▇▇▇▇▇')
  })

  test('measureOf keeps the tightest rate-limit window', () => {
    const m = measureOf(MEASURE)
    expect(m).toEqual({ ctxPercent: 61, limitKind: 'five_hour', limitPercent: 62, resetsAt: '2026-10-03T20:00:00Z', costUsd: 4.12 })
    expect(measureCells(m)).toEqual(['5h:62%', 'ctx ▇▇▇▇▇░░░ 61%', '$4.12'])
  })

  test('API-key sessions (no rate limits, no cost) draw only what they have', () => {
    const m = measureOf({ context: { window: 200000, tokens: 20000, percent: 10 }, rateLimits: [], changed: ['context'] })
    expect(m).toEqual({ ctxPercent: 10, limitKind: null, limitPercent: null, resetsAt: null, costUsd: null })
    expect(measureCells(m)).toEqual(['ctx ▇░░░░░░░ 10%'])
  })

  test('nothing draws before the first measurement', async ($, on) => {
    worldOf(on)
    await $.session.start(SESSION)
    for (const surface of SURFACES) {
      const ui = await $.ui.mount({ plugin: 'datapce', surface, ...BAND })
      expect(await ui.find({ key: 'datapce-band-line' })).toBeUndefined()
      await ui.unmount()
    }
  })

  test('draws on terminal and desktop once measured; Hide hides it', async ($, on) => {
    worldOf(on)
    await $.session.start(SESSION)
    await $.session.measure(MEASURE)
    for (const surface of SURFACES) {
      const ui = await $.ui.mount({ plugin: 'datapce', surface, ...BAND })
      expect((await ui.find({ key: 'datapce-band-line' }))?.text).toBe('apex  5h:62%  ctx ▇▇▇▇▇░░░ 61%  $4.12')
      await ui.unmount()
    }
    const ui = await $.ui.mount({ plugin: 'datapce', surface: 'terminal', ...BAND })
    await ui.press({ key: 'datapce-hide' })
    expect(await ui.find({ key: 'datapce-band-line' })).toBeUndefined()
    await ui.unmount()
  })

  test('a survey holds the band', async ($, on) => {
    worldOf(on)
    await $.session.start(SESSION)
    await $.session.measure(MEASURE)
    const ui = await $.ui.mount({ plugin: 'datapce', surface: 'terminal', ...BAND, props: { ...BAND.props, hasSurvey: true } })
    expect(await ui.find({ key: 'datapce-band-line' })).toBeUndefined()
    await ui.unmount()
  })

  test('band: false never draws', { options: { band: false } }, async ($, on) => {
    worldOf(on)
    await $.session.start(SESSION)
    await $.session.measure(MEASURE)
    const ui = await $.ui.mount({ plugin: 'datapce', surface: 'terminal', ...BAND })
    expect(await ui.find({ key: 'datapce-band-line' })).toBeUndefined()
    await ui.unmount()
  })
})
