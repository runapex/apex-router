// datapce state contract: `claude plugin validate` holds every $.state key the module names to
// it. Also the value shapes the hooks modules share. Self-contained: no imports.

export type Level = 'GREEN' | 'AMBER' | 'RED'
export type Tier = 'haiku' | 'sonnet' | 'opus' | 'fable'
export type TaskType = 'explore' | 'review' | 'debug' | 'refactor' | 'generate' | 'mechanical' | 'synthesis' | 'other'
export type CellState = 'COLD' | 'WARMING' | 'READY' | 'DRIFTING'
export type Arm = 'evidence' | 'holdout'
export type BreakerState = 'closed' | 'open' | 'half-open'

/** Welford running moments: persist parameters (n, mean, M2), never rows. */
export type Welford = { n: number; mean: number; m2: number }
/** Multivariate Welford: mean vector and co-moment matrix. */
export type WelfordCov = { n: number; mean: number[]; c: number[][] }
/** One evidence cell (task_type × pressure × tier): the §5 state machine's whole state. */
export type Cell = {
  n: number
  pass: number
  state: CellState
  winN: number
  winPass: number
  below: number
  above: number
  cusum: number
  p0: number | null
}
export type LevelState = { level: Level; up: number; down: number }
export type Breaker = { fails: number; openedAt: number | null }
export type TokenBucket = { rate: number; burst: number; tokens: number; t: number }
/** Session-anomaly model: parameters only (A9). */
export type AnomalyModel = {
  cov: WelfordCov
  vectors: number[][]
  values: number[]
  sd: number[]
  fittedAt: number
  qHist: number[]
  t2Hist: number[]
}

export type Measure = {
  ctxPercent: number | null
  limitKind: string | null
  limitPercent: number | null
  resetsAt: string | null
  costUsd: number | null
}

export type Dispatch = {
  toolUseId: string
  agentId: string | null
  description: string
  subagentType: string
  taskType: TaskType
  requested: string
  resolved: string | null
  advised: Tier | null
  advisedState: 'COLD' | 'WARMING' | 'READY' | null
  applied: boolean
  basis: string
  outcome: 'running' | 'ok' | 'failed'
  startedAt: number
  durationMs: number | null
  /** tok(all): input + output + cache reads + cache writes. */
  tokens: number | null
  level: Level
}

export type CellView = {
  key: string
  taskType: string
  level: Level
  tier: Tier
  state: CellState
  n: number
  pass: number
  wilsonLo: number
  tokMean: number | null
  /** Runs that fed tokMean, clamped to n (the stats are lifetime; the cell can restart on a rebase). */
  tokN: number
  durationMean: number | null
  /** Runs that fed durationMean, clamped to n. */
  durationN: number
  /** Failed runs whose kind was "unavailable", clamped to n − pass (the rest is "other"). */
  unavailable: number
}

export type SignalsView = {
  level: Level
  /** What the last window observed; differs from `level` while the enter/exit streak holds it. */
  observed: Level
  /** Calm windows still needed before `level` drops to `observed` (null when not settling). */
  exitIn: number | null
  burnShort: number | null
  burnLong: number | null
  budgetBurn: number | null
  budgetBurnShort: number | null
  budgetBurnLong: number | null
  minutesToExhaust: number | null
  breakers: Record<string, BreakerState>
  anomaly: { score: number; term: 'Q' | 'T2' } | null
  fanoutRisk: number | null
  admissionRate: number
}

export type LaneStats = Record<string, { n: number; ok: number; escalated: number }>

export type BackendView = {
  present: boolean
  conformance: number | null
  families: Record<string, Level>
  lanes: LaneStats | null
  drainEtaS: number | null
  health: number | null
  verdicts: Record<string, string>
  handoffTokens: number | null
}

export type ProfileStore = {
  repos: string[]
  taskMix: Record<string, number>
  skills: Record<string, number>
  hours: number[]
  backend: boolean
}

export type ProfileView = {
  taskMix: Record<string, number>
  skills: Record<string, number>
  repos: number
  hours: number[]
  backend: boolean
}

export type InjectView = { arm: Arm; bytes: number; sections: number }

declare module 'claude-code' {
  interface PluginState {
    datapce: {
      measure: Measure | null
      dispatches: Dispatch[]
      cells: CellView[]
      signals: SignalsView
      backend: BackendView
      profile: ProfileView
      inject: InjectView
      bandHidden: boolean
    }
  }
}
