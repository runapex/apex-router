import { describe, expect, test } from 'claude-code/testing'

import type { Measure } from '../types/index.d.ts'
import { tick } from '../hooks/live.ts'
import { newRuntime, optionsOf } from '../hooks/runtime.ts'
import { EMPTY_SIGNALS } from '../hooks/state.ts'
import { close } from './fixtures/close.ts'
import { MEASURE, SESSION } from './fixtures/inputs.ts'
import { BACKEND, T0, worldOf } from './fixtures/world.ts'

const M = (limitPercent: number | null, costUsd: number | null = null): Measure => ({
  ctxPercent: 10, limitKind: limitPercent === null ? null : 'five_hour', limitPercent, resetsAt: '2026-10-03T20:00:00Z', costUsd,
})
const rtAt = (raw = {}) => {
  const rt = newRuntime(optionsOf(raw))
  rt.startedAt = T0
  return rt
}

const END = { reason: 'other', sessionId: 'sess-0001' } as never

describe('live: tick', () => {
  test('rate-limit pressure: RED after two windows, one toast', () => {
    const rt = rtAt()
    let v = tick(rt, T0 + 60_000, M(95), EMPTY_SIGNALS)
    expect(v.view.level).toBe('GREEN')
    expect(v.toasts).toEqual([])
    v = tick(rt, T0 + 120_000, M(95), v.view)
    expect(v.view.level).toBe('RED')
    expect(v.toasts).toEqual([['red', 'datapce: pressure RED — serialize heavy fan-out']])
    expect(rt.profileDirty).toBe(true)
  })

  test('no rate limits (API key): level from burn and backend only, budget off', () => {
    const rt = rtAt()
    const first = tick(rt, T0 + 60_000, M(null), EMPTY_SIGNALS).view
    expect(first.level).toBe('GREEN')
    expect([first.budgetBurn, first.budgetBurnShort, first.budgetBurnLong, first.minutesToExhaust]).toEqual([null, null, null, null])
    expect(first.admissionRate).toBe(10)
    rt.backend = { ...rt.backend, families: { opus: 'AMBER' } }
    tick(rt, T0 + 120_000, M(null), EMPTY_SIGNALS)
    expect(tick(rt, T0 + 180_000, M(null), EMPTY_SIGNALS).view.level).toBe('AMBER')
  })

  test('no measure at all behaves like no rate limits', () => {
    const v = tick(rtAt(), T0 + 60_000, null, EMPTY_SIGNALS).view
    expect(v.level).toBe('GREEN')
    expect(v.budgetBurn).toBeNull()
  })

  test('three lane failures open the breaker and toast', () => {
    const rt = rtAt()
    for (let i = 0; i < 3; i++) rt.bashEvents.push({ cmd: 'ornith', signal: 'OrnithBusy', isError: true, t: T0 + i })
    const v = tick(rt, T0 + 60_000, M(10), EMPTY_SIGNALS)
    expect(v.view.breakers).toEqual({ ornith: 'open' })
    expect(v.toasts).toContainEqual(['breaker:ornith', 'datapce: ornith lane breaker open — escalate for 10 min'])
  })

  test('a failure while the breaker is open does not extend the cooldown', () => {
    const rt = rtAt()
    for (let i = 0; i < 3; i++) rt.bashEvents.push({ cmd: 'ornith', signal: null, isError: true, t: T0 + i })
    rt.bashEvents.push({ cmd: 'ornith', signal: 'OrnithBusy', isError: true, t: T0 + 300_000 })
    const v = tick(rt, T0 + 660_000, M(10), EMPTY_SIGNALS)
    expect(rt.breakers.ornith!.openedAt).toBe(T0 + 2)
    expect(v.view.breakers).toEqual({ ornith: 'half-open' })
  })

  test('half-open: one probe decides; later failures in the same tick are not extra probes', () => {
    const rt = rtAt()
    for (let i = 0; i < 3; i++) rt.bashEvents.push({ cmd: 'ornith', signal: null, isError: true, t: T0 + i })
    tick(rt, T0 + 60_000, M(10), EMPTY_SIGNALS)
    const probe = T0 + 700_000
    rt.bashEvents.push({ cmd: 'ornith', signal: null, isError: true, t: probe }, { cmd: 'ornith', signal: null, isError: true, t: probe + 1000 })
    const v = tick(rt, probe + 60_000, M(10), EMPTY_SIGNALS)
    expect(rt.breakers.ornith!.openedAt).toBe(probe)
    expect(v.view.breakers).toEqual({ ornith: 'open' })
  })

  test('budget burn over both windows toasts', () => {
    const rt = rtAt({ budgetUsd: 10 })
    let v = { view: EMPTY_SIGNALS, toasts: [] as [string, string][] }
    for (let i = 1; i <= 5; i++) {
      rt.minute.spend = 0.5
      v = tick(rt, T0 + i * 60_000, M(10, 0.5 * i), v.view)
    }
    expect(v.view.budgetBurnShort!).toBeGreaterThan(10)
    expect(v.view.budgetBurnLong!).toBeGreaterThan(6)
    expect(v.toasts).toContainEqual(['budget', 'datapce: budget burning 72× (5 min) / 72× (60 min)'])
    expect(v.view.minutesToExhaust).not.toBeNull()
    expect(v.view.minutesToExhaust!).toBeGreaterThanOrEqual(0)
  })

  test('after a reload the whole-session burn covers spend since identify, not the whole process', () => {
    // identify ran at T0 on a fresh runtime: the engine's cost already holds $9 spent before the reload
    const rt = rtAt({ budgetUsd: 10 })
    let v = tick(rt, T0 + 60_000, M(10, 9), EMPTY_SIGNALS).view
    expect(v.budgetBurn).toBe(0)
    v = tick(rt, T0 + 10 * 60_000, M(10, 9.1), v).view
    // $0.10 over 10 min against $10/day ≈ 1.44×; the whole $9.10 over 10 min would be ≈ 131×
    close(v.budgetBurn, (0.1 / 10) / (10 / 1440))
  })

  test('after a hot reload (startedAt 0) the whole-session burn is unknown, not ~0', () => {
    const rt = rtAt({ budgetUsd: 10 })
    rt.startedAt = 0
    rt.minute.spend = 0.5
    const v = tick(rt, T0 + 60_000, M(10, 5), EMPTY_SIGNALS).view
    expect(v.budgetBurn).toBeNull()
    expect(v.minutesToExhaust).toBeNull()
  })

  test('the 60 min burn needs 5 closed minutes: early spend neither shows nor toasts', () => {
    const rt = rtAt({ budgetUsd: 10 })
    let v = { view: EMPTY_SIGNALS, toasts: [] as [string, string][] }
    for (let i = 1; i <= 4; i++) {
      rt.minute.spend = 0.5
      v = tick(rt, T0 + i * 60_000, M(10, 0.5 * i), v.view)
      expect(v.view.budgetBurnLong).toBeNull()
      expect(v.toasts.filter(([k]) => k === 'budget')).toEqual([])
    }
    expect(v.view.budgetBurnShort!).toBeGreaterThan(10)
  })

  test('an unparseable resetsAt is unknown, never a NaN admission rate', () => {
    const rt = rtAt()
    rt.limitHistory = [{ t: T0, pct: 40 }, { t: T0 + 600_000, pct: 50 }]
    rt.heavySpawns = [1, 2, 3, 4, 5].map(i => T0 + i * 60_000)
    const bad: Measure = { ...M(50), resetsAt: 'not a date' }
    const v = tick(rt, T0 + 600_000, bad, EMPTY_SIGNALS).view
    expect(Number.isFinite(v.admissionRate)).toBe(true)
    expect(v.admissionRate).toBe(10)
  })

  test('admission rate follows the rate-limit slope', () => {
    const rt = rtAt()
    rt.limitHistory = [{ t: T0, pct: 40 }, { t: T0 + 600_000, pct: 50 }]
    rt.heavySpawns = [1, 2, 3, 4, 5].map(i => T0 + i * 60_000)
    const v = tick(rt, T0 + 600_000, M(50), EMPTY_SIGNALS)
    expect(v.view.admissionRate).toBe(0.2)
    expect(rt.bucket.rate).toBe(0.2)
  })

  test('no anomaly card: step features are left alone and the view never carries one', () => {
    const rt = rtAt()
    rt.stepFeatures.push([1, 2, 3, 4, 5])
    const v = tick(rt, T0 + 60_000, M(10), EMPTY_SIGNALS).view
    expect(v.anomaly).toBeNull()
    expect(rt.anomaly).toBeNull()
  })
})

describe('live: settling', () => {
  test('a level held by the exit streak reports the observed level and the windows left', () => {
    const rt = rtAt()
    let v = tick(rt, T0 + 60_000, M(75), EMPTY_SIGNALS).view
    v = tick(rt, T0 + 120_000, M(75), v).view
    expect([v.level, v.observed, v.exitIn]).toEqual(['AMBER', 'AMBER', null])
    v = tick(rt, T0 + 180_000, M(10), v).view
    expect([v.level, v.observed, v.exitIn]).toEqual(['AMBER', 'GREEN', 2])
    v = tick(rt, T0 + 240_000, M(10), v).view
    expect([v.level, v.observed, v.exitIn]).toEqual(['AMBER', 'GREEN', 1])
    v = tick(rt, T0 + 300_000, M(10), v).view
    expect([v.level, v.observed, v.exitIn]).toEqual(['GREEN', 'GREEN', null])
  })
})

describe('live: hooks', () => {
  test('the status entry follows the level on the same tick that publishes it', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.session.measure({ ...MEASURE, rateLimits: [{ kind: 'five_hour', percentUsed: 75, resetsAt: '2026-10-03T20:00:00Z' }] })
    expect(world.statuses.at(-1)).toMatch(/^apex ●GREEN/)
    await world.clock.advance(60_000)
    expect(world.statuses.at(-1)).toMatch(/^apex ●GREEN/)
    await world.clock.advance(60_000)
    expect(world.statuses.at(-1)).toMatch(/^apex ●AMBER/)
  })

  test('a RED rate limit toasts once from the timer', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.session.measure({ ...MEASURE, rateLimits: [{ kind: 'five_hour', percentUsed: 95, resetsAt: '2026-10-03T20:00:00Z' }] })
    await world.clock.advance(60_000)
    expect(world.toasts).toEqual([])
    await world.clock.advance(60_000)
    expect(world.toasts).toEqual(['datapce: pressure RED — serialize heavy fan-out'])
    await world.clock.advance(180_000)
    expect(world.toasts).toEqual(['datapce: pressure RED — serialize heavy fan-out'])
  })

  test('nothing is persisted as an anomaly model', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await world.clock.advance(11 * 60_000)
    expect(world.store.has('datapce.anomaly')).toBe(false)
  })

  test('a flush after a hot reload (no session.start) writes nothing, least of all a relative path', async ($, on) => {
    const world = worldOf(on)
    await $.session.measure(MEASURE)
    await $.session.end(END)
    expect([...world.appended.keys()]).toEqual([])
    expect([...world.files.keys()]).toEqual([])
    expect(world.runs.filter(argv => argv.some(a => a.includes('~')))).toEqual([])
  })

  test('after session.start the same flush lands under the resolved directory', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.session.measure(MEASURE)
    await $.session.end(END)
    expect([...world.appended.keys()].every(p => p.startsWith(`${BACKEND}/`))).toBe(true)
    expect(world.appended.size).toBeGreaterThan(0)
  })
})

describe('live: unresolved backendDir', () => {
  test('a fresh runtime has no directory until identify resolves it', () => {
    expect(newRuntime(optionsOf({})).backendDir).toBe('')
  })
})
