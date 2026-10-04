import { describe, expect, test } from 'claude-code/testing'

import type { Cell } from '../types/index.d.ts'
import { cellKey } from '../hooks/evidence.ts'
import {
  armOf,
  bareSkill,
  composeText,
  describeText,
  evidenceSection,
  fnv1a,
  INJECT_SKILLS,
  skillSection,
  MAX_COMPOSE_LINES,
  MAX_DESCRIBE_LINES,
  MAX_SECTION_LINES,
  SESSION_CAP_BYTES,
  utf8Bytes,
} from '../hooks/inject.ts'
import type { Host } from '../hooks/host.ts'
import { newRuntime, optionsOf } from '../hooks/runtime.ts'
import { complete, MEASURE, SESSION, spawnInput } from './fixtures/inputs.ts'
import { BACKEND, T0, worldOf, type World } from './fixtures/world.ts'

const READY: Cell = { n: 35, pass: 35, state: 'READY', winN: 5, winPass: 5, below: 0, above: 0, cusum: 0, p0: 1 }
const WARMING: Cell = { n: 12, pass: 11, state: 'WARMING', winN: 5, winPass: 4, below: 0, above: 0, cusum: 0, p0: null }
const PROVIDER = { plugin: 'engine', tier: 'core' } as never
const HOLDOUT_ID = 'sess-0002'
const RED_MEASURE = { ...MEASURE, rateLimits: [{ kind: 'five_hour' as const, percentUsed: 95, resetsAt: '2026-10-03T20:00:00Z' }] }
const composeOf = (tools: string[], traits: string[] = []) =>
  ({ model: 'claude-opus-5-5', promptModel: 'claude-opus-5-5', surfaces: ['terminal'], tools, outputStyle: null, traits }) as never
const COMPOSE = composeOf(['Agent', 'Bash'])

const observed = (world: World): Record<string, unknown>[] =>
  (world.appended.get(`${BACKEND}/observe/2026-10-03.jsonl`) ?? []).map(l => JSON.parse(l))
const injectRows = (world: World) => observed(world).filter(r => r.ev === 'inject')
const routeRows = (world: World): Record<string, unknown>[] => (world.appended.get(`${BACKEND}/route_log.jsonl`) ?? []).map(l => JSON.parse(l))
const seed = (world: World, cells: Record<string, Cell>): void => void world.store.set('datapce.cells', cells)
const seedReady = (world: World): void => seed(world, { [cellKey('explore', 'GREEN', 'sonnet')]: READY })
/** Two 1-minute windows at 95% of the five-hour limit: the live tick moves pressure to RED. */
const goRed = async ($: { session: { measure(e: never): Promise<unknown> } }, world: World): Promise<void> => {
  await $.session.measure(RED_MEASURE as never)
  await world.clock.advance(120_000)
}

describe('inject: pure', () => {
  test('FNV-1a and the 80/20 arm split', () => {
    expect(fnv1a('')).toBe(2166136261)
    expect(fnv1a('a')).toBe(3826002220)
    expect(armOf('sess-0001')).toBe('evidence')
    expect(armOf(HOLDOUT_ID)).toBe('holdout')
  })

  test('skills match on the name after the last colon', () => {
    expect(bareSkill('superpowers:writing-plans')).toBe('writing-plans')
    expect(INJECT_SKILLS.has(bareSkill('datapce:datapce'))).toBe(true)
    expect(INJECT_SKILLS.has(bareSkill('commit'))).toBe(false)
  })

  test('no READY row, no section (P3): empty, WARMING-only and DRIFTING-only all give nothing', () => {
    expect(evidenceSection([], 'GREEN', null)).toBeNull()
    expect(evidenceSection([{ taskType: 'review', tier: 'opus', n: 12, passPct: 92, tokMean: null, state: 'WARMING' }], 'GREEN', null)).toBeNull()
    expect(evidenceSection([{ taskType: 'review', tier: 'opus', n: 40, passPct: 80, tokMean: null, state: 'DRIFTING' }], 'GREEN', null)).toBeNull()
  })

  test('the section is a table of READY rows only plus one rule', () => {
    const text = evidenceSection(
      [
        { taskType: 'explore', tier: 'sonnet', n: 35, passPct: 100, tokMean: 41000, state: 'READY' },
        { taskType: 'review', tier: 'opus', n: 12, passPct: 92, tokMean: null, state: 'WARMING' },
      ],
      'AMBER',
      5.88,
    )
    expect(text).toBe(
      [
        '## datapce evidence (this machine)',
        'pressure AMBER · budget $5.88 left',
        '| task_type | tier | n pass% | tok μ | state |',
        '|---|---|---|---|---|',
        '| explore | sonnet | 35 100% | 41k | READY |',
        'rule: name the tier per task in the plan; datapce applies it at dispatch.',
      ].join('\n'),
    )
  })

  test('≤ 25 lines however many task types', () => {
    const rows = Array.from({ length: 40 }, (_, i) => ({ taskType: `t${i}`, tier: 'sonnet' as const, n: 35, passPct: 100, tokMean: null, state: 'READY' as const }))
    expect(evidenceSection(rows, 'GREEN', null)!.split('\n').length).toBeLessThanOrEqual(MAX_SECTION_LINES)
  })

  test('describe text is pressure only, ≤ 2 lines, nothing when GREEN', () => {
    expect(describeText('GREEN')).toBeNull()
    expect(describeText('AMBER')).toBe('datapce: pressure AMBER — limit parallel fan-out')
    expect(describeText('RED')).toBe('datapce: pressure RED — serialize heavy fan-out')
    for (const l of ['AMBER', 'RED'] as const) expect(describeText(l)!.split('\n').length).toBeLessThanOrEqual(MAX_DESCRIBE_LINES)
  })

  test('compose text: ≤ 4 lines, nothing when GREEN', () => {
    expect(composeText('GREEN')).toBeNull()
    for (const l of ['AMBER', 'RED'] as const) {
      const t = composeText(l)!
      expect(t).toContain(`pressure ${l}`)
      expect(t.split('\n').length).toBeLessThanOrEqual(MAX_COMPOSE_LINES)
    }
  })

  test('hot reload (no session.start, no session id): no arm is known, so nothing is injected or logged', async () => {
    const rt = newRuntime(optionsOf({}))
    rt.cells = { [cellKey('explore', 'GREEN', 'sonnet')]: READY }
    expect(await skillSection({} as Host, rt, 'superpowers:writing-plans', 'PLAN', T0)).toBe('PLAN')
    expect(rt.rows).toEqual([])
    expect(rt.injectedBytes).toBe(0)
  })
})

describe('inject: hooks', () => {
  test('v1, no READY row (WARMING only): planning skills get nothing, nothing is logged, the arm is published', async ($, on) => {
    const world = worldOf(on)
    seed(world, { [cellKey('review', 'GREEN', 'opus')]: WARMING })
    await $.session.start(SESSION)
    expect((await $.skill.prompt({ skill: 'superpowers:writing-plans', text: 'PLAN' })).text).toBe('PLAN')
    expect((await $.skill.prompt({ skill: 'datapce:datapce', text: 'D' })).text).toBe('D')
    await world.clock.advance(5000)
    expect(injectRows(world)).toEqual([])
    expect(world.published.get('inject')).toEqual({ arm: 'evidence', bytes: 0, sections: 0 })
  })

  test('GREEN pressure injects nothing anywhere, even with a READY row in describe/compose', async ($, on) => {
    const world = worldOf(on)
    seedReady(world)
    await $.session.start(SESSION)
    for (const tool of ['Agent', 'Workflow']) {
      expect((await $.tool.describe({ tool, description: 'Launch work', provider: PROVIDER })).description).toBe('Launch work')
    }
    expect((await $.prompt.compose(COMPOSE)).sections.map(s => s.id)).toEqual(['intro'])
    await world.clock.advance(5000)
    expect(injectRows(world)).toEqual([])
  })

  test('READY row, evidence arm: planning skills only, logged, and stamped on route rows', async ($, on) => {
    const world = worldOf(on)
    seedReady(world)
    await $.session.start(SESSION)
    const plan = await $.skill.prompt({ skill: 'superpowers:writing-plans', text: 'PLAN' })
    expect(plan.text.startsWith('PLAN\n\n## datapce evidence (this machine)\npressure GREEN\n')).toBe(true)
    expect(plan.text).toContain('| explore | sonnet | 35 100% | — | READY |')
    expect((await $.skill.prompt({ skill: 'commit', text: 'COMMIT' })).text).toBe('COMMIT')
    await $.agent.spawn(spawnInput())
    await $.turn.complete(complete())
    await world.clock.advance(5000)
    const inject = injectRows(world)
    expect(inject).toEqual([expect.objectContaining({ site: 'skill', skill: 'superpowers:writing-plans', arm: 'evidence' })])
    expect(inject[0]!.bytes as number).toBeGreaterThan(0)
    expect(routeRows(world)[0]).toMatchObject({ inject_arm: 'evidence', injected: 'yes' })
  })

  test('no injection in the session: route rows still carry the arm, injected no', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput())
    await $.turn.complete(complete())
    await world.clock.advance(5000)
    expect(routeRows(world)[0]).toMatchObject({ inject_arm: 'evidence', injected: 'no' })
  })

  test('the same planning skill many times: each invocation appends again until the 2 KB session cap', async ($, on) => {
    const world = worldOf(on)
    seedReady(world)
    await $.session.start(SESSION)
    const outs: string[] = []
    for (let i = 0; i < 20; i++) outs.push((await $.skill.prompt({ skill: 'superpowers:writing-plans', text: 'PLAN' })).text)
    const appended = outs.filter(t => t !== 'PLAN')
    expect(appended.length).toBeGreaterThan(1)
    expect(appended.length).toBeLessThan(20)
    // once refused, every later invocation is refused too (the same section never fits again)
    expect(outs.slice(appended.length).every(t => t === 'PLAN')).toBe(true)
    const appendedBytes = appended.reduce((s, t) => s + utf8Bytes(t) - utf8Bytes('PLAN'), 0)
    expect(appendedBytes).toBeLessThanOrEqual(SESSION_CAP_BYTES)
    await world.clock.advance(5000)
    const rows = injectRows(world)
    expect(rows.length).toBe(20)
    expect(rows.reduce((s, r) => s + (r.bytes as number), 0)).toBe(appendedBytes)
    expect(rows.filter(r => r.capped === true).length).toBe(20 - appended.length)
    expect(world.published.get('inject')).toEqual({ arm: 'evidence', bytes: appendedBytes, sections: appended.length })
  })

  test('the cap is per session across sites: skill, describe and compose share the 2 KB', async ($, on) => {
    const world = worldOf(on)
    seedReady(world)
    await $.session.start(SESSION)
    for (let i = 0; i < 20; i++) await $.skill.prompt({ skill: 'superpowers:writing-plans', text: 'PLAN' })
    await goRed($, world)
    const described: string[] = []
    for (let i = 0; i < 20; i++) described.push((await $.tool.describe({ tool: 'Agent', description: 'Launch work', provider: PROVIDER })).description)
    expect(described[described.length - 1]).toBe('Launch work')
    await $.prompt.compose(COMPOSE)
    await world.clock.advance(5000)
    const rows = injectRows(world)
    expect(rows.some(r => r.site === 'describe' && (r.bytes as number) > 0)).toBe(true)
    expect(rows.some(r => r.site === 'describe' && r.capped === true)).toBe(true)
    expect(rows.reduce((s, r) => s + (r.bytes as number), 0)).toBeLessThanOrEqual(SESSION_CAP_BYTES)
  })

  test('holdout arm: the same skill many times never injects; each withheld section is logged', async ($, on) => {
    const world = worldOf(on, {}, HOLDOUT_ID)
    seedReady(world)
    await $.session.start(SESSION)
    for (let i = 0; i < 10; i++) {
      expect((await $.skill.prompt({ skill: 'superpowers:executing-plans', text: 'EXEC' })).text).toBe('EXEC')
    }
    await $.agent.spawn(spawnInput())
    await $.turn.complete(complete())
    await world.clock.advance(5000)
    const rows = injectRows(world)
    expect(rows.length).toBe(10)
    expect(rows[0]).toEqual({ ev: 'inject', ts: T0, site: 'skill', skill: 'superpowers:executing-plans', arm: 'holdout', bytes: 0, withheld: true })
    expect(rows.every(r => r.withheld === true && r.bytes === 0)).toBe(true)
    expect(routeRows(world)[0]).toMatchObject({ inject_arm: 'holdout', injected: 'no' })
    expect(world.published.get('inject')).toEqual({ arm: 'holdout', bytes: 0, sections: 0 })
  })

  test('holdout arm under RED: describe and compose withheld', async ($, on) => {
    const world = worldOf(on, {}, HOLDOUT_ID)
    await $.session.start(SESSION)
    await goRed($, world)
    expect((await $.tool.describe({ tool: 'Agent', description: 'Launch work', provider: PROVIDER })).description).toBe('Launch work')
    expect((await $.prompt.compose(COMPOSE)).sections.map(s => s.id)).toEqual(['intro'])
    await world.clock.advance(5000)
    expect(injectRows(world).map(r => [r.site, r.withheld])).toEqual([
      ['describe', true],
      ['compose', true],
    ])
  })

  test('RED: tool.describe on Agent and Workflow appends the pressure line only', async ($, on) => {
    const world = worldOf(on)
    seedReady(world)
    await $.session.start(SESSION)
    await goRed($, world)
    for (const tool of ['Agent', 'Workflow']) {
      const r = await $.tool.describe({ tool, description: 'Launch work', provider: PROVIDER })
      expect(r.description).toBe('Launch work\ndatapce: pressure RED — serialize heavy fan-out')
    }
  })

  test('RED: prompt.compose appends one session section, logged once per text, only where Agent/Workflow is offered', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await goRed($, world)
    for (let i = 0; i < 5; i++) {
      const r = await $.prompt.compose(COMPOSE)
      expect(r.sections.map(s => [s.id, s.scope])).toEqual([
        ['intro', 'shared'],
        ['datapce:pressure', 'session'],
      ])
      expect(r.sections[1]!.text.split('\n').length).toBeLessThanOrEqual(MAX_COMPOSE_LINES)
      expect(r.sections[1]!.text).toContain('pressure RED')
    }
    expect((await $.prompt.compose(composeOf(['Bash', 'Read']))).sections.map(s => s.id)).toEqual(['intro'])
    await world.clock.advance(5000)
    expect(injectRows(world).filter(r => r.site === 'compose')).toEqual([expect.objectContaining({ arm: 'evidence', skill: 'prompt' })])
  })

  test('a changed pressure line invalidates tool.describe at most once per 10 minutes', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await goRed($, world)
    await world.clock.advance(7 * 60_000)
    expect(world.invalidated).toEqual([])
    await world.clock.advance(60_000)
    expect(world.invalidated).toEqual(['tool.describe'])
    await world.clock.advance(5 * 60_000)
    expect(world.invalidated).toEqual(['tool.describe'])
  })

  test('holdout arm schedules no describe refresh', async ($, on) => {
    const world = worldOf(on, {}, HOLDOUT_ID)
    await $.session.start(SESSION)
    await goRed($, world)
    await world.clock.advance(20 * 60_000)
    expect(world.invalidated).toEqual([])
  })
})
