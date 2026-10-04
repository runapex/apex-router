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
  /** Absolute, `~` expanded; '' until identify resolves it (a hot reload skips identify: every writer waits). */
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
  byAgent: Map<string, string>
  routes: Map<string, RouteEntry>
  workflows: Map<string, number>
  injectedBytes: number
  injectedSections: number
  toastAt: Map<string, number>
  minute: MinuteBucket
  minutes: MinuteBucket[]
  lastCostUsd: number | null
  cacheRead: number
  stepFeatures: number[][]
  bashEvents: BashEvent[]
  breakers: Record<string, Breaker>
  bucket: TokenBucket
  heavySpawns: number[]
  limitHistory: { t: number; pct: number }[]
  anomaly: AnomalyModel | null
  backend: BackendView
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
    byAgent: new Map(),
    routes: new Map(),
    workflows: new Map(),
    injectedBytes: 0,
    injectedSections: 0,
    toastAt: new Map(),
    minute: { steps: 0, failed: 0, spend: 0 },
    minutes: [],
    lastCostUsd: null,
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

/** session.start, first: who and where this session is. register.ts calls it before any module. */
export async function identify(host: Host, e: SessionStartInput, rt: Runtime): Promise<void> {
  rt.home = await host.home()
  const resolved = resolveBackendDir(rt.options.backendDir, rt.home)
  rt.backendDir = resolved.dir
  rt.backendRefused = resolved.refused
  rt.sessionId = await host.sessionId()
  rt.surface = e.surface
  rt.startedAt = await host.now()
}
