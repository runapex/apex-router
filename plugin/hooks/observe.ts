// §4 observation: counts and identifiers only — never prompt text, file contents or command text.
import type { AgentSpawnInput, On, SessionMeasureInput, SessionStartInput, TurnStepInput, TurnStepResult } from 'claude-code'

import type { Measure, ProfileStore } from '../types/index.d.ts'
import { measureOf } from './band.tsx'
import { welfordNew, welfordPush } from './core/stats.ts'
import { asStats, type Stats } from './evidence.ts'
import type { Host } from './host.ts'
import { appendLines, ensureDir } from './io.ts'
import { everyOnce, type BashEvent, type Runtime } from './runtime.ts'
import { EMPTY_PROFILE_STORE, profileView } from './state.ts'
import { tierOf } from './tiers.ts'

export const DESC_MAX = 120
export const RETENTION_DAYS = 30
export const FLUSH_MS = 5_000
export const MAX_PENDING = 5_000
export const ROW_MAX_BYTES = 2_048
const LIMIT_HISTORY_MS = 15 * 60_000

export type Row = Record<string, unknown>

export const dayOf = (ms: number): string => new Date(ms).toISOString().slice(0, 10)
export const observeDir = (backendDir: string): string => `${backendDir}/observe`
export const observePath = (backendDir: string, ms: number): string => `${observeDir(backendDir)}/${dayOf(ms)}.jsonl`
export const routeLogPath = (backendDir: string): string => `${backendDir}/route_log.jsonl`

export function spawnRow(
  e: Pick<AgentSpawnInput, 'tool_use_id' | 'subagentType' | 'description' | 'model' | 'parentAgentId'>,
  taskType: string,
  resolved: string | null,
  ts: number,
): Row {
  return {
    ev: 'spawn',
    ts,
    tool_use_id: e.tool_use_id,
    subagent_type: e.subagentType,
    description: e.description.slice(0, DESC_MAX),
    task_type: taskType,
    model_requested: e.model ?? null,
    model_resolved: resolved,
    parent_agent_id: e.parentAgentId ?? null,
  }
}

export function stepRow(e: Pick<TurnStepInput, 'model' | 'effort' | 'agentId'>, r: TurnStepResult, ttftMs: number | null, stepMs: number, ts: number): Row {
  return {
    ev: 'step',
    ts,
    agent_id: e.agentId ?? null,
    model: e.model,
    effort: e.effort ?? null,
    input: r.usage?.input_tokens ?? null,
    output: r.usage?.output_tokens ?? null,
    cache_read: r.usage?.cache_read_input_tokens ?? null,
    cache_write: r.usage?.cache_creation_input_tokens ?? null,
    stop: r.stopReason,
    tools: r.toolUses.map(t => t.name),
    ttft_ms: ttftMs === null ? null : Math.round(ttftMs),
    step_ms: Math.round(stepMs),
  }
}

export const measureRow = (m: Measure, ts: number): Row => ({
  ev: 'measure',
  ts,
  ctx_pct: m.ctxPercent,
  limit_kind: m.limitKind,
  limit_pct: m.limitPercent,
  resets_at: m.resetsAt,
  cost_usd: m.costUsd,
})

export const BACKEND_COMMANDS: readonly (readonly [RegExp, string])[] = [
  [/\bapex-router\s+pressure\b/, 'pressure'],
  [/\breview-preread\b/, 'review-preread'],
  [/\bapex-ornith\b/, 'ornith'],
]

export const SIGNALS: readonly (readonly [RegExp, string])[] = [
  [/OrnithBusy/, 'OrnithBusy'],
  [/timeout after/, 'timeout'],
  [/finish_reason=length/, 'truncation'],
]

export function bashEventOf(command: string, output: string, isError: boolean, t: number): BashEvent | null {
  const cmd = BACKEND_COMMANDS.find(([re]) => re.test(command))?.[1]
  if (cmd === undefined) return null
  const signal = SIGNALS.find(([re]) => re.test(output))?.[1] ?? null
  return { cmd, signal, isError, t }
}

export const bashRow = (b: BashEvent): Row => ({ ev: 'bash', ts: b.t, cmd: b.cmd, signal: b.signal, error: b.isError })
export const skillRow = (skill: string, ts: number): Row => ({ ev: 'skill', ts, skill })
/** `tokens` = tok(all): input + output + cache reads + cache writes. */
export const completeRow = (agentId: string, ok: boolean, durationMs: number, tokens: number | null, ts: number): Row => ({
  ev: 'complete',
  ts,
  agent_id: agentId,
  ok,
  duration_ms: durationMs,
  tokens,
})

/** The text a tool result carries: core's `text`, else a hook's `{ result: { stdout, stderr } }`. */
export function outputOf(r: unknown): string {
  if (r === null || typeof r !== 'object') return ''
  const o = r as { text?: unknown; result?: unknown }
  if (typeof o.text === 'string') return o.text
  if (o.result !== null && typeof o.result === 'object') {
    const res = o.result as { stdout?: unknown; stderr?: unknown }
    return `${typeof res.stdout === 'string' ? res.stdout : ''}${typeof res.stderr === 'string' ? res.stderr : ''}`
  }
  return typeof o.result === 'string' ? o.result : ''
}

export function expiredFiles(names: readonly string[], now: number, days = RETENTION_DAYS): string[] {
  const cutoff = dayOf(now - days * 86_400_000)
  return names.filter(n => /^\d{4}-\d{2}-\d{2}\.jsonl$/.test(n) && n.slice(0, 10) < cutoff)
}

const bytes = (s: string): number => new TextEncoder().encode(s).length

export function record(rt: Runtime, row: Row): void {
  let line = JSON.stringify(row)
  if (bytes(line) > ROW_MAX_BYTES) line = JSON.stringify({ ev: row.ev, ts: row.ts, truncated: true })
  rt.rows.push(line)
  if (rt.rows.length > MAX_PENDING) rt.rows.splice(0, rt.rows.length - MAX_PENDING)
}

export function pushStat(stats: Stats, key: string, metric: string, x: number | null | undefined): void {
  if (typeof x !== 'number' || !Number.isFinite(x)) return
  const m = (stats[key] ??= {})
  m[metric] = welfordPush(m[metric] ?? welfordNew(), x)
}

export function noteStep(rt: Runtime, e: TurnStepInput, r: TurnStepResult, ttftMs: number | null, stepMs: number, ts: number): void {
  record(rt, stepRow(e, r, ttftMs, stepMs, ts))
  rt.minute.steps += 1
  if (r.stopReason === null) rt.minute.failed += 1
  const tier = tierOf(r.usage?.model ?? e.model) ?? 'other'
  pushStat(rt.stats, `step|${tier}`, 'output', r.usage?.output_tokens)
  pushStat(rt.stats, `step|${tier}`, 'cache_read', r.usage?.cache_read_input_tokens)
  rt.statsDirty = true
  if (e.agentId === undefined && r.usage !== null) {
    rt.cacheRead += r.usage.cache_read_input_tokens
    rt.stepFeatures.push([r.usage.cache_read_input_tokens, r.usage.output_tokens, ttftMs ?? stepMs, r.toolUses.length, stepMs])
    if (rt.stepFeatures.length > 500) rt.stepFeatures.splice(0, rt.stepFeatures.length - 500)
  }
}

const isCount = (x: unknown): x is number => typeof x === 'number' && Number.isInteger(x) && x >= 0

function counts(v: unknown): Record<string, number> {
  const out: Record<string, number> = {}
  if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
    for (const [k, n] of Object.entries(v as Record<string, unknown>)) if (isCount(n)) out[k] = n
  }
  return out
}

const SALT = /^[0-9a-f]{32}$/
const TOKEN = /^[0-9a-f]{16}$/

/**
 * The persisted profile, field by field. Repo tokens are kept only beside the salt that keyed them:
 * a profile without a valid salt is a 0.4.0 one (unsalted path hashes) or a damaged one, and its
 * repos are dropped (the repos-seen count restarts; the other counts are kept).
 */
export function asProfile(v: unknown): ProfileStore {
  const p = v !== null && typeof v === 'object' ? (v as Record<string, unknown>) : {}
  const hours = Array.isArray(p.hours) && p.hours.length === 24 && p.hours.every(isCount) ? (p.hours as number[]) : EMPTY_PROFILE_STORE.hours
  const base = { repos: [] as string[], taskMix: counts(p.taskMix), skills: counts(p.skills), hours, backend: p.backend === true }
  if (typeof p.salt !== 'string' || !SALT.test(p.salt)) return base
  const repos = Array.isArray(p.repos) ? p.repos.filter((r): r is string => typeof r === 'string' && TOKEN.test(r)).slice(-200) : []
  return { ...base, repos, salt: p.salt }
}

const hex = (bytes: Uint8Array): string => [...bytes].map(b => b.toString(16).padStart(2, '0')).join('')

/** A per-install salt: 16 bytes from the engine's crypto.getRandomValues (a CSPRNG), as 32 hex characters. */
export const newSalt = (): string => hex(crypto.getRandomValues(new Uint8Array(16)))

function joined(a: Uint8Array, b: Uint8Array): Uint8Array {
  const out = new Uint8Array(a.length + b.length)
  out.set(a)
  out.set(b, a.length)
  return out
}

/**
 * HMAC-SHA-256 (RFC 2104) built on SHA-256: the engine's crypto.subtle offers digest only, no
 * importKey or sign. Checked against RFC 4231 in the tests.
 */
export async function hmacSha256(key: Uint8Array, msg: Uint8Array): Promise<Uint8Array> {
  const k = key.length > 64 ? new Uint8Array(await crypto.subtle.digest('SHA-256', key)) : key
  const pad = (x: number): Uint8Array => Uint8Array.from({ length: 64 }, (_, i) => (k[i] ?? 0) ^ x)
  const inner = new Uint8Array(await crypto.subtle.digest('SHA-256', joined(pad(0x36), msg)))
  return new Uint8Array(await crypto.subtle.digest('SHA-256', joined(pad(0x5c), inner)))
}

/**
 * A repo token: the first 8 bytes (16 hex) of HMAC-SHA-256(salt, path), the salt this install's own.
 * Without this machine's salt a list of candidate paths cannot be matched to it; with the salt (it
 * sits in the same local plugin store) it can, so the token hides where you work only from someone
 * who does not have this machine's store. Nothing leaves the machine. Runs once per session.start.
 */
export async function repoToken(path: string, salt: string): Promise<string> {
  const key = Uint8Array.from(salt.match(/../g) ?? [], h => parseInt(h, 16))
  return hex((await hmacSha256(key, new TextEncoder().encode(path))).slice(0, 8))
}

function keep(into: string[], rows: string[]): void {
  into.unshift(...rows)
  if (into.length > MAX_PENDING) into.splice(0, into.length - MAX_PENDING)
}

export async function flushAll(host: Host, rt: Runtime): Promise<void> {
  // Until identify resolved the directory rows stay buffered; a raw "~/…" would be a relative path.
  if (rt.backendDir !== '') {
    const now = await host.now()
    const rows = rt.rows.splice(0)
    if (rows.length > 0 && !(await appendLines(host, observePath(rt.backendDir, now), rows))) keep(rt.rows, rows)
    const route = rt.routeRows.splice(0)
    if (route.length > 0 && !(await appendLines(host, routeLogPath(rt.backendDir), route))) keep(rt.routeRows, route)
  }
  // Each persisted value is written only once session.start read it: writing one that was never read
  // (before session.start, or a read that failed) would replace what is stored.
  if (!rt.storeLoaded) return
  try {
    if (rt.statsDirty && rt.statsLoaded) {
      rt.statsDirty = false
      await host.storeSet('datapce.stats', rt.stats)
    }
    if (rt.cellsDirty) {
      rt.cellsDirty = false
      await host.storeSet('datapce.cells', rt.cells)
    }
    if (rt.profileDirty && rt.profileLoaded) {
      rt.profileDirty = false
      // Repo tokens are written only beside their salt: never a repo list without one.
      await host.storeSet('datapce.profile', rt.profile.salt === undefined ? { ...rt.profile, repos: [] } : rt.profile)
      await host.publish.profile(profileView(rt.profile))
    }
  } catch {
    // fail open: the next change marks the value dirty again
  }
}

/** session.start (from register.ts, after identify): directory, retention, persisted state, flush timer. */
export async function start(host: Host, e: SessionStartInput, rt: Runtime): Promise<void> {
  try {
    if (rt.backendDir === '') throw new Error('backendDir unresolved')
    const dir = observeDir(rt.backendDir)
    await ensureDir(host, dir)
    const old = expiredFiles((await host.list(dir)).map(entry => entry.name), await host.now())
    if (old.length > 0) await host.run(['/bin/rm', '-f', '--', ...old.map(n => `${dir}/${n}`)], { timeoutMs: 5000 })
  } catch {
    // retention is best effort
  }
  // Stats and profile load apart: one that cannot be read starts empty and is never persisted this
  // module lifetime, so the other still loads and a flush cannot overwrite the unread one.
  try {
    rt.stats = asStats(await host.storeGet('datapce.stats'))
    rt.statsLoaded = true
  } catch {
    // unreadable: counted in memory only
  }
  try {
    const read = asProfile(await host.storeGet('datapce.profile'))
    // First load after 0.4.0 (or a damaged salt): asProfile dropped the repos; draw the salt once.
    const salt = read.salt ?? newSalt()
    const p = { ...read, salt }
    const token = await repoToken(e.cwd, salt)
    rt.profile = p.repos.includes(token) ? p : { ...p, repos: [...p.repos, token].slice(-200) }
    rt.profileLoaded = true
    rt.profileDirty = true
  } catch {
    // unreadable: counted in memory only
  }
  try {
    everyOnce(host, rt, 'observe.flush', FLUSH_MS, () => {
      void flushAll(host, rt)
    })
  } catch {
    // no flush timer: rows stay buffered and the session.end flush still writes them
  }
}

/** session.measure (from register.ts): the row, the spend delta, the rate-limit sample. */
export function measure(rt: Runtime, e: SessionMeasureInput, now: number): void {
  try {
    const m = measureOf(e)
    record(rt, measureRow(m, now))
    if (m.costUsd !== null) {
      if (rt.lastCostUsd !== null) rt.minute.spend += Math.max(0, m.costUsd - rt.lastCostUsd)
      rt.lastCostUsd = m.costUsd
      rt.costAtStart ??= m.costUsd
    }
    if (m.limitPercent !== null) {
      rt.limitHistory.push({ t: now, pct: m.limitPercent })
      rt.limitHistory = rt.limitHistory.filter(s => now - s.t <= LIMIT_HISTORY_MS)
    }
  } catch {
    // observation never breaks a measurement
  }
}

/** skill.prompt (from register.ts): which workflows are in use on this machine. */
export function skill(rt: Runtime, name: string, now: number): void {
  try {
    record(rt, skillRow(name, now))
    rt.profile = { ...rt.profile, skills: { ...rt.profile.skills, [name]: (rt.profile.skills[name] ?? 0) + 1 } }
    rt.profileDirty = true
  } catch {
    // observation never breaks a skill
  }
}

/** Events only observe hooks: turn.step (streaming) and tool.call on Bash. */
export function install(on: On, rt: Runtime): void {
  on('turn.step', async function* ($, e, next) {
    const t0 = performance.now()
    let ttft: number | null = null
    const stream = next(e)
    for await (const chunk of stream) {
      if (ttft === null) ttft = performance.now() - t0
      yield chunk
    }
    const result = await stream.result
    try {
      noteStep(rt, e, result, ttft, performance.now() - t0, await $.clock.now())
    } catch {
      // observation never breaks a turn
    }
    return result
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const r = await next(e)
    try {
      const command = e.tool === 'Bash' ? e.command : ''
      const ev = bashEventOf(command, outputOf(r), 'isError' in r && r.isError === true, await $.clock.now())
      if (ev !== null) {
        rt.bashEvents.push(ev)
        record(rt, bashRow(ev))
      }
    } catch {
      // never deny, never rewrite (§10)
    }
    return r
  })
}
