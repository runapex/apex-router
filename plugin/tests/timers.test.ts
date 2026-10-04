import { describe, expect, test } from 'claude-code/testing'

import { start as backendStart } from '../hooks/backend.ts'
import type { Host } from '../hooks/host.ts'
import { start as injectStart } from '../hooks/inject.ts'
import { start as liveStart } from '../hooks/live.ts'
import { start as observeStart } from '../hooks/observe.ts'
import { start as routerStart } from '../hooks/router.ts'
import { newRuntime, optionsOf } from '../hooks/runtime.ts'
import { SESSION } from './fixtures/inputs.ts'
import { BACKEND, HOME, SESSION_ID } from './fixtures/world.ts'

/** A host whose every call is a no-op resolving to undefined, except what a test overrides. */
function stubHost(over: Record<string, unknown> = {}): Host {
  const noop = async (): Promise<undefined> => undefined
  const publish = new Proxy({}, { get: () => noop })
  return new Proxy({}, { get: (_t, key: string) => (key in over ? over[key] : key === 'publish' ? publish : key === 'now' ? async () => 1_000_000 : noop) }) as never
}

const rtReady = () => {
  const rt = newRuntime(optionsOf({}))
  rt.backendDir = BACKEND
  rt.home = HOME
  rt.sessionId = SESSION_ID
  return rt
}

describe('timer and await hygiene', () => {
  test('every module registers its timer once however often session.start runs', async () => {
    const calls: number[] = []
    const host = stubHost({ every: (ms: number) => void calls.push(ms) })
    const rt = rtReady()
    for (let i = 0; i < 3; i++) {
      await observeStart(host, SESSION, rt)
      await routerStart(host, rt)
      await injectStart(host, rt)
      await backendStart(host, rt)
      await liveStart(host, rt, () => null)
    }
    expect(calls.slice().sort((a, b) => a - b)).toEqual([1_000, 5_000, 60_000, 60_000, 60_000])
  })

  test('observe.start survives a throwing timer registration', async () => {
    const host = stubHost({ every: () => { throw new Error('no timers') } })
    await expect(observeStart(host, SESSION, rtReady())).resolves.toBeUndefined()
  })

  test('backend.start does not wait for the first poll', async () => {
    const host = stubHost({ exists: async () => true, run: () => new Promise(() => undefined), every: () => undefined })
    const outcome = await Promise.race([
      backendStart(host, rtReady()).then(() => 'returned'),
      new Promise(r => setTimeout(() => r('waited'), 200)),
    ])
    expect(outcome).toBe('returned')
  })
})
