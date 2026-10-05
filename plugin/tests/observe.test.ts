import { describe, expect, test } from 'claude-code/testing'

import type { Host } from '../hooks/host.ts'
import {
  asProfile, bashEventOf, expiredFiles, flushAll, hmacSha256, measureRow, observePath, record, repoToken, spawnRow, start as observeStart,
  ROW_MAX_BYTES,
} from '../hooks/observe.ts'
import { start as routerStart } from '../hooks/router.ts'
import { newRuntime, optionsOf } from '../hooks/runtime.ts'
import { MEASURE, SESSION, spawnInput, stepInput } from './fixtures/inputs.ts'
import { BACKEND, T0, drain, worldOf, type World } from './fixtures/world.ts'

const DAY = observePath(BACKEND, T0)
const rowsOf = (world: World, path = DAY): Record<string, unknown>[] => (world.appended.get(path) ?? []).map(l => JSON.parse(l))

describe('observe: pure rows', () => {
  test('spawn rows keep the dispatch label (≤ 120 chars), never the prompt', () => {
    const row = spawnRow(spawnInput({ description: 'd'.repeat(300), prompt: 'SECRET PROMPT' }), 'explore', 'claude-sonnet-5-5', 1)
    expect(String(row.description)).toHaveLength(120)
    expect(JSON.stringify(row)).not.toContain('SECRET')
    expect(row).toMatchObject({ ev: 'spawn', tool_use_id: 'toolu_1', task_type: 'explore', model_resolved: 'claude-sonnet-5-5' })
  })

  test('measure rows', () => {
    expect(measureRow({ ctxPercent: 61, limitKind: 'five_hour', limitPercent: 62, resetsAt: null, costUsd: 4.12 }, 7)).toEqual({
      ev: 'measure', ts: 7, ctx_pct: 61, limit_kind: 'five_hour', limit_pct: 62, resets_at: null, cost_usd: 4.12,
    })
  })

  test('backend commands and signal patterns are classified; anything else is ignored', () => {
    expect(bashEventOf('apex-router pressure --check', 'GREEN', false, 1)).toEqual({ cmd: 'pressure', signal: null, isError: false, t: 1 })
    expect(bashEventOf('apex-ornith review x', 'OrnithBusy: queue full', true, 2)).toEqual({ cmd: 'ornith', signal: 'OrnithBusy', isError: true, t: 2 })
    expect(bashEventOf('apex-router review-preread', 'timeout after 30s', false, 3)?.signal).toBe('timeout')
    expect(bashEventOf('apex-ornith gen', 'finish_reason=length', false, 4)?.signal).toBe('truncation')
    expect(bashEventOf('ls -la', 'OrnithBusy', false, 5)).toBeNull()
  })

  test('retention keeps 30 UTC days and ignores other files', () => {
    expect(expiredFiles(['2026-09-01.jsonl', '2026-09-03.jsonl', '2026-10-03.jsonl', '.keep', 'notes.txt'], T0)).toEqual(['2026-09-01.jsonl'])
  })

  test('the pending buffer is bounded at 5000 rows, newest kept', () => {
    const rt = newRuntime(optionsOf({}))
    for (let i = 0; i < 5100; i++) record(rt, { ev: 'step', ts: i })
    expect(rt.rows).toHaveLength(5000)
    expect(rt.rows[0]).toBe('{"ev":"step","ts":100}')
  })

  test('an oversized row is replaced by a stub, never split', () => {
    const rt = newRuntime(optionsOf({}))
    record(rt, { ev: 'spawn', ts: 1, description: 'x'.repeat(5000) })
    expect(rt.rows[0]).toBe('{"ev":"spawn","ts":1,"truncated":true}')
    expect(new TextEncoder().encode(rt.rows[0]!).length).toBeLessThanOrEqual(ROW_MAX_BYTES)
  })
})

describe('observe: hooks', () => {
  test('session.start removes expired day files', async ($, on) => {
    const old = `${BACKEND}/observe/2026-09-01.jsonl`
    const kept = `${BACKEND}/observe/2026-09-20.jsonl`
    const world = worldOf(on, { [old]: '{}\n', [kept]: '{}\n' })
    await $.session.start(SESSION)
    expect(world.files.has(old)).toBe(false)
    expect(world.files.has(kept)).toBe(true)
  })

  test('session.start creates the observe directory when it is missing', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    expect(world.files.has(`${BACKEND}/observe/.keep`)).toBe(true)
  })

  test('a step and a measurement land as rows after the 5 s flush — counts only', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    world.step = { toolUses: [{ name: 'Agent', input: { prompt: 'SECRET PROMPT' } }] }
    await drain($.turn.step(stepInput()))
    await $.session.measure(MEASURE)
    expect(rowsOf(world)).toEqual([])
    await world.clock.advance(5000)
    const rows = rowsOf(world)
    expect(rows.map(r => r.ev)).toEqual(['step', 'measure'])
    expect(rows[0]).toMatchObject({ model: 'claude-opus-5-5', output: 50, cache_read: 1000, stop: 'end_turn', tools: ['Agent'] })
    expect(JSON.stringify(rows)).not.toContain('SECRET')
  })

  test('Bash backend commands become signal rows; results pass through untouched', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    world.bashOutput = 'error: OrnithBusy (queue full)'
    const r = await $.tool.call({ tool: 'Bash', command: 'apex-router pressure --check --token=SECRET' } as never)
    expect((r as { result?: unknown }).result).toEqual({ stdout: 'error: OrnithBusy (queue full)', stderr: '' })
    await $.tool.call({ tool: 'Bash', command: 'ls -la' } as never)
    await world.clock.advance(5000)
    expect(rowsOf(world)).toEqual([{ ev: 'bash', ts: T0, cmd: 'pressure', signal: 'OrnithBusy', error: false }])
    expect(JSON.stringify(rowsOf(world))).not.toContain('SECRET')
  })

  test('skills in use are counted in the profile', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.skill.prompt({ skill: 'superpowers:writing-plans', text: 'PLAN' })
    await world.clock.advance(5000)
    const p = world.store.get('datapce.profile') as { skills: Record<string, number>; repos: string[] }
    expect(p.skills['superpowers:writing-plans']).toBe(1)
    expect(p.repos).toHaveLength(1)
    expect(p.repos[0]).toMatch(/^[0-9a-f]{16}$/)
  })

  test('a failed append keeps the rows and retries on the next flush', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    world.answer = argv => (argv[0] === '/usr/bin/tee' ? { exitCode: 1, stdout: '' } : null)
    await drain($.turn.step(stepInput()))
    await world.clock.advance(5000)
    expect(rowsOf(world)).toEqual([])
    world.answer = () => null
    await world.clock.advance(5000)
    expect(rowsOf(world).map(r => r.ev)).toEqual(['step'])
  })

  test('malformed persisted values are dropped, not fatal', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.stats', 'garbage')
    world.store.set('datapce.profile', { repos: 5, hours: 'x', skills: { a: 'b' } })
    await $.session.start(SESSION)
    await drain($.turn.step(stepInput()))
    await world.clock.advance(5000)
    const p = world.store.get('datapce.profile') as { repos: string[]; hours: number[]; skills: Record<string, number> }
    expect(p.repos).toHaveLength(1)
    expect(p.hours).toHaveLength(24)
    expect(p.skills).toEqual({})
    expect(Object.keys(world.store.get('datapce.stats') as object)).toEqual(['step|opus'])
  })
})

describe('observe: a store part that cannot be read is never overwritten', () => {
  /** A host whose store throws for `failing`; every other call is a no-op except storeSet (recorded). */
  const hostFailing = (failing: string, set: [string, unknown][]): Host => {
    const noop = async (): Promise<undefined> => undefined
    const publish = new Proxy({}, { get: () => noop })
    const over: Record<string, unknown> = {
      now: async () => T0,
      list: async () => [],
      every: () => undefined,
      storeGet: async (key: string) => {
        if (key === failing) throw new Error('store read failed')
        return undefined
      },
      storeSet: async (key: string, value: unknown) => void set.push([key, value]),
    }
    return new Proxy({}, { get: (_t, key: string) => (key in over ? over[key] : key === 'publish' ? publish : noop) }) as never
  }
  const ready = () => {
    const rt = newRuntime(optionsOf({}))
    rt.backendDir = BACKEND
    rt.sessionId = 'sess-0001'
    return rt
  }

  for (const [failing, kept, written] of [
    ['datapce.stats', 'datapce.stats', 'datapce.profile'],
    ['datapce.profile', 'datapce.profile', 'datapce.stats'],
  ] as const) {
    test(`${failing} unreadable: a flush writes ${written} and the cells, never ${kept}`, async () => {
      const set: [string, unknown][] = []
      const host = hostFailing(failing, set)
      const rt = ready()
      await observeStart(host, SESSION, rt)
      await routerStart(host, rt)
      rt.statsDirty = true
      rt.profileDirty = true
      rt.cellsDirty = true
      await flushAll(host, rt)
      const keys = set.map(([k]) => k)
      expect(keys).not.toContain(kept)
      expect(keys).toContain(written)
      expect(keys).toContain('datapce.cells')
    })
  }
})

describe('observe: repos are a salted per-install token', () => {
  const hexOf = (b: Uint8Array): string => [...b].map(x => x.toString(16).padStart(2, '0')).join('')
  const enc = (t: string): Uint8Array => new TextEncoder().encode(t)
  const unsalted = async (cwd: string): Promise<string> => hexOf(new Uint8Array(await crypto.subtle.digest('SHA-256', enc(cwd)))).slice(0, 16)
  const SALT_A = '00112233445566778899aabbccddeeff'
  const SALT_B = 'ffeeddccbbaa99887766554433221100'

  /** A host over an in-memory store; everything else is a no-op. */
  const hostOver = (store: Map<string, unknown>): Host => {
    const noop = async (): Promise<undefined> => undefined
    const publish = new Proxy({}, { get: () => noop })
    const over: Record<string, unknown> = {
      now: async () => T0,
      list: async () => [],
      every: () => undefined,
      storeGet: async (key: string) => store.get(key),
      storeSet: async (key: string, value: unknown) => void store.set(key, structuredClone(value)),
    }
    return new Proxy({}, { get: (_t, key: string) => (key in over ? over[key] : key === 'publish' ? publish : noop) }) as never
  }
  const session = async (store: Map<string, unknown>, cwd: string): Promise<void> => {
    const host = hostOver(store)
    const rt = newRuntime(optionsOf({}))
    rt.backendDir = BACKEND
    rt.sessionId = 'sess-0001'
    rt.storeLoaded = true
    await observeStart(host, { ...SESSION, cwd }, rt)
    await flushAll(host, rt)
  }
  type Stored = { repos: string[]; salt?: string; skills: Record<string, number> }

  test('HMAC-SHA-256 matches RFC 4231 test case 2', async () => {
    const mac = await hmacSha256(enc('Jefe'), enc('what do ya want for nothing?'))
    expect(hexOf(mac)).toBe('5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843')
  })

  test('HMAC-SHA-256 hashes a key longer than the block first (RFC 4231 test case 6)', async () => {
    const key = new Uint8Array(131).fill(0xaa)
    const mac = await hmacSha256(key, enc('Test Using Larger Than Block-Size Key - Hash Key First'))
    expect(hexOf(mac)).toBe('60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54')
  })

  test('the token is not the unsalted path hash, and two salts give two tokens', async () => {
    const a = await repoToken('/w/x', SALT_A)
    expect(a).toMatch(/^[0-9a-f]{16}$/)
    expect(a).not.toBe(await unsalted('/w/x'))
    expect(await repoToken('/w/x', SALT_B)).not.toBe(a)
    expect(await repoToken('/w/x', SALT_A)).toBe(a)
    expect(await repoToken('/w/y', SALT_A)).not.toBe(a)
  })

  test('the salt is created once and reused across sessions', async () => {
    const store = new Map<string, unknown>()
    await session(store, '/w/x')
    const first = store.get('datapce.profile') as Stored
    expect(first.salt).toMatch(/^[0-9a-f]{32}$/)
    expect(first.repos).toEqual([await repoToken('/w/x', first.salt as string)])
    await session(store, '/w/x')
    await session(store, '/w/y')
    const later = store.get('datapce.profile') as Stored
    expect(later.salt).toBe(first.salt)
    expect(later.repos).toEqual([await repoToken('/w/x', first.salt as string), await repoToken('/w/y', first.salt as string)])
    expect(later.repos).not.toContain(await unsalted('/w/x'))
  })

  test('two installs draw two salts', async () => {
    const one = new Map<string, unknown>()
    const two = new Map<string, unknown>()
    await session(one, '/w/x')
    await session(two, '/w/x')
    expect((one.get('datapce.profile') as Stored).salt).not.toBe((two.get('datapce.profile') as Stored).salt)
  })

  test('a 0.4.0 profile (unsalted hashes, no salt) drops its repos once and keeps its counts', async () => {
    const store = new Map<string, unknown>()
    const old = [await unsalted('/w/x'), await unsalted('/w/old')]
    store.set('datapce.profile', { repos: old, taskMix: { explore: 3 }, skills: { s: 2 }, backend: true })
    await session(store, '/w/x')
    const p = store.get('datapce.profile') as Stored & { taskMix: Record<string, number> }
    expect(p.repos).toEqual([await repoToken('/w/x', p.salt as string)])
    for (const h of old) expect(p.repos).not.toContain(h)
    expect(p.taskMix).toEqual({ explore: 3 })
    expect(p.skills).toEqual({ s: 2 })
  })

  test('a malformed salt is treated as no salt: repos dropped, never fatal', async () => {
    for (const salt of ['nope', 42, null, 'A'.repeat(32), '0'.repeat(31)]) {
      expect(asProfile({ repos: ['0123456789abcdef'], salt })).toMatchObject({ repos: [] })
      expect(asProfile({ repos: ['0123456789abcdef'], salt }).salt).toBeUndefined()
    }
    expect(asProfile({ repos: ['0123456789abcdef', 7, 'not-a-token'], salt: SALT_A })).toMatchObject({ repos: ['0123456789abcdef'], salt: SALT_A })
    const store = new Map<string, unknown>([['datapce.profile', { repos: ['0123456789abcdef'], salt: 'zz' }]])
    await session(store, '/w/x')
    const p = store.get('datapce.profile') as Stored
    expect(p.salt).toMatch(/^[0-9a-f]{32}$/)
    expect(p.repos).toEqual([await repoToken('/w/x', p.salt as string)])
  })

  test('a profile without a salt is never written with repos', async () => {
    const store = new Map<string, unknown>()
    const rt = newRuntime(optionsOf({}))
    rt.backendDir = BACKEND
    rt.storeLoaded = true
    rt.profileLoaded = true
    rt.profileDirty = true
    rt.profile = { ...rt.profile, repos: [await unsalted('/w/x')] }
    await flushAll(hostOver(store), rt)
    expect((store.get('datapce.profile') as Stored).repos).toEqual([])
  })
})
