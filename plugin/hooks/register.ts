import type { EngineInterface, Register } from 'claude-code'

import { install as band, measureOf } from './band.tsx'
import type { Host } from './host.ts'
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
const ENFORCE = { plugin: 'datapce', key: 'enforce' } as const

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
      enforce: async v => void (await $.state.set(ENFORCE, v)),
    },
  }
}

export const register: Register = (on, raw) => {
  const rt = newRuntime(optionsOf(raw))

  on('session.start', async ($, e, next) => {
    const r = await next(e)
    await identify(hostOf($), e, rt)
    return r
  })

  on('session.measure', async ($, e, next) => {
    await hostOf($).publish.measure(measureOf(e))
    return next(e)
  })

  band(on, rt)
}
