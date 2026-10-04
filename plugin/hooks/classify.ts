// Port of apex_router.route_log.classify_dispatch (+ classify.classify_request's marker table).
// The Python is the oracle: tests/classify.test.ts replays plugin/tests/fixtures/classify.ts.
import type { TaskType } from '../types/index.d.ts'

const MARKERS: readonly (readonly [string, TaskType])[] = [
  ['debug', 'debug'],
  ['review', 'review'],
  ['refactor', 'refactor'],
  ['generate', 'generate'],
  ['explore', 'explore'],
  ['plan', 'explore'],
]

const FALLBACK: readonly (readonly [RegExp, TaskType])[] = [
  [/\b(review|audit|verif)/, 'review'],
  [/\b(implement|write|build|fix)/, 'generate'],
  [/\b(debug|root[ -]cause|why)\b/, 'debug'],
  [/\b(refactor|rename)/, 'refactor'],
]

export const PROMPT_HEAD = 200

export const promptHeadOf = (prompt: string): string => prompt.trim().slice(0, PROMPT_HEAD)

export function classifyDispatch(subagentType?: string | null, description?: string | null, promptHead?: string | null): TaskType {
  try {
    const st = typeof subagentType === 'string' ? subagentType.trim().toLowerCase() : ''
    if (st === 'explore') return 'explore'
    const text = [description, promptHead]
      .filter((x): x is string => typeof x === 'string')
      .join(' ')
      .toLowerCase()
    const words = new Set(text.match(/[a-z]+/g) ?? [])
    if (st !== '') words.add(st)
    for (const [marker, type] of MARKERS) if (words.has(marker)) return type
    for (const [pattern, type] of FALLBACK) if (pattern.test(text)) return type
  } catch {
    // never raises, like the oracle
  }
  return 'explore'
}
