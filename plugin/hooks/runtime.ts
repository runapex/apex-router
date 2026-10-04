import type { PluginOptions, SessionStartInput } from 'claude-code'

import type {
  AnomalyModel,
  Arm,
  BackendView,
  Breaker,
  Cell,
  Dispatch,
  LevelState,
  ProfileStore,
  TokenBucket,
  Welford,
} from '../types/index.d.ts'
import type { Host } from './host.ts'
import { EMPTY_BACKEND, EMPTY_PROFILE_STORE } from './state.ts'

export type Options = {
  pane: 'auto' | 'command' | 'off'
  budgetUsd: number
  band: boolean
  backendDir: string
}

export const DEFAULTS: Options = { pane: 'command', budgetUsd: 0, band: true, backendDir: '~/.apex-router' }

/** userConfig (§11) as typed options; anything malformed falls back to its default. */
export function optionsOf(raw: PluginOptions): Options {
  const pane = raw.pane === 'auto' || raw.pane === 'off' ? raw.pane : 'command'
  const budget = typeof raw.budgetUsd === 'number' && Number.isFinite(raw.budgetUsd) && raw.budgetUsd > 0 ? raw.budgetUsd : 0
  const dir = typeof raw.backendDir === 'string' && raw.backendDir.trim() !== '' ? raw.backendDir.trim() : DEFAULTS.backendDir
  return { pane, budgetUsd: budget, band: raw.band !== false, backendDir: dir }
}

/** One minute of the live signals' raw counts. */
export type MinuteBucket = { steps: number; failed: number; spend: number }

/** A Bash call to a backend command, as observe classified it — never its text. */
export type BashEvent = { cmd: string; signal: string | null; isError: boolean; t: number }

/**
 * A route_log row of this session, kept until it can no longer change: pending until the agent's
 * turn.complete (or session.end), then kept ≤ 2 h so a higher-tier re-dispatch can mark it escalated.
 */
export type RouteEntry = { row: Record<string, unknown>; start: string; desc: string; at: number; done: boolean }

/** Module-local session state shared by every hooks module (lost on hot reload; $.state is not). */
export type Runtime = {
  options: Options
  sessionId: string
  home: string
  /** Absolute, `~` expanded; '' until identify resolves it (every writer waits for it). */
  backendDir: string
  /** Why backend detection is off (a configured backendDir that is not absolute), or null. */
  backendRefused: string | null
  surface: string | null
  arm: Arm
  level: LevelState
  cells: Record<string, Cell>
  /** True once session.start loaded the persisted cells/stats/profile; before that nothing is persisted. */
  storeLoaded: boolean
  cellsDirty: boolean
  stats: Record<string, Record<string, Welford>>
  statsDirty: boolean
  profile: ProfileStore
  profileDirty: boolean
  rows: string[]
  routeRows: string[]
  dispatches: Map<string, Dispatch>
  /** Last dispatch-view publish (ms) and whether a newer view is waiting for the coalescing timer. */
  dispatchesPublishedAt: number
  dispatchesDirty: boolean
  byAgent: Map<string, string>
  routes: Map<string, RouteEntry>
  workflows: Map<string, number>
  injectedBytes: number
  injectedSections: number
  /** Memoised describe/compose decisions, `site|name|text` → admitted: each distinct text is decided (logged, debited) once. */
  injectDecisions: Map<string, boolean>
  toastAt: Map<string, number>
  /**
   * Timers this module lifetime already registered: each registers once. The engine fires session.start
   * once per load (never on /clear or resume) and a reload builds a fresh runtime, so this is defence only.
   */
  timers: Set<string>
  minute: MinuteBucket
  minutes: MinuteBucket[]
  lastCostUsd: number | null
  /** The session's cost when it began (first cost seen after identify): budget burn covers this session only. */
  costAtStart: number | null
  cacheRead: number
  stepFeatures: number[][]
  bashEvents: BashEvent[]
  breakers: Record<string, Breaker>
  bucket: TokenBucket
  heavySpawns: number[]
  limitHistory: { t: number; pct: number }[]
  anomaly: AnomalyModel | null
  backend: BackendView
  /** When this session was identified (ms); 0 between a /clear or resume and its first turn. */
  startedAt: number
}

export function newRuntime(options: Options): Runtime {
  return {
    options,
    sessionId: '',
    home: '',
    backendDir: '',
    backendRefused: null,
    surface: null,
    arm: 'evidence',
    level: { level: 'GREEN', up: 0, down: 0 },
    cells: {},
    storeLoaded: false,
    cellsDirty: false,
    stats: {},
    statsDirty: false,
    profile: EMPTY_PROFILE_STORE,
    profileDirty: false,
    rows: [],
    routeRows: [],
    dispatches: new Map(),
    dispatchesPublishedAt: -Infinity,
    dispatchesDirty: false,
    byAgent: new Map(),
    routes: new Map(),
    workflows: new Map(),
    injectedBytes: 0,
    injectedSections: 0,
    injectDecisions: new Map(),
    toastAt: new Map(),
    timers: new Set(),
    minute: { steps: 0, failed: 0, spend: 0 },
    minutes: [],
    lastCostUsd: null,
    costAtStart: null,
    cacheRead: 0,
    stepFeatures: [],
    bashEvents: [],
    breakers: {},
    bucket: { rate: 10, burst: 2, tokens: 2, t: 0 },
    heavySpawns: [],
    limitHistory: [],
    anomaly: null,
    backend: EMPTY_BACKEND,
    startedAt: 0,
  }
}

export const expandHome = (path: string, home: string): string =>
  path === '~' ? home : path.startsWith('~/') ? `${home}${path.slice(1)}` : path

/** The backend directory as configured: `~` expanded, then required absolute; otherwise the default, with a reason. */
export function resolveBackendDir(configured: string, home: string): { dir: string; refused: string | null } {
  const dir = expandHome(configured, home)
  if (dir.startsWith('/')) return { dir, refused: null }
  return { dir: expandHome(DEFAULTS.backendDir, home), refused: `backendDir "${configured}" is not absolute; backend detection is off` }
}

export const TOAST_EVERY_MS = 600_000

/** §6: each toast kind at most once per 10 minutes. */
export function toastOnce(host: Host, rt: Runtime, kind: string, text: string, now: number): boolean {
  const last = rt.toastAt.get(kind)
  if (last !== undefined && now - last < TOAST_EVERY_MS) return false
  rt.toastAt.set(kind, now)
  host.toast(text)
  return true
}

/** Register a recurring timer once per module lifetime (a second session.start must not stack another). */
export function everyOnce(host: Host, rt: Runtime, key: string, ms: number, fn: () => void): void {
  if (rt.timers.has(key)) return
  rt.timers.add(key)
  host.every(ms, fn)
}

/**
 * session.start, first: who and where this session is. register.ts calls it before any module.
 * The engine fires session.start once per load (a hot reload included, never /clear or resume).
 */
export async function identify(host: Host, e: SessionStartInput, rt: Runtime): Promise<void> {
  rt.home = await host.home()
  const resolved = resolveBackendDir(rt.options.backendDir, rt.home)
  rt.backendDir = resolved.dir
  rt.backendRefused = resolved.refused
  rt.sessionId = await host.sessionId()
  rt.surface = e.surface
  rt.startedAt = await host.now()
  rt.costAtStart = rt.lastCostUsd
}

/** session.end reasons after which the process goes on under a new session id, with no session.start. */
export const CONTINUES: ReadonlySet<string> = new Set(['clear', 'resume'])

/**
 * session.end{clear|resume}, after the final flush: forget who this session was and everything counted
 * per session. sessionId '' fails closed (no injection, arm stamp 'unknown') until reidentify runs.
 * Home, backendDir, persisted cells/stats/profile and the live windows are per process and stay.
 */
export function endSession(rt: Runtime): void {
  rt.sessionId = ''
  rt.startedAt = 0
  rt.injectedBytes = 0
  rt.injectedSections = 0
  rt.injectDecisions = new Map()
  rt.cacheRead = 0
  rt.stepFeatures = []
  rt.lastCostUsd = null
  rt.costAtStart = null
  rt.toastAt.delete('handoff')
}

/**
 * The first turn.start / prompt.compose after a /clear or resume: the new session's id (2 engine
 * calls, once per session; never on agent.spawn). True when it identified a new session.
 */
export async function reidentify(host: Host, rt: Runtime): Promise<boolean> {
  if (rt.sessionId !== '' || rt.backendDir === '') return false
  const id = await host.sessionId()
  const now = await host.now()
  if (rt.sessionId !== '' || id === '') return false
  rt.sessionId = id
  rt.startedAt = now
  rt.costAtStart = rt.lastCostUsd
  return true
}
