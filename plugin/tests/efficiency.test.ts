import { describe, expect, test } from 'claude-code/testing'

import type { Cell } from '../types/index.d.ts'
import { cellKey } from '../hooks/evidence.ts'
import type { Host } from '../hooks/host.ts'
import { MAX_COMPOSE_LINES, MAX_DESCRIBE_LINES, MAX_SECTION_LINES, SESSION_CAP_BYTES, skillSection, utf8Bytes } from '../hooks/inject.ts'
import { newRuntime, optionsOf } from '../hooks/runtime.ts'
import { decideSpawn, type SpawnContext } from '../hooks/router.ts'
import { MEASURE, SESSION, spawnInput } from './fixtures/inputs.ts'
import { BACKEND, T0, worldOf, type World } from './fixtures/world.ts'

const READY: Cell = { n: 35, pass: 35, state: 'READY', winN: 5, winPass: 5, below: 0, above: 0, cusum: 0, p0: 1 }
const PLANNING = [
  'superpowers:writing-plans',
  'superpowers:subagent-driven-development',
  'superpowers:dispatching-parallel-agents',
  'superpowers:executing-plans',
  'datapce:datapce',
]
const SESSION_ID = 'sess-0001' // armOf → evidence
const PROVIDER = { plugin: 'engine', tier: 'core' } as never
const RED_MEASURE = { ...MEASURE, rateLimits: [{ kind: 'five_hour' as const, percentUsed: 95, resetsAt: '2026-10-03T20:00:00Z' }] }
const COMPOSE = { model: 'claude-opus-5-5', promptModel: 'claude-opus-5-5', surfaces: ['terminal'], tools: ['Agent', 'Bash'], outputStyle: null, traits: [] } as never
const manyReady = (n: number): Record<string, Cell> =>
  Object.fromEntries(Array.from({ length: n }, (_, i) => [cellKey(`task${String(i).padStart(2, '0')}`, 'GREEN', 'sonnet'), READY]))
const injectRows = (world: World): Record<string, unknown>[] =>
  (world.appended.get(`${BACKEND}/observe/2026-10-03.jsonl`) ?? []).map(l => JSON.parse(l)).filter(r => r.ev === 'inject')

describe('§17 efficiency contract', () => {
  test('COLD: zero bytes injected anywhere, at GREEN pressure', async ($, on) => {
    worldOf(on)
    await $.session.start(SESSION)
    for (const skill of PLANNING) expect((await $.skill.prompt({ skill, text: 'X' })).text).toBe('X')
    for (const tool of ['Agent', 'Workflow']) expect((await $.tool.describe({ tool, description: 'D', provider: PROVIDER })).description).toBe('D')
    expect((await $.prompt.compose(COMPOSE)).sections.map(s => s.id)).toEqual(['intro'])
  })

  test('the per-session cap holds however often the planning skills run; every decision is logged (v1.1 path)', async () => {
    const rt = newRuntime(optionsOf({}))
    rt.sessionId = SESSION_ID
    rt.cells = manyReady(18)
    const host = { publish: { inject: async () => {} } } as unknown as Host
    let appended = 0
    let sections = 0
    for (let i = 0; i < 10; i++) {
      const text = await skillSection(host, rt, PLANNING[i % PLANNING.length]!, 'X', T0, true)
      if (text !== 'X') {
        sections += 1
        expect(text.slice(3).split('\n').length).toBeLessThanOrEqual(MAX_SECTION_LINES)
        appended += utf8Bytes(text) - 1
      }
    }
    expect(sections).toBeGreaterThan(0)
    expect(appended).toBeLessThanOrEqual(SESSION_CAP_BYTES)
    const rows = rt.rows.map(l => JSON.parse(l)).filter(r => r.ev === 'inject')
    expect(rows).toHaveLength(10)
    expect(rows.every(r => r.arm === 'evidence')).toBe(true)
    expect(rows.filter(r => r.capped === true).length).toBe(10 - sections)
    expect(rows.filter(r => r.capped === true).length).toBeGreaterThan(0)
  })

  test('v1 gate: planning skills get nothing even with many READY cells', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', manyReady(18))
    await $.session.start(SESSION)
    for (const skill of PLANNING) expect((await $.skill.prompt({ skill, text: 'X' })).text).toBe('X')
    await world.clock.advance(5000)
    expect(injectRows(world)).toEqual([])
  })

  test('tool.describe appends at most two lines, and only when pressure is not GREEN', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', manyReady(18))
    await $.session.start(SESSION)
    expect((await $.tool.describe({ tool: 'Agent', description: 'D', provider: PROVIDER })).description).toBe('D')
    await $.session.measure(RED_MEASURE as never)
    await world.clock.advance(120_000)
    const d = (await $.tool.describe({ tool: 'Agent', description: 'D', provider: PROVIDER })).description
    expect(d).not.toBe('D')
    expect(d.split('\n').length - 1).toBeLessThanOrEqual(MAX_DESCRIBE_LINES)
  })

  test('prompt.compose injects at most four lines, only when pressure is not GREEN', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    expect((await $.prompt.compose(COMPOSE)).sections.map(s => s.id)).toEqual(['intro'])
    await $.session.measure(RED_MEASURE as never)
    await world.clock.advance(120_000)
    const sections = (await $.prompt.compose(COMPOSE)).sections
    expect(sections.map(s => s.id)).toEqual(['intro', 'datapce:pressure'])
    expect(sections[1]!.text.split('\n').length).toBeLessThanOrEqual(MAX_COMPOSE_LINES)
  })

  test('under RED the session cap spans skill, describe and compose together', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', manyReady(18))
    await $.session.start(SESSION)
    await $.session.measure(RED_MEASURE as never)
    await world.clock.advance(120_000)
    for (let i = 0; i < 20; i++) {
      await $.skill.prompt({ skill: PLANNING[i % PLANNING.length]!, text: 'X' })
      await $.tool.describe({ tool: 'Agent', description: 'D', provider: PROVIDER })
      await $.prompt.compose(COMPOSE)
    }
    await world.clock.advance(5000)
    const sent = injectRows(world).reduce((a, r) => a + (typeof r.bytes === 'number' ? r.bytes : 0), 0)
    expect(sent).toBeGreaterThan(0)
    expect(sent).toBeLessThanOrEqual(SESSION_CAP_BYTES)
    expect((world.published.get('inject') as { bytes: number }).bytes).toBeLessThanOrEqual(SESSION_CAP_BYTES)
  })

  test('agent.spawn does no file or process I/O on the hot path', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', manyReady(18))
    await $.session.start(SESSION)
    await new Promise(r => setTimeout(r, 0)) // the first backend poll finishes in the background
    const asked = world.asked.length
    const runs = world.runs.length
    for (let i = 0; i < 50; i++) await $.agent.spawn(spawnInput({ tool_use_id: `toolu_${i}` }))
    expect(world.asked.length).toBe(asked)
    expect(world.runs.length).toBe(runs)
  })

  test('the spawn always reaches the engine exactly as requested (advise-only)', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', manyReady(18))
    await $.session.start(SESSION)
    for (const model of [undefined, 'opus', 'sonnet', 'haiku']) await $.agent.spawn(spawnInput({ model, tool_use_id: `toolu_${model}` }))
    expect(world.spawned.map(s => s.model)).toEqual([undefined, 'opus', 'sonnet', 'haiku'])
  })

  test('the agent.spawn hook adds under 5 ms at p99 with a full cell table', async ($, on) => {
    const world = worldOf(on)
    const cells: Record<string, Cell> = {}
    for (const task of ['explore', 'review', 'debug', 'refactor', 'generate', 'mechanical', 'synthesis', 'other']) {
      for (const level of ['GREEN', 'AMBER', 'RED'] as const) {
        for (const tier of ['haiku', 'sonnet', 'opus'] as const) cells[cellKey(task, level, tier)] = { ...READY, state: tier === 'haiku' ? 'WARMING' : 'READY' }
      }
    }
    world.store.set('datapce.cells', cells)
    await $.session.start(SESSION)
    const times: number[] = []
    for (let i = 0; i < 300; i++) {
      const input = spawnInput({ tool_use_id: `toolu_p${i}`, description: `Review change ${i}`, prompt: 'p'.repeat(4000) })
      const t0 = performance.now()
      await $.agent.spawn(input)
      times.push(performance.now() - t0)
    }
    times.sort((a, b) => a - b)
    expect(times[Math.ceil(times.length * 0.99) - 1]!).toBeLessThan(5)
  })

  test('the pure spawn decision stays under 5 ms at p99 with a full cell table', () => {
    const cells: Record<string, Cell> = {}
    for (const task of ['explore', 'review', 'debug', 'refactor', 'generate', 'mechanical', 'synthesis', 'other']) {
      for (const level of ['GREEN', 'AMBER', 'RED'] as const) {
        for (const tier of ['haiku', 'sonnet', 'opus'] as const) cells[cellKey(task, level, tier)] = { ...READY, state: tier === 'haiku' ? 'WARMING' : 'READY' }
      }
    }
    const ctx: SpawnContext = { level: 'AMBER', cells, stats: {}, admitted: true }
    const times: number[] = []
    for (let i = 0; i < 1000; i++) {
      const t0 = performance.now()
      decideSpawn(spawnInput({ description: `Review change ${i}`, prompt: 'p'.repeat(4000) }), ctx)
      times.push(performance.now() - t0)
    }
    times.sort((a, b) => a - b)
    expect(times[989]!).toBeLessThan(5)
  })
})
