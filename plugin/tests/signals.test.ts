import { describe, expect, test } from 'claude-code/testing'

import type { Level, LevelState } from '../types/index.d.ts'
import {
  admissionRate, availability, BREAKER_COOL_MS, BREAKER_THRESHOLD, breakerNew, breakerRecord, breakerState, bucketNew, bucketTake, burnAlert, drainEta,
  fanoutRisk, levelStep, limitLevel, LONG_LIMIT, LONG_MIN, routingBurn, series, SHORT_LIMIT, SHORT_MIN, SLO_429, SLO_TRANSPORT, spendBurn, worst,
} from '../hooks/signals.ts'
import { close } from './fixtures/close.ts'

describe('§7 signals against their reference numbers', () => {
  test('the exported constants are the spec numbers', () => {
    expect([SLO_429, SLO_TRANSPORT, SHORT_MIN, LONG_MIN, SHORT_LIMIT, LONG_LIMIT, BREAKER_THRESHOLD, BREAKER_COOL_MS]).toEqual([
      0.98, 0.97, 5, 60, 10, 6, 3, 600_000,
    ])
  })

  test('default arguments alert strictly above both limits (burn exactly 10 and 6 does not)', () => {
    // slo 0.97: burn = bad/total/0.03. 30% bad = 10.0 over both windows → not > 10.
    const at = Array.from({ length: 60 }, () => [100, 30] as const)
    expect(burnAlert(at, SLO_TRANSPORT).alert).toBe(false)
    const above = Array.from({ length: 60 }, () => [100, 31] as const)
    expect(burnAlert(above, SLO_TRANSPORT).alert).toBe(true)
  })

  test('multi-window burn rate (True, 11.5, 7.0)', () => {
    const b = burnAlert([[1000, 1], [1000, 1], [1000, 10], [1000, 12], [1000, 11]], 0.999, 2, 5)
    expect(b.alert).toBe(true)
    expect(b.short!.toFixed(2)).toBe('11.50')
    expect(b.long!.toFixed(2)).toBe('7.00')
  })

  test('no traffic is no evidence, never an alert', () => {
    expect(burnAlert([], 0.97)).toEqual({ alert: false, short: null, long: null })
    expect(burnAlert([[0, 0], [0, 0]], 0.97)).toEqual({ alert: false, short: null, long: null })
  })

  test('spend burn = spend rate over budget/1440', () => {
    close(spendBurn(Array.from({ length: 5 }, () => 10 / 720), 10, 5), 2.0)
    expect(spendBurn([1, 2], 0, 5)).toBeNull()
    expect(spendBurn([], 10, 5)).toBeNull()
  })

  test('level hysteresis: enter after 2 windows, exit after 3', () => {
    let s: LevelState = { level: 'GREEN', up: 0, down: 0 }
    const seen: Level[] = []
    for (const observed of ['AMBER', 'AMBER', 'GREEN', 'GREEN', 'GREEN', 'RED', 'GREEN', 'RED', 'RED'] as Level[]) {
      s = levelStep(s, observed)
      seen.push(s.level)
    }
    expect(seen).toEqual(['GREEN', 'AMBER', 'AMBER', 'AMBER', 'GREEN', 'GREEN', 'GREEN', 'GREEN', 'RED'])
  })

  test('worst level and rate-limit level', () => {
    expect(worst([])).toBe('GREEN')
    expect(worst(['GREEN', 'RED', 'AMBER'])).toBe('RED')
    expect([null, 50, 70, 89.9, 90].map(limitLevel)).toEqual(['GREEN', 'GREEN', 'AMBER', 'AMBER', 'RED'])
  })

  test('lane breaker opens at 3, fast-fails for 10 minutes, half-open probe closes it', () => {
    let b = breakerNew()
    const out: string[] = []
    for (const t of [0, 2, 4, 6, 8, 10, 12, 14]) {
      const now = t * 60_000
      if (breakerState(b, now) === 'open') {
        out.push('FAST-FAIL')
        continue
      }
      const up = t >= 12
      b = breakerRecord(b, now, up)
      out.push(up ? 'ok' : 'error')
    }
    expect(out).toEqual(['error', 'error', 'error', 'FAST-FAIL', 'FAST-FAIL', 'FAST-FAIL', 'FAST-FAIL', 'ok'])
    expect(breakerState(b, 15 * 60_000)).toBe('closed')
  })

  test('token bucket allows 10 of 15, then 5 of 15 a second later', () => {
    let b = bucketNew(5, 10)
    let allowed = 0
    for (let i = 0; i < 15; i++) {
      const r = bucketTake(b, 0)
      b = r.bucket
      if (r.allowed) allowed++
    }
    expect(allowed).toBe(10)
    allowed = 0
    for (let i = 0; i < 15; i++) {
      const r = bucketTake(b, 1)
      b = r.bucket
      if (r.allowed) allowed++
    }
    expect(allowed).toBe(5)
  })

  test('admission rate scales the heavy-spawn pace by sustainable/observed burn', () => {
    close(admissionRate(2, 1.0, 40, 100), 0.8)
    expect(admissionRate(2, 0, 40, 100)).toBe(10)
    expect(admissionRate(0, 1.0, 40, 100)).toBe(0.2)
    expect(admissionRate(100, 0.01, 90, 10)).toBe(10)
  })

  test('fan-out tail, availability, series, drain ETA, routing burn', () => {
    expect(fanoutRisk(0.01, 10).toFixed(3)).toBe('0.096')
    expect(fanoutRisk(0.01, 100).toFixed(3)).toBe('0.634')
    expect(availability(720, 0.5).toFixed(5)).toBe('0.99931')
    expect(series(0.9999, 0.999, 0.999).toFixed(5)).toBe('0.99790')
    expect(Math.round(drainEta(135000, 2200, 1850)!)).toBe(386)
    expect(drainEta(10, 5, 6)).toBeNull()
    close(routingBurn(34, 36), (1 - 34 / 36) / 0.1)
    expect(routingBurn(0, 0)).toBeNull()
  })
})
