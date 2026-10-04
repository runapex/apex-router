import type { On, TurnStepInput, TurnStepResult } from 'claude-code'
import { mock } from 'claude-code/testing'
import type { MockClock } from 'claude-code/testing'

export const HOME = '/home/u'
/** 2026-10-03T12:00:00Z — the world's clock starts here (a mock: it moves only on advance). */
export const T0 = Date.UTC(2026, 9, 3, 12, 0, 0)
export const BACKEND = `${HOME}/.apex-router`
export const SESSION_ID = 'sess-0001'

export type Answer = { exitCode: number; stdout: string }

/** The world beneath the plugin, in memory, and a record of what the plugin asked of it. */
export type World = {
  clock: MockClock
  files: Map<string, string>
  mtimes: Map<string, number>
  appended: Map<string, string[]>
  runs: string[][]
  asked: string[]
  store: Map<string, unknown>
  /** What the plugin published with `$.state.set`, by key (latest value). */
  published: Map<string, unknown>
  toasts: string[]
  statuses: (string | undefined)[]
  opened: string[]
  closed: string[]
  commands: string[]
  invalidated: string[]
  spawned: { model: string | undefined; subagentType: string }[]
  denySpawn: boolean
  bashOutput: string
  step: Partial<TurnStepResult>
  answer: (argv: readonly string[]) => Answer | null
}

export const RESOLVED: Record<string, string> = {
  haiku: 'claude-haiku-4-5',
  sonnet: 'claude-sonnet-5-5',
  opus: 'claude-opus-5-5',
  fable: 'claude-fable-5',
}

export function resolveModel(model: string): string {
  const low = model.toLowerCase()
  for (const [tier, id] of Object.entries(RESOLVED)) if (low.includes(tier)) return id
  return model
}

const run = (stdout: string, exitCode = 0) => ({
  value: { exitCode, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
})

export function worldOf(on: On, files: Readonly<Record<string, string>> = {}, sessionId = SESSION_ID): World {
  const world: World = {
    clock: mock.clock(on, { now: T0 }),
    files: new Map(Object.entries(files)),
    mtimes: new Map(),
    appended: new Map(),
    runs: [],
    asked: [],
    store: new Map(),
    published: new Map(),
    toasts: [],
    statuses: [],
    opened: [],
    closed: [],
    commands: [],
    invalidated: [],
    spawned: [],
    denySpawn: false,
    bashOutput: '',
    step: {},
    answer: () => null,
  }
  const isDir = (p: string) => [...world.files.keys()].some(f => f.startsWith(`${p}/`))

  on('fs.read', ($, e) => {
    world.asked.push(`read ${e.path}`)
    const text = world.files.get(e.path)
    return text === undefined ? { deny: `ENOENT: ${e.path}` } : { value: text }
  })
  on('fs.exists', ($, e) => {
    world.asked.push(`exists ${e.path}`)
    return { value: world.files.has(e.path) || isDir(e.path) }
  })
  on('fs.write', ($, e) => {
    world.files.set(e.path, e.text)
    return { value: undefined }
  })
  on('fs.stat', ($, e) => {
    world.asked.push(`stat ${e.path}`)
    const text = world.files.get(e.path)
    if (text !== undefined) return { value: { kind: 'file' as const, size: text.length, mtimeMs: world.mtimes.get(e.path) ?? 0, isLink: false } }
    return isDir(e.path) ? { value: { kind: 'dir' as const, size: 0, mtimeMs: 0, isLink: false } } : { deny: `ENOENT: ${e.path}` }
  })
  on('fs.list', ($, e) => {
    world.asked.push(`list ${e.path}`)
    const names = new Map<string, 'file' | 'dir'>()
    for (const file of world.files.keys()) {
      if (!file.startsWith(`${e.path}/`)) continue
      const rest = file.slice(e.path.length + 1)
      names.set(rest.split('/')[0] ?? '', rest.includes('/') ? 'dir' : 'file')
    }
    if (names.size === 0 && !isDir(e.path)) return { deny: `ENOENT: ${e.path}` }
    return {
      value: [...names.entries()].sort().map(([name, kind]) => ({
        name,
        kind,
        size: kind === 'file' ? (world.files.get(`${e.path}/${name}`)?.length ?? 0) : 0,
        mtimeMs: world.mtimes.get(`${e.path}/${name}`) ?? 0,
        isLink: false,
      })),
    }
  })
  on('process.run', ($, e) => {
    const argv = [...e.argv]
    world.runs.push(argv)
    const custom = world.answer(argv)
    if (custom !== null) return run(custom.stdout, custom.exitCode)
    const stdin = e.init?.stdin ?? ''
    if (argv[0] === '/usr/bin/tee' && argv[1] === '-a' && argv[2] === '--' && argv[3] !== undefined) {
      const lines = stdin.split('\n').filter(l => l !== '')
      world.appended.set(argv[3], [...(world.appended.get(argv[3]) ?? []), ...lines])
      return run(stdin)
    }
    if (argv[0] === '/usr/bin/tail' && argv[1] === '-n' && argv[3] === '--') {
      const text = world.files.get(argv[4] ?? '')
      if (text === undefined) return run('', 1)
      const lines = text.split('\n').filter(l => l !== '')
      return run(`${lines.slice(-Number(argv[2])).join('\n')}\n`)
    }
    if (argv[0] === '/bin/rm' && argv[1] === '-f' && argv[2] === '--') {
      for (const p of argv.slice(3)) world.files.delete(p)
      return run('')
    }
    return run('', 127)
  })
  on('store.get', ($, e) => ({ value: world.store.get(e.key) }))
  on('store.set', ($, e) => {
    world.store.set(e.key, e.value)
    return { value: undefined }
  })
  on('state.set', (_$, e, next) => {
    world.published.set(e.key, e.value)
    return next(e)
  })
  on('env.get', ($, e) => ({ value: e.name === 'HOME' ? HOME : undefined }))
  on('session.id', () => ({ value: sessionId }))
  on('ui.toast', ($, e) => {
    world.toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.status', ($, e) => {
    world.statuses.push(e.text)
    return { value: undefined }
  })
  on('ui.open', ($, e) => {
    world.opened.push(e.id)
    return { value: { isPlaced: true } } as never
  })
  on('ui.close', ($, e) => {
    world.closed.push(e.id)
    return { value: undefined }
  })
  on('ui.invalidate', ($, e) => {
    world.invalidated.push(e.event)
    return { value: undefined }
  })
  on('command.register', ($, e) => {
    world.commands.push(e.name)
    return { value: { command: e.name } } as never
  })

  // Engine bottoms for the events the plugin hooks (a site the plugin passes on draws nothing).
  on('ui.render', () => ({ type: 'Box', children: [] }) as never)
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('session.end', ($, e) => ({ sessionId: e.sessionId }))
  on('session.measure', ($, e) => ({ changed: e.changed }))
  on('agent.spawn', ($, e) => {
    world.spawned.push({ model: e.model, subagentType: e.subagentType })
    if (world.denySpawn) return { deny: 'denied by policy' }
    return { model: resolveModel(e.model ?? e.parentModel), agentId: `agent-${world.spawned.length}` }
  })
  on('turn.complete', ($, e) => ({ text: e.answer }))
  on('skill.prompt', ($, e) => ({ text: e.text }))
  on('tool.describe', ($, e) => ({ description: e.description }))
  on('tool.call', () => ({ result: { stdout: world.bashOutput, stderr: '' } }) as never)
  on('turn.step', async function* ($, e: TurnStepInput) {
    const result: TurnStepResult = {
      turnId: e.turnId,
      index: e.index,
      answer: '',
      toolUses: [],
      stopReason: 'end_turn',
      usage: { input_tokens: 100, output_tokens: 50, cache_read_input_tokens: 1000, cache_creation_input_tokens: 0, model: resolveModel(e.model) },
      ...world.step,
    }
    yield { kind: 'stop', stopReason: result.stopReason, usage: result.usage } as never
    return result
  })
  return world
}

export async function drain(stream: AsyncIterable<unknown>): Promise<void> {
  for await (const _chunk of stream) {
    // read to the end so the plugin's generator finishes
  }
}
