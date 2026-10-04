import { describe, expect, test } from 'claude-code/testing'

import {
  conformanceRate, drainEtaOf, healthOf, laneStatsOf, parseHandoff, parsePressure, parseVerdicts, viewOf,
} from '../hooks/backend.ts'
import { resolveBackendDir } from '../hooks/runtime.ts'
import { SESSION } from './fixtures/inputs.ts'
import { BACKEND, HOME, T0, worldOf } from './fixtures/world.ts'

const PRESSURE = JSON.stringify({
  families: {
    fable: { level: 'GREEN', requests: 25 },
    opus: { level: 'AMBER', requests: 353, transport_rate: 0.0312 },
    bogus: { level: 'PURPLE' },
  },
})

describe('backend: parsing', () => {
  test('pressure families keep valid levels only', () => {
    expect(parsePressure(JSON.parse(PRESSURE))).toEqual({ fable: 'GREEN', opus: 'AMBER' })
    expect(parsePressure('garbage')).toEqual({})
  })

  test('conformance rate over rows that carry a boolean matched', () => {
    expect(conformanceRate([{ matched: true }, { matched: false }, { matched: true }, { note: 'x' }])).toBe(2 / 3)
    expect(conformanceRate([])).toBeNull()
  })

  test('route-advise verdicts and the handoff threshold', () => {
    expect(parseVerdicts({ explore: { verdict: 'cost_favors_cheap_start' }, review: { verdict: 3 } })).toEqual({ explore: 'cost_favors_cheap_start' })
    expect(parseHandoff({ threshold_tokens: 43860328 })).toBe(43860328)
    expect(parseHandoff({})).toBeNull()
  })

  test('lane stats over the last 24 h', () => {
    const now = T0 / 1000
    const rows = [
      { ts: now - 60, lane: 'codegen', ok: true, escalated: false },
      { ts: now - 120, lane: 'codegen', ok: false, escalated: true },
      { ts: now - 90000, lane: 'codegen', ok: true, escalated: false },
      { ts: now - 30, lane: 'preread', ok: true, escalated: true },
    ]
    expect(laneStatsOf(rows, now)).toEqual({ codegen: { n: 2, ok: 1, escalated: 1 }, preread: { n: 1, ok: 1, escalated: 1 } })
    expect(laneStatsOf([], now)).toBeNull()
  })

  test('drain ETA = backlog / (cap − arrive)', () => {
    const shrinking = [{ t: T0 - 3_600_000, inbox: 160 }, { t: T0, inbox: 100 }]
    expect(drainEtaOf(shrinking, 120, T0)).toBe(6000)
    const growing = [{ t: T0 - 3_600_000, inbox: 100 }, { t: T0, inbox: 160 }]
    expect(drainEtaOf(growing, 120, T0)).toBeNull()
    expect(drainEtaOf([{ t: T0, inbox: 0 }], 0, T0)).toBeNull()
  })

  test('health is the series product of per-component availability', () => {
    expect(healthOf({ proxy: [true, true, true, false], worker: [true, true] })).toBe(0.75)
    expect(healthOf({})).toBeNull()
  })

  test('the view keeps absent things absent', () => {
    expect(viewOf({ present: false, families: {}, conformance: null, verdicts: {}, handoff: null, lanes: null, drainEtaS: null, health: null })).toEqual({
      present: false, conformance: null, families: {}, lanes: null, drainEtaS: null, health: null, verdicts: {}, handoffTokens: null,
    })
  })
})

describe('backend: hooks', () => {
  test('no backend: nothing is run, the profile says absent', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await world.clock.advance(5000)
    expect(world.runs.some(argv => argv.some(a => a.endsWith('apex-router')))).toBe(false)
    expect((world.store.get('datapce.profile') as { backend: boolean }).backend).toBe(false)
  })

  test('backend present: pressure each minute, route-advise every 10 minutes, files read', async ($, on) => {
    const bin = `${BACKEND}/.venv/bin/apex-router`
    const world = worldOf(on, {
      [`${BACKEND}/pressure.json`]: PRESSURE,
      [`${BACKEND}/conformance.jsonl`]: '{"matched": true}\n{"matched": false}\n',
      [`${BACKEND}/handoff_threshold.json`]: '{"threshold_tokens": 1000}',
      [`${HOME}/.apex/offload_telemetry.jsonl`]: `{"ts": ${T0 / 1000 - 10}, "lane": "codegen", "ok": true, "escalated": false}\n`,
      [bin]: '#!/bin/sh\n',
    })
    world.answer = argv => {
      if (argv[0] !== bin) return null
      if (argv[1] === 'pressure') return { exitCode: 0, stdout: PRESSURE }
      if (argv[1] === 'route-advise') return { exitCode: 0, stdout: '{"explore": {"verdict": "hold"}}' }
      return null
    }
    await $.session.start(SESSION)
    await world.clock.advance(5000)
    const calls = () => world.runs.filter(argv => argv[0] === bin).map(argv => argv.slice(1).join(' '))
    expect(calls()).toEqual(['pressure --json --no-write', 'route-advise --json'])
    await world.clock.advance(60_000)
    expect(calls()).toEqual(['pressure --json --no-write', 'route-advise --json', 'pressure --json --no-write'])
    expect((world.store.get('datapce.profile') as { backend: boolean }).backend).toBe(true)
    expect(world.asked).toContain(`read ${BACKEND}/handoff_threshold.json`)
  })

  test('a failing backend command falls back to the files and never throws', async ($, on) => {
    const bin = `${HOME}/.local/bin/apex-router`
    const world = worldOf(on, { [`${BACKEND}/pressure.json`]: PRESSURE, [bin]: '#!/bin/sh\n' })
    world.answer = argv => (argv[0] === bin ? { exitCode: 2, stdout: 'boom' } : null)
    await $.session.start(SESSION)
    await world.clock.advance(60_000)
    expect(world.asked).toContain(`read ${BACKEND}/pressure.json`)
  })
})

type View = { present: boolean; families: Record<string, string> }

describe('backend: freshness, no-backend, path rules', () => {
  const stale = JSON.stringify({ generated_at: T0 / 1000 - 3600, families: { opus: { level: 'RED' } } })
  const fresh = JSON.stringify({ generated_at: T0 / 1000 - 30, families: { opus: { level: 'AMBER' } } })

  test('a stale pressure.json is unknown, not an error and not stale levels', async ($, on) => {
    const world = worldOf(on, { [`${BACKEND}/pressure.json`]: stale })
    await $.session.start(SESSION)
    await world.clock.advance(5000)
    const view = world.published.get('backend') as View
    expect(view.present).toBe(true)
    expect(view.families).toEqual({})
  })

  test('a fresh pressure.json is used when the command is unavailable', async ($, on) => {
    const world = worldOf(on, { [`${BACKEND}/pressure.json`]: fresh })
    await $.session.start(SESSION)
    await world.clock.advance(5000)
    expect((world.published.get('backend') as View).families).toEqual({ opus: 'AMBER' })
  })

  test('a future-dated pressure.json (clock skew beyond 60 s) is unknown', async ($, on) => {
    const future = JSON.stringify({ generated_at: T0 / 1000 + 3600, families: { opus: { level: 'RED' } } })
    const world = worldOf(on, { [`${BACKEND}/pressure.json`]: future })
    await $.session.start(SESSION)
    await world.clock.advance(5000)
    expect((world.published.get('backend') as View).families).toEqual({})
  })

  test('a future mtime is unknown too', async ($, on) => {
    const world = worldOf(on, { [`${BACKEND}/pressure.json`]: PRESSURE })
    world.mtimes.set(`${BACKEND}/pressure.json`, T0 + 3_600_000)
    await $.session.start(SESSION)
    await world.clock.advance(5000)
    expect((world.published.get('backend') as View).families).toEqual({})
  })

  test('a pressure.json a few seconds ahead of the clock is tolerated', async ($, on) => {
    const near = JSON.stringify({ generated_at: T0 / 1000 + 20, families: { opus: { level: 'AMBER' } } })
    const world = worldOf(on, { [`${BACKEND}/pressure.json`]: near })
    await $.session.start(SESSION)
    await world.clock.advance(5000)
    expect((world.published.get('backend') as View).families).toEqual({ opus: 'AMBER' })
  })

  test('a pressure.json without generated_at is judged by its mtime', async ($, on) => {
    const world = worldOf(on, { [`${BACKEND}/pressure.json`]: PRESSURE })
    world.mtimes.set(`${BACKEND}/pressure.json`, T0 - 3_600_000)
    await $.session.start(SESSION)
    await world.clock.advance(5000)
    expect((world.published.get('backend') as View).families).toEqual({})
  })

  test('no backend at all and no ~/.apex-router: the plugin still runs, view is absent', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await world.clock.advance(125_000)
    const view = world.published.get('backend') as View & { conformance: number | null; lanes: unknown; health: number | null }
    expect(view).toMatchObject({ present: false, families: {}, conformance: null, lanes: null, health: null })
    expect(world.runs.filter(argv => argv.some(a => a.includes('apex-router')))).toEqual([])
    expect([...world.files.keys()].every(p => p.startsWith(`${BACKEND}/observe/`))).toBe(true)
  })

  test('a backendDir that is not absolute after ~ expansion is refused with a reason', () => {
    expect(resolveBackendDir('~/.apex-router', HOME)).toEqual({ dir: `${HOME}/.apex-router`, refused: null })
    expect(resolveBackendDir('/srv/apex', HOME)).toEqual({ dir: '/srv/apex', refused: null })
    const bad = resolveBackendDir('relative/dir', HOME)
    expect(bad.dir).toBe(`${HOME}/.apex-router`)
    expect(bad.refused).toMatch(/not absolute/)
    expect(resolveBackendDir('~', '').refused).toMatch(/not absolute/)
  })
})
