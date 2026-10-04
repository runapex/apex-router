import type { EngineInterface, Register } from 'claude-code'

import type { Measure } from '../types/index.d.ts'
import { start as backendStart } from './backend.ts'
import { install as band, measureOf, refreshStatus } from './band.tsx'
import { checkHandoff } from './handoff.ts'
import type { Host } from './host.ts'
import { start as liveStart } from './live.ts'
import {
  flushAll,
  install as observe,
  measure as observeMeasure,
  skill as observeSkill,
  start as observeStart,
} from './observe.ts'
import {
  afterSpawn,
  beforeSpawn,
  end as routerEnd,
  install as router,
  onComplete,
  start as routerStart,
  type Decision,
} from './router.ts'
import { identify, newRuntime, optionsOf } from './runtime.ts'

// Wiring only. Shared events are registered once here (engine rule) and every engine call the
// modules make is bound into a Host here (the engine follows `$` only within this file).

const MEASURE = { plugin: 'datapce', key: 'measure' } as const
const DISPATCHES = { plugin: 'datapce', key: 'dispatches' } as const
const CELLS = { plugin: 'datapce', key: 'cells' } as const
const SIGNALS = { plugin: 'datapce', key: 'signals' } as const
const BACKEND = { plugin: 'datapce', key: 'backend' } as const
const PROFILE = { plugin: 'datapce', key: 'profile' } as const
const INJECT = { plugin: 'datapce', key: 'inject' } as const

function hostOf($: EngineInterface): Host {
  return {
    now: () => $.clock.now(),
    every: (ms, fn) => $.clock.every(ms, fn),
    home: async () => (await $.env.get('HOME')) ?? '',
    sessionId: () => $.session.id(),
    read: path => $.fs.read(path),
    write: (path, text) => $.fs.write(path, text),
    exists: path => $.fs.exists(path),
    list: path => $.fs.list(path),
    stat: path => $.fs.stat(path),
    run: (argv, init) => $.process.run(argv, init),
    storeGet: key => $.store.get(key),
    storeSet: (key, value) => $.store.set(key, value),
    toast: text => $.ui.toast(text),
    status: text => $.ui.status(text),
    invalidate: event => $.ui.invalidate(event),
    open: args => $.ui.open(args),
    close: id => $.ui.close({ id }),
    registerCommand: spec => $.command.register(spec),
    publish: {
      measure: async v => void (await $.state.set(MEASURE, v)),
      dispatches: async v => void (await $.state.set(DISPATCHES, v)),
      cells: async v => void (await $.state.set(CELLS, v)),
      signals: async v => void (await $.state.set(SIGNALS, v)),
      backend: async v => void (await $.state.set(BACKEND, v)),
      profile: async v => void (await $.state.set(PROFILE, v)),
      inject: async v => void (await $.state.set(INJECT, v)),
    },
  }
}

export const register: Register = (on, raw) => {
  const rt = newRuntime(optionsOf(raw))
  let lastMeasure: Measure | null = null

  on('session.start', async ($, e, next) => {
    const r = await next(e)
    const host = hostOf($)
    await identify(host, e, rt)
    await observeStart(host, e, rt)
    await routerStart(host, rt)
    await backendStart(host, rt)
    await liveStart(host, rt, () => lastMeasure)
    return r
  })

  on('session.end', async ($, e, next) => {
    routerEnd(rt)
    await flushAll(hostOf($), rt)
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    const host = hostOf($)
    lastMeasure = measureOf(e)
    await host.publish.measure(lastMeasure)
    const now = await host.now()
    observeMeasure(rt, e, now)
    refreshStatus(host, rt, lastMeasure)
    checkHandoff(host, rt, now)
    return next(e)
  })

  on('skill.prompt', async ($, e, next) => {
    const r = await next(e)
    observeSkill(rt, e.skill, await hostOf($).now())
    return r
  })

  // Advise-only (pivot P1): the spawn always reaches the engine exactly as requested.
  on('agent.spawn', async ($, e, next) => {
    const host = hostOf($)
    let d: Decision | null = null
    try {
      d = beforeSpawn(rt, e, await host.now())
    } catch {
      d = null
    }
    const res = await next(e)
    if (d !== null) await afterSpawn(host, rt, e, d, res, await host.now())
    return res
  })

  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    const host = hostOf($)
    await onComplete(host, rt, e, await host.now())
    return r
  })

  band(on, rt)
  observe(on, rt)
  router(on, rt)
}
