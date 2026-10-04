import type { AgentSpawnInput, RenderInput, SessionMeasureInput, SessionStartInput, TurnCompleteInput, TurnStepInput } from 'claude-code'

export const SESSION: SessionStartInput = { cwd: '/work', surface: 'terminal', isInteractive: true }

export const MEASURE: SessionMeasureInput = {
  context: { window: 200000, tokens: 122000, percent: 61 },
  rateLimits: [
    { kind: 'seven_day', percentUsed: 20 },
    { kind: 'five_hour', percentUsed: 62, resetsAt: '2026-10-03T20:00:00Z' },
  ],
  cost: { usd: 4.12 },
  changed: ['context', 'rateLimits', 'cost'],
}

export const BAND = {
  component: 'AbovePrompt',
  requestId: 'band',
  viewport: { columns: 160, rows: 48 },
  props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 140, scroll: { offset: 0, bodyRows: 10 }, view: {} },
} as const satisfies Omit<RenderInput<'AbovePrompt'>, 'surface'>

export const PANE = {
  component: 'Pane',
  requestId: 'datapce',
  viewport: { columns: 180, rows: 48, isFullscreen: true },
  props: { title: 'datapce', isFocused: false, bodyColumns: 72, placement: 'dock', scroll: { offset: 0, bodyRows: 44 }, view: {} },
} as const satisfies Omit<RenderInput<'Pane'>, 'surface'>

export const command = (name: string, args = '') => ({
  command: name,
  args,
  origin: { kind: 'composer' as const },
  presentation: { isFullscreen: true, columns: 180 },
})

export const spawnInput = (over: Partial<AgentSpawnInput> = {}): AgentSpawnInput => ({
  tool_use_id: 'toolu_1',
  prompt: 'Find where the pressure gate reads its state file',
  description: 'Find pressure gate state file',
  subagentType: 'Explore',
  provider: { plugin: 'engine', tier: 'core' } as AgentSpawnInput['provider'],
  parentModel: 'claude-opus-5-5',
  background: false,
  fork: false,
  ...over,
})

export const stepInput = (over: Partial<TurnStepInput> = {}): TurnStepInput => ({
  turnId: 'turn-1',
  index: 0,
  model: 'claude-opus-5-5',
  messageCount: 3,
  ...over,
})

export const complete = (over: Partial<TurnCompleteInput> = {}): TurnCompleteInput =>
  ({
    answer: 'done',
    durationMs: 41000,
    isAborted: false,
    turnId: 'sub-turn-1',
    agentId: 'agent-1',
    reason: 'answer',
    usage: { input_tokens: 30000, output_tokens: 11000, cache_read_input_tokens: 0, cache_creation_input_tokens: 0, model: 'claude-sonnet-5-5' },
    ...over,
  }) as TurnCompleteInput
