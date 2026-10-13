// Experiment E-D (docs/research/2026-10-12-rsi-next-steps.md §3): a randomised W0-vs-W2 workflow arm
// per typed main-session turn. W0 = do the turn's task in this session, no subagents; W2 = delegate its
// separable parts to subagents. The arm is a mixed hash of (session id, turn number): reproducible from
// the row, a fair coin per turn, independent of the task. It reaches the model as one context block
// beside the prompt (`prompt.submit` context: read by the model, never shown, no system-prompt cache
// break), so the analysis is intention-to-treat, within task type, token-matched
// (`apex-router worldmodel ed`). One row per turn goes to <backendDir>/labels/ed_arms.jsonl:
// identifiers, times and counts — never the prompt text. Tokens come from turn.step usage only (main
// agent, and the subagents spawned in the turn, background ones included; a Workflow agent, which
// has no agent.spawn, is bound by its first step while the turn is open); a turn's row is written
// once its last subagent completed, else at session end with `subs_open` set. A submitted prompt is
// matched to its turn.start by a hash of the text kept in memory, never by queue order. Holdout
// sessions of the evidence experiment (inject.ts) get no arm: the holdout never injects. Advise-only:
// nothing is denied or rewritten (§10). Off by default; userConfig `ed: true` turns it on.
import type { PromptComposeInput, PromptSubmitInput, TurnCompleteInput, TurnStartInput } from 'claude-code'

import type { Host } from './host.ts'
import { armOf, armStamp, fnv1a } from './inject.ts'
import { appendLines, ensureDir } from './io.ts'
import type { Runtime } from './runtime.ts'

export type EdArm = 'W0' | 'W2'

export type EdState = {
  /** 1-based count of typed main-session turns since this session was identified. */
  turn: number
  arm: EdArm
  /** fnv1a of the submitted text, matched against turn.start's text; in memory only, never written. */
  key: number
  /** prompt.submit time (ms) and turn.start time (ms, null until the turn starts). */
  ts: number
  tsStart: number | null
  turnId: string | null
  /** True once the context block was attached to the prompt. */
  applied: boolean
  /** True once a prompt render of this turn offered a fan-out tool (without one W2 is impossible: not a unit). */
  fanout: boolean
  tokMain: number
  tokSub: number
  /** Agents attributed to this turn (ids): those seen at agent.spawn (`spawned`, waited for) and any bound by a step; `open` = spawned ones not yet completed. */
  agents: Set<string>
  spawned: Set<string>
  open: number
  /** The main turn completed. */
  done: boolean
  tsEnd: number | null
  aborted: boolean
  durationMs: number | null
}

export const ED_FILE = 'ed_arms.jsonl'
export const edDir = (backendDir: string): string => `${backendDir}/labels`
export const edPath = (backendDir: string): string => `${edDir(backendDir)}/${ED_FILE}`
const FANOUT_TOOLS: readonly string[] = ['Agent', 'Workflow']
/** Prompt origins that are the user's own turn; absent = the user's own too. */
const USER_ORIGINS: readonly string[] = ['composer', 'bridge']

/** murmur3's finaliser: FNV-1a's low bit is the parity of the input's low bits, so it is mixed before use. */
export function mix32(h: number): number {
  h ^= h >>> 16
  h = Math.imul(h, 0x85ebca6b) >>> 0
  h ^= h >>> 13
  h = Math.imul(h, 0xc2b2ae35) >>> 0
  h ^= h >>> 16
  return h >>> 0
}

/** The arm of turn `turn` of session `sessionId`: a fair coin on a mixed hash, reproducible from the row. */
export const edArmOf = (sessionId: string, turn: number): EdArm => (mix32(fnv1a(`${sessionId}:ed:${turn}`)) % 2 === 0 ? 'W0' : 'W2')

export const ED_TEXT: Record<EdArm, string> = {
  W0:
    'Workflow arm W0 (randomised experiment E-D, this turn only): do this turn\'s task yourself in this session ' +
    'and do not launch subagents (Agent, Workflow) for it, unless the user asks for subagents in this turn.',
  W2:
    'Workflow arm W2 (randomised experiment E-D, this turn only): delegate the separable parts of this turn\'s task ' +
    'to subagents (Agent tool) and integrate their results yourself. Skip delegation only when the task has no separable part.',
}

const MAX_QUEUE = 8
const MAX_CLOSING = 32

export const keyOf = (text: string): number => fnv1a(text)

/** The user's own, non-empty, not typed over a running turn (`turnId` set: delivered into it, no turn.start of its own). */
type SubmitLike = Pick<PromptSubmitInput, 'text' | 'context' | 'turnId'> & { origin?: PromptSubmitInput['origin'] }

const isUserPrompt = (e: SubmitLike): boolean =>
  e.text.trim() !== '' && e.turnId === undefined && (e.origin === undefined || USER_ORIGINS.includes(e.origin.kind))

/**
 * prompt.submit (the user's own, non-empty): assign the next turn's arm and return the context to pass
 * down (`next({ ...e, context })`); null when this prompt gets no arm. The state waits in the queue
 * until its turn.start (a prompt typed mid-turn is queued by the engine too).
 */
export function submitPrompt(rt: Runtime, e: SubmitLike, now: number): readonly string[] | null {
  if (!rt.options.ed || rt.sessionId === '' || armOf(rt.sessionId) === 'holdout' || !isUserPrompt(e)) return null
  rt.edTurns += 1
  const ed: EdState = {
    turn: rt.edTurns, arm: edArmOf(rt.sessionId, rt.edTurns), key: keyOf(e.text), ts: now, tsStart: null, turnId: null, applied: true,
    fanout: false, tokMain: 0, tokSub: 0, agents: new Set(), spawned: new Set(), open: 0, done: false, tsEnd: null, aborted: false, durationMs: null,
  }
  rt.edQueue.push(ed)
  if (rt.edQueue.length > MAX_QUEUE) rt.edQueue.splice(0, rt.edQueue.length - MAX_QUEUE)
  return [...(e.context ?? []), ED_TEXT[ed.arm]]
}

/** The prompt did not enter (a hook beneath dropped it): its arm was never read; forget it. */
export function dropPrompt(rt: Runtime, text: string): void {
  const k = keyOf(text)
  for (let i = rt.edQueue.length - 1; i >= 0; i--) {
    if (rt.edQueue[i]!.key === k) {
      rt.edQueue.splice(i, 1)
      return
    }
  }
}

/**
 * turn.start (the engine fires it for no subagent): bind the queued arm whose text this turn
 * proceeds with; older queued arms are stale (their prompts never got a turn) and are dropped. A
 * turn with no match (a continuation, a notification, a peer, a plugin's prompt) binds nothing.
 */
export function startTurn(rt: Runtime, e: Pick<TurnStartInput, 'text' | 'turnId'>, now: number): void {
  if (!rt.options.ed || e.text.trim() === '') return
  const k = keyOf(e.text)
  const i = rt.edQueue.findIndex(x => x.key === k)
  if (i < 0) return
  const ed = rt.edQueue[i]!
  rt.edQueue.splice(0, i + 1)
  ed.turnId = e.turnId
  ed.tsStart = now
  rt.ed = ed
}

/** prompt.compose: a render that offers a fan-out tool makes the open turn a unit. Adds no section. */
export function noteCompose(rt: Runtime, e: Pick<PromptComposeInput, 'tools'>): void {
  const ed = rt.ed
  if (ed === null || ed.done) return
  if ((e.tools ?? []).some(t => FANOUT_TOOLS.includes(t))) ed.fanout = true
}

/** agent.spawn result: the subagent belongs to the open turn; its steps and completion are attributed to it, however late. */
export function noteEdSpawn(rt: Runtime, agentId: string | null | undefined): void {
  const ed = rt.ed
  if (ed === null || ed.done || typeof agentId !== 'string' || agentId === '' || ed.spawned.has(agentId)) return
  ed.agents.add(agentId)
  ed.spawned.add(agentId)
  ed.open += 1
  rt.edAgents.set(agentId, ed)
}

/**
 * turn.step usage: the main agent's steps count toward the open turn; a subagent's toward the turn
 * that spawned it. An agent nobody spawned through agent.spawn (a Workflow agent, an engine fork)
 * that steps while a typed turn is open has its tokens counted for that turn but never holds the
 * row open: only agents seen at agent.spawn are waited for.
 */
export function noteEdTokens(rt: Runtime, agentId: string | undefined, tokens: number): void {
  if (agentId === undefined) {
    const ed = rt.ed
    if (ed !== null && !ed.done) ed.tokMain += tokens
    return
  }
  let ed = rt.edAgents.get(agentId)
  if (ed === undefined) {
    if (rt.ed === null || rt.ed.done || agentId === '') return
    ed = rt.ed
    ed.agents.add(agentId)
    rt.edAgents.set(agentId, ed)
  }
  ed.tokSub += tokens
}

function row(rt: Runtime, ed: EdState): string {
  return JSON.stringify({
    ev: 'ed_arm',
    ts: ed.ts / 1000,
    ts_start: ed.tsStart === null ? null : ed.tsStart / 1000,
    ts_end: ed.tsEnd === null ? null : ed.tsEnd / 1000,
    session_id: rt.sessionId,
    turn: ed.turn,
    turn_id: ed.turnId,
    arm: ed.arm,
    applied: ed.applied,
    fanout: ed.fanout,
    aborted: ed.aborted,
    duration_ms: ed.durationMs,
    tok_main: ed.tokMain,
    tok_sub: ed.tokSub,
    subs: ed.agents.size,
    subs_open: ed.open,
    inject_arm: armStamp(rt),
  })
}

async function write(host: Host, rt: Runtime, ed: EdState): Promise<void> {
  for (const id of ed.agents) if (rt.edAgents.get(id) === ed) rt.edAgents.delete(id)
  if (rt.backendDir === '') return
  try {
    const dir = edDir(rt.backendDir)
    await ensureDir(host, dir)
    await appendLines(host, edPath(rt.backendDir), [row(rt, ed)]) // a row that cannot be written is dropped (fail open; independent of the arm)
  } catch {
    // the experiment never breaks a turn
  }
}

/**
 * turn.complete: a subagent's completion closes its slot in the turn that spawned it (the row is
 * written when the main turn is done and no subagent is open); the main turn's completion marks the
 * turn done and writes the row unless a subagent (background) is still running.
 */
export async function completeEd(host: Host, rt: Runtime, e: Pick<TurnCompleteInput, 'turnId' | 'agentId' | 'isAborted' | 'durationMs'>, now: number): Promise<void> {
  if (e.agentId !== undefined && e.agentId !== null) {
    const ed = rt.edAgents.get(e.agentId)
    if (ed === undefined) return
    rt.edAgents.delete(e.agentId)
    if (!ed.spawned.has(e.agentId)) return // bound by a step, never waited for
    ed.open = Math.max(0, ed.open - 1)
    if (ed.done && ed.open === 0) {
      rt.edClosing = rt.edClosing.filter(x => x !== ed)
      await write(host, rt, ed)
    }
    return
  }
  const ed = rt.ed
  if (ed === null || ed.turnId !== e.turnId) return
  rt.ed = null
  ed.done = true
  ed.tsEnd = now
  ed.aborted = e.isAborted === true
  ed.durationMs = e.durationMs
  if (ed.open === 0) {
    await write(host, rt, ed)
    return
  }
  rt.edClosing.push(ed)
  if (rt.edClosing.length > MAX_CLOSING) await write(host, rt, rt.edClosing.shift()!)
}

/** session.end: turns still waiting on background subagents are written as they stand (`subs_open` > 0). */
export async function flushEd(host: Host, rt: Runtime): Promise<void> {
  const closing = rt.edClosing.splice(0)
  for (const ed of closing) await write(host, rt, ed)
}

/** session.end{clear|resume}: the open turn, the queue and the agent map belong to the session that ended. */
export function endEd(rt: Runtime): void {
  rt.ed = null
  rt.edQueue = []
  rt.edClosing = []
  rt.edAgents = new Map()
  rt.edTurns = 0
}
