// §9 backend boundary: detected, never required; polled every 60 s off the hot path; every call
// fails open. Commands: `apex-router pressure --json --no-write` (never rewrites live state), `apex-router route-advise --json` only.
import type { BackendView, LaneStats, Level } from '../types/index.d.ts'
import type { Host } from './host.ts'
import { jsonLines, readJson, tailLines } from './io.ts'
import { toastOnce, type Runtime } from './runtime.ts'
import { availability, breakerState, drainEta, series } from './signals.ts'

export const POLL_MS = 60_000
export const ADVISE_EVERY_MS = 600_000
const LEVELS: readonly Level[] = ['GREEN', 'AMBER', 'RED']
const HOUR_MS = 3_600_000
const SAMPLES_MAX = 120
const PRESSURE_FRESH_MS = 600_000

export const detectPaths = (backendDir: string, home: string): string[] => [
  `${backendDir}/ornith.env`,
  `${backendDir}/pressure.json`,
  `${home}/.apex/telemetry.jsonl`,
]

export const apexCandidates = (backendDir: string, home: string): string[] => [`${backendDir}/.venv/bin/apex-router`, `${home}/.local/bin/apex-router`]

const obj = (v: unknown): Record<string, unknown> | null => (v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null)

export function parsePressure(v: unknown): Record<string, Level> {
  const out: Record<string, Level> = {}
  const families = obj(obj(v)?.families)
  if (families === null) return out
  for (const [name, f] of Object.entries(families)) {
    const level = obj(f)?.level
    if (LEVELS.includes(level as Level)) out[name] = level as Level
  }
  return out
}

export function conformanceRate(rows: readonly Record<string, unknown>[]): number | null {
  const judged = rows.filter(r => typeof r.matched === 'boolean')
  return judged.length === 0 ? null : judged.filter(r => r.matched === true).length / judged.length
}

export function parseVerdicts(v: unknown): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [tt, rec] of Object.entries(obj(v) ?? {})) {
    const verdict = obj(rec)?.verdict
    if (typeof verdict === 'string') out[tt] = verdict
  }
  return out
}

export function parseHandoff(v: unknown): number | null {
  const t = obj(v)?.threshold_tokens
  return typeof t === 'number' && Number.isFinite(t) && t > 0 ? t : null
}

export function laneStatsOf(rows: readonly Record<string, unknown>[], nowSec: number): LaneStats | null {
  const out: LaneStats = {}
  for (const r of rows) {
    if (typeof r.ts !== 'number' || nowSec - r.ts > 86_400) continue
    const lane = typeof r.lane === 'string' ? r.lane : 'unknown'
    const s = (out[lane] ??= { n: 0, ok: 0, escalated: 0 })
    s.n += 1
    if (r.ok === true) s.ok += 1
    if (r.escalated === true) s.escalated += 1
  }
  return Object.keys(out).length > 0 ? out : null
}

export type Sample = { t: number; inbox: number }

/** Seconds until the inbox drains: backlog / (cap − arrive), both per minute over the last hour. */
export function drainEtaOf(history: readonly Sample[], completedLastHour: number, now: number): number | null {
  const recent = history.filter(s => now - s.t <= HOUR_MS)
  const first = recent[0]
  const last = recent[recent.length - 1]
  if (first === undefined || last === undefined || last.inbox === 0) return null
  const cap = completedLastHour / 60
  const minutes = (last.t - first.t) / 60_000
  const arrive = cap + (minutes > 0 ? (last.inbox - first.inbox) / minutes : 0)
  const eta = drainEta(last.inbox, cap, arrive)
  return eta === null ? null : Math.round(eta * 60)
}

export function healthOf(up: Record<string, readonly boolean[]>): number | null {
  const parts: number[] = []
  for (const samples of Object.values(up)) {
    if (samples.length === 0) continue
    const ups = samples.filter(Boolean).length
    parts.push(availability(ups, samples.length - ups))
  }
  return parts.length === 0 ? null : series(...parts)
}

export type Raw = {
  present: boolean
  families: Record<string, Level>
  conformance: number | null
  verdicts: Record<string, string>
  handoff: number | null
  lanes: LaneStats | null
  drainEtaS: number | null
  health: number | null
}

export const viewOf = (raw: Raw): BackendView => ({
  present: raw.present,
  conformance: raw.conformance,
  families: raw.families,
  lanes: raw.lanes,
  drainEtaS: raw.drainEtaS,
  health: raw.health,
  verdicts: raw.verdicts,
  handoffTokens: raw.handoff,
})

async function firstExisting(host: Host, paths: readonly string[]): Promise<string | null> {
  for (const p of paths) {
    try {
      if (await host.exists(p)) return p
    } catch {
      // keep looking
    }
  }
  return null
}

async function jsonOf(host: Host, argv: readonly string[], timeoutMs: number): Promise<unknown> {
  try {
    const r = await host.run(argv, { timeoutMs })
    return r.exitCode === 0 ? (JSON.parse(r.stdout) as unknown) : null
  } catch {
    return null
  }
}

async function finishedSince(host: Host, dir: string, since: number): Promise<number> {
  try {
    return (await host.list(dir)).filter(e => e.kind === 'file' && e.mtimeMs >= since).length
  } catch {
    return 0
  }
}

/** The state file is evidence only while young: generated_at (epoch s) if it has one, else its mtime. Stale is unknown. */
async function freshPressureFile(host: Host, path: string, now: number): Promise<unknown> {
  const v = await readJson(host, path)
  if (v === null) return null
  const at = obj(v)?.generated_at
  let ms: number | null = typeof at === 'number' && Number.isFinite(at) ? at * 1000 : null
  if (ms === null) {
    try {
      ms = (await host.stat(path)).mtimeMs
    } catch {
      ms = null
    }
  }
  return ms !== null && now - ms <= PRESSURE_FRESH_MS ? v : null
}

type PollState = { inbox: Sample[]; up: Record<string, boolean[]>; lastAdvise: number; verdicts: Record<string, string> }

async function poll(host: Host, rt: Runtime, st: PollState): Promise<void> {
  const now = await host.now()
  const present = rt.backendRefused === null && (await firstExisting(host, detectPaths(rt.backendDir, rt.home))) !== null
  if (!present) {
    await publish(host, rt, viewOf({ present: false, families: {}, conformance: null, verdicts: {}, handoff: null, lanes: null, drainEtaS: null, health: null }))
    return
  }
  const bin = await firstExisting(host, apexCandidates(rt.backendDir, rt.home))
  let pressure = bin === null ? null : await jsonOf(host, [bin, 'pressure', '--json', '--no-write'], 15_000)
  if (pressure === null) pressure = await freshPressureFile(host, `${rt.backendDir}/pressure.json`, now)
  if (bin !== null && now - st.lastAdvise >= ADVISE_EVERY_MS) {
    const advice = await jsonOf(host, [bin, 'route-advise', '--json'], 20_000)
    if (advice !== null) st.verdicts = parseVerdicts(advice)
    st.lastAdvise = now
  }
  const conformance = conformanceRate(jsonLines(await tailLines(host, `${rt.backendDir}/conformance.jsonl`, 200)))
  const handoff = parseHandoff(await readJson(host, `${rt.backendDir}/handoff_threshold.json`))
  const lanes = laneStatsOf(jsonLines(await tailLines(host, `${rt.home}/.apex/offload_telemetry.jsonl`, 2000)), now / 1000)
  const jobs = `${rt.backendDir}/queue/jobs`
  let inbox: number | null = null
  try {
    inbox = (await host.list(`${jobs}/inbox`)).length
  } catch {
    inbox = null
  }
  const finished = (await finishedSince(host, `${jobs}/done`, now - HOUR_MS)) + (await finishedSince(host, `${jobs}/failed`, now - HOUR_MS))
  if (inbox !== null) st.inbox = [...st.inbox, { t: now, inbox }].slice(-SAMPLES_MAX)
  const sample = (name: string, up: boolean): void => {
    st.up[name] = [...(st.up[name] ?? []), up].slice(-SAMPLES_MAX)
  }
  sample('proxy', pressure !== null)
  const ornith = rt.breakers.ornith
  sample('ornith', ornith === undefined || breakerState(ornith, now) !== 'open')
  if (inbox !== null) sample('worker', inbox === 0 || finished > 0)
  await publish(
    host,
    rt,
    viewOf({
      present: true,
      families: parsePressure(pressure),
      conformance,
      verdicts: st.verdicts,
      handoff,
      lanes,
      drainEtaS: drainEtaOf(st.inbox, finished, now),
      health: healthOf(st.up),
    }),
  )
}

async function publish(host: Host, rt: Runtime, view: BackendView): Promise<void> {
  rt.backend = view
  if (rt.profile.backend !== view.present) {
    rt.profile = { ...rt.profile, backend: view.present }
    rt.profileDirty = true
  }
  await host.publish.backend(view)
}

/** session.start: one poll now (with route-advise), then every 60 s. */
export async function start(host: Host, rt: Runtime): Promise<void> {
  if (rt.backendRefused !== null) toastOnce(host, rt, 'backend-dir', `datapce: ${rt.backendRefused}`, await host.now())
  const st: PollState = { inbox: [], up: {}, lastAdvise: Number.NEGATIVE_INFINITY, verdicts: {} }
  try {
    await poll(host, rt, st)
  } catch {
    // fail open: an absent or broken backend is an absent backend
  }
  host.every(POLL_MS, () => {
    void poll(host, rt, st).catch(() => undefined)
  })
}
