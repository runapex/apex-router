// §5 routing at agent.spawn, advise-only in v1 (pivot P1): the spawn reaches the engine exactly as
// requested; advice is text (dispatch view) and string fields on the route row. Hard limits are
// checked before any advice is computed. All I/O goes through the Host; decideSpawn is pure (the §17
// latency budget). Route rows are finalized at turn.complete (P4) with ok|error from structured
// fields only — never from turn.complete.answer.
import type { AgentSpawnInput, AgentSpawnResult, On, TurnCompleteInput } from 'claude-code'

import type { Cell, Dispatch, Level, TaskType, Tier } from '../types/index.d.ts'
import { classifyDispatch, promptHeadOf } from './classify.ts'
import { adviceFor, asCells, cellKey, cellViews, recordOutcome, type Advice, type Stats } from './evidence.ts'
import type { Host } from './host.ts'
import { armStamp } from './inject.ts'
import { completeRow, DESC_MAX, pushStat, record, spawnRow, type Row } from './observe.ts'
import type { RouteEntry, Runtime } from './runtime.ts'
import { bucketTake } from './signals.ts'
import { rankOf, tierOf } from './tiers.ts'

export type SpawnContext = { level: Level; cells: Record<string, Cell>; stats: Stats; admitted: boolean }

export type Decision = {
  taskType: TaskType
  requested: string
  requestedTier: Tier | null
  advice: Advice | null
  serialize: boolean
  hardLimit: string | null
}

export type Finish = { outcome: 'ok' | 'error'; errorKind: 'unavailable' | 'other' | null }

const HEAVY: ReadonlySet<Tier> = new Set<Tier>(['opus', 'fable'])
const HEAVY_WINDOW_MS = 10 * 60_000
const DISPATCH_VIEW_MAX = 200
/** route_join: only an explicit cheap start can be escalated; a redo after 2 h is a new task. */
export const CHEAP_START_TIERS: ReadonlySet<string> = new Set(['haiku', 'sonnet'])
export const ESCALATION_WINDOW_MS = 2 * 3600_000
/**
 * A Workflow run gives no end signal. An unknown agent is attributed to a run only if it STARTED
 * (now − durationMs) after the run's launch (+ clock slack): an agent that survived a reload started
 * earlier and is ignored. Launch times never move; the expiry only bounds memory.
 */
export const WORKFLOW_SLACK_MS = 2_000
export const WORKFLOW_EXPIRY_MS = 24 * 3600_000
const EXPLICIT = 'explicit model'

type SpawnFields = Pick<AgentSpawnInput, 'subagentType' | 'description' | 'prompt' | 'model' | 'parentModel' | 'fork'>

export function hardLimitOf(e: Pick<AgentSpawnInput, 'model' | 'fork'>, requestedTier: Tier | null): string | null {
  if (e.fork) return 'a fork inherits the parent model'
  if (e.model !== undefined) return EXPLICIT
  if (requestedTier === 'fable') return 'Fable is never downshifted silently'
  if (requestedTier === null) return 'unknown tier'
  return null
}

export function decideSpawn(e: SpawnFields, ctx: SpawnContext): Decision {
  const taskType = classifyDispatch(e.subagentType, e.description, promptHeadOf(e.prompt))
  const requestedTier = tierOf(e.model ?? e.parentModel)
  // Hard limits first: a fork, a Fable request or an unknown tier gets no advice at all; an explicit
  // model keeps advice as text only. v1 never rewrites a spawn. Any future enforce must apply only
  // an own READY cell with cell.n >= MIN_N (asCells accepts a hand-edited READY with n < MIN_N), and
  // only when hardLimit === null.
  const hardLimit = hardLimitOf(e, requestedTier)
  const advisable = (hardLimit === null || hardLimit === EXPLICIT) && requestedTier !== 'fable'
  const advice = advisable ? adviceFor(ctx.cells, ctx.stats, taskType, ctx.level, requestedTier) : null
  const heavy = requestedTier !== null && HEAVY.has(requestedTier)
  return {
    taskType,
    requested: e.model?.toLowerCase() ?? 'inherit',
    requestedTier,
    advice,
    serialize: heavy && (ctx.level === 'RED' || !ctx.admitted),
    hardLimit,
  }
}

/** route_join's tier of a later dispatch: `requested or tier_of(resolved)` (inherit/unknown → resolved). */
export const escalationTierOf = (requested: string, resolved: string): Tier | null =>
  (requested !== 'inherit' ? tierOf(requested) : null) ?? tierOf(resolved)

/** route_join.normalize_description: lowercase, non-alphanumerics → space, collapsed. */
export function normalizeDescription(desc: unknown): string {
  if (typeof desc !== 'string') return ''
  return desc.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim().split(' ').filter(w => w !== '').join(' ')
}

/**
 * ok iff the run answered and was not interrupted. An error with no usage at all means no request of
 * the run was served (the "model unavailable" shape: overload, rate limit); any other end is 'other'.
 */
export function finishOf(e: Pick<TurnCompleteInput, 'reason' | 'isAborted' | 'usage'>): Finish {
  if (e.reason === 'answer' && !e.isAborted) return { outcome: 'ok', errorKind: null }
  return { outcome: 'error', errorKind: e.reason === 'error' && e.usage === undefined ? 'unavailable' : 'other' }
}

/** One route_log.jsonl row in the schema route_join parses: optional fields are strings or absent.
 * Written with outcome 'async'; finishRow replaces it at turn.complete. */
export function routeRow(e: AgentSpawnInput, d: Decision, resolved: string, agentId: string | null, rt: Runtime, ts: number): Row {
  const row: Row = {
    ts: ts / 1000,
    task_type: d.taskType,
    model: d.requested,
    passed: null,
    escalated: false,
    note: `agent:${e.subagentType || 'general-purpose'}`,
    label_pending: true,
    outcome: 'async',
    surface: 'claude-code',
    tool_use_id: e.tool_use_id,
    start_tier: d.requested,
    description: e.description.slice(0, DESC_MAX),
    resolved_model: resolved,
    applied: 'no',
    inject_arm: armStamp(rt),
    injected: rt.injectedSections > 0 ? 'yes' : 'no',
  }
  if (rt.sessionId !== '') row.session_id = rt.sessionId
  if (agentId !== null) row.agent_id = agentId
  if (e.parentAgentId !== undefined) row.parent_agent_id = e.parentAgentId
  if (d.advice !== null) {
    row.advice_tier = d.advice.tier
    row.advice_state = d.advice.confidence
  }
  return row
}

/** A Workflow agent() run: no agent.spawn, so no request, no description; the tier is the one that ran. */
export function workflowRow(agentId: string, model: string | null, toolUseId: string | null, rt: Runtime, ts: number): Row {
  const row: Row = {
    ts: ts / 1000,
    task_type: 'other',
    model: tierOf(model) ?? 'unknown',
    passed: null,
    escalated: false,
    note: 'workflow',
    label_pending: true,
    outcome: 'async',
    surface: 'claude-code',
    source: 'workflow',
    start_tier: 'unknown',
    agent_id: agentId,
    applied: 'no',
    inject_arm: armStamp(rt),
    injected: rt.injectedSections > 0 ? 'yes' : 'no',
  }
  if (model !== null) row.resolved_model = model
  if (toolUseId !== null) row.tool_use_id = toolUseId
  if (rt.sessionId !== '') row.session_id = rt.sessionId
  return row
}

export function finishRow(row: Row, e: Pick<TurnCompleteInput, 'reason' | 'isAborted' | 'usage'>): Row {
  const f = finishOf(e)
  const out: Row = { ...row, outcome: f.outcome, end_reason: e.reason }
  if (f.errorKind !== null) out.error_kind = f.errorKind
  return out
}

/** Before next(): admission bookkeeping for heavy spawns, then the pure decision. */
export function beforeSpawn(rt: Runtime, e: AgentSpawnInput, now: number): Decision {
  const requestedTier = tierOf(e.model ?? e.parentModel)
  let admitted = true
  if (requestedTier !== null && HEAVY.has(requestedTier)) {
    const r = bucketTake(rt.bucket, now / 60_000)
    rt.bucket = r.bucket
    admitted = r.allowed
    rt.heavySpawns = [...rt.heavySpawns.filter(t => now - t <= HEAVY_WINDOW_MS), now]
  }
  return decideSpawn(e, { level: rt.level.level, cells: rt.cells, stats: rt.stats, admitted })
}

async function label(host: Host, rt: Runtime, key: string, passed: boolean): Promise<void> {
  // Advise-only (P1): the cell is a ledger (n, ok%); a promotion or demotion announces nothing.
  recordOutcome(rt.cells, key, passed)
  rt.cellsDirty = true
  await host.publish.cells(cellViews(rt.cells, rt.stats))
}

const dispatchList = (rt: Runtime): Dispatch[] => [...rt.dispatches.values()].slice(-DISPATCH_VIEW_MAX)

export const DISPATCH_PUBLISH_MS = 1000
export const DISPATCH_KEEP_MAX = 500

/** Bound rt.dispatches: every running dispatch stays, the newest finished ones fill the rest. */
function trimDispatches(rt: Runtime): void {
  let excess = rt.dispatches.size - DISPATCH_KEEP_MAX
  if (excess <= 0) return
  for (const [id, d] of rt.dispatches) {
    if (excess <= 0) break
    if (d.outcome === 'running') continue
    rt.dispatches.delete(id)
    excess -= 1
  }
}

/**
 * The dispatch view is up to 200 rows and publishing it clones them, so it is coalesced: the first
 * change publishes at once, later ones within 1 s wait for the timer (and session.end). Nothing
 * on the agent.spawn path serialises more than a single publish per window.
 */
async function publishDispatches(host: Host, rt: Runtime, now: number): Promise<void> {
  if (now - rt.dispatchesPublishedAt < DISPATCH_PUBLISH_MS) {
    rt.dispatchesDirty = true
    return
  }
  rt.dispatchesPublishedAt = now
  rt.dispatchesDirty = false
  await host.publish.dispatches(dispatchList(rt))
}

/** Publish the latest view if one is waiting (timer tick, session.end). */
export async function flushDispatches(host: Host, rt: Runtime): Promise<void> {
  try {
    if (!rt.dispatchesDirty) return
    rt.dispatchesPublishedAt = await host.now()
    rt.dispatchesDirty = false
    await host.publish.dispatches(dispatchList(rt))
  } catch {
    // the next change or tick republishes
  }
}

function queue(rt: Runtime, entry: RouteEntry): void {
  entry.done = true
  rt.routeRows.push(JSON.stringify(entry.row))
}

/**
 * route_join's escalation rule, in session: an earlier row that started on an explicit cheap tier is
 * escalated by a later dispatch (≤ 2 h) with the same normalized description at a strictly higher
 * tier (explicit, else resolved). A row already flushed stays as written (route_join infers it offline).
 */
function markEscalations(rt: Runtime, desc: string, tier: Tier | null, toolUseId: string, now: number): void {
  if (desc === '' || tier === null) return
  for (const a of rt.routes.values()) {
    const dt = now - a.at
    if (a.row.escalated === true || a.desc !== desc || dt <= 0 || dt > ESCALATION_WINDOW_MS) continue
    if (!CHEAP_START_TIERS.has(a.start) || rankOf(tier) <= rankOf(a.start as Tier)) continue
    const before = a.done ? JSON.stringify(a.row) : null
    a.row = { ...a.row, escalated: true, escalated_by: toolUseId }
    if (before !== null) {
      const i = rt.routeRows.indexOf(before)
      if (i >= 0) rt.routeRows[i] = JSON.stringify(a.row)
    }
  }
}

function pruneRoutes(rt: Runtime, now: number): void {
  for (const [id, a] of rt.routes) if (a.done && now - a.at > ESCALATION_WINDOW_MS) rt.routes.delete(id)
}

/** After next(): the dispatch view, the observe row, the pending route row, escalation of earlier rows. */
export async function afterSpawn(host: Host, rt: Runtime, e: AgentSpawnInput, d: Decision, res: AgentSpawnResult, now: number): Promise<void> {
  try {
    const resolved = res.deny === undefined ? res.model : null
    const agentId = res.deny === undefined ? (res.agentId ?? null) : null
    const notes = [
      d.advice?.basis,
      d.serialize ? 'serialize heavy fan-out' : undefined,
      d.advice !== null && d.hardLimit !== null ? `advise only: ${d.hardLimit}` : undefined,
    ].filter((x): x is string => x !== undefined)
    rt.dispatches.set(e.tool_use_id, {
      toolUseId: e.tool_use_id,
      agentId,
      description: e.description.slice(0, DESC_MAX),
      subagentType: e.subagentType,
      taskType: d.taskType,
      requested: d.requested,
      resolved,
      advised: d.advice?.tier ?? null,
      advisedState: d.advice?.confidence ?? null,
      applied: false,
      basis: notes.join('; '),
      outcome: resolved === null ? 'failed' : 'running',
      startedAt: now,
      durationMs: null,
      tokens: null,
      level: rt.level.level,
    })
    record(rt, spawnRow(e, d.taskType, resolved, now))
    // A denied spawn was never made: no route row, no cell, no stat.
    if (resolved !== null) {
      pruneRoutes(rt, now)
      const desc = normalizeDescription(e.description.slice(0, DESC_MAX))
      const tier = escalationTierOf(d.requested, resolved)
      markEscalations(rt, desc, tier, e.tool_use_id, now)
      rt.routes.set(e.tool_use_id, { row: routeRow(e, d, resolved, agentId, rt, now), start: d.requested, desc, at: now, done: false })
      if (agentId !== null) rt.byAgent.set(agentId, e.tool_use_id)
    }
    rt.profile = { ...rt.profile, taskMix: { ...rt.profile.taskMix, [d.taskType]: (rt.profile.taskMix[d.taskType] ?? 0) + 1 } }
    rt.profileDirty = true
    trimDispatches(rt)
    await publishDispatches(host, rt, now)
  } catch {
    // fail open: the spawn already happened
  }
}

/** Runs launched no later than the agent's start (+ slack); expired runs are forgotten. */
function workflowsBefore(rt: Runtime, agentStart: number, now: number): string[] {
  for (const [id, at] of rt.workflows) if (now - at > WORKFLOW_EXPIRY_MS) rt.workflows.delete(id)
  return [...rt.workflows].filter(([, at]) => at <= agentStart + WORKFLOW_SLACK_MS).map(([id]) => id)
}

/** tool.call Workflow: a run whose agent() calls will raise turn.complete with agentIds never spawned. */
export function workflowStarted(rt: Runtime, toolUseId: string, now: number): void {
  rt.workflows.set(toolUseId, now)
}

/**
 * turn.complete of a subagent: its outcome labels the cell of the tier that ran and finalizes its route
 * row. An agentId never spawned is a Workflow agent while a run is active, else (hot reload) ignored.
 */
export async function onComplete(host: Host, rt: Runtime, e: TurnCompleteInput, now: number): Promise<void> {
  try {
    if (e.agentId === undefined || e.agentId === null) return
    const f = finishOf(e)
    const tokens = e.usage === undefined ? null : e.usage.input_tokens + e.usage.output_tokens
    let key = rt.byAgent.get(e.agentId)
    let d = key === undefined ? undefined : rt.dispatches.get(key)
    if (key !== undefined && (d === undefined || d.outcome !== 'running')) return
    if (key === undefined || d === undefined) {
      const runs = workflowsBefore(rt, now - e.durationMs, now)
      if (runs.length === 0) return
      const toolUseId = runs.length === 1 ? runs[0]! : null
      const model = e.usage?.model ?? null
      key = `wf:${e.agentId}`
      d = {
        toolUseId: toolUseId ?? key,
        agentId: e.agentId,
        description: '',
        subagentType: 'workflow',
        taskType: 'other',
        requested: 'unknown',
        resolved: model,
        advised: null,
        advisedState: null,
        applied: false,
        basis: 'workflow agent',
        outcome: 'running',
        startedAt: now - e.durationMs,
        durationMs: null,
        tokens: null,
        level: rt.level.level,
      }
      rt.byAgent.set(e.agentId, key)
      rt.routes.set(key, { row: workflowRow(e.agentId, model, toolUseId, rt, now), start: 'unknown', desc: '', at: now, done: false })
    }
    rt.dispatches.set(key, { ...d, outcome: f.outcome === 'ok' ? 'ok' : 'failed', durationMs: e.durationMs, tokens })
    const route = rt.routes.get(key)
    if (route !== undefined && !route.done) {
      route.row = finishRow(route.row, e)
      queue(rt, route)
    }
    const tier = tierOf(d.resolved)
    // Until start() has loaded the persisted cells, a label would be flushed over them (hot reload).
    if (tier !== null && rt.storeLoaded) {
      const cell = cellKey(d.taskType, d.level, tier)
      pushStat(rt.stats, cell, 'tokens', tokens)
      pushStat(rt.stats, cell, 'duration_ms', e.durationMs)
      pushStat(rt.stats, cell, 'unavailable', f.errorKind === 'unavailable' ? 1 : 0)
      rt.statsDirty = true
      await label(host, rt, cell, f.outcome === 'ok')
    }
    record(rt, completeRow(e.agentId, f.outcome === 'ok', e.durationMs, tokens, now))
    await publishDispatches(host, rt, now)
  } catch {
    // fail open: a label lost is a label lost, never a broken turn
  }
}

/** session.end (before the final flush): agents still running are written as 'async'. */
export function end(rt: Runtime): void {
  try {
    for (const route of rt.routes.values()) if (!route.done) queue(rt, route)
    // The next session (after /clear) shares neither runs nor escalation candidates with this one.
    rt.routes.clear()
    rt.workflows.clear()
  } catch {
    // fail open
  }
}

/** session.start (after observe.start): persisted cells. Advise-only: no enforce switch is read. */
export async function start(host: Host, rt: Runtime): Promise<void> {
  try {
    rt.cells = asCells(await host.storeGet('datapce.cells'))
    rt.storeLoaded = true
    await host.publish.cells(cellViews(rt.cells, rt.stats))
    host.every(DISPATCH_PUBLISH_MS, () => void flushDispatches(host, rt))
  } catch {
    // a store that cannot be read starts with no cells
  }
}

/** Events only the router hooks: tool.call on Workflow (observed, never denied or rewritten). */
export function install(on: On, rt: Runtime): void {
  on('tool.call', { tool: 'Workflow' }, async ($, e, next) => {
    try {
      workflowStarted(rt, e.tool_use_id, await $.clock.now())
    } catch {
      // observation never breaks a tool call
    }
    return next(e)
  })
}
