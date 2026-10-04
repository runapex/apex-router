import { describe, expect, test } from 'claude-code/testing'

import { classifyDispatch, promptHeadOf } from '../hooks/classify.ts'
import { CLASSIFY } from './fixtures/classify.ts'

describe('classify ≡ route_log.classify_dispatch', () => {
  test('every oracle case', () => {
    for (const [st, desc, head, want] of CLASSIFY) {
      expect(classifyDispatch(st, desc, head), `${String(st)} | ${String(desc)} | ${String(head)}`).toBe(want as string)
    }
  })

  test('prompt head is trimmed and cut at 200', () => {
    expect(promptHeadOf(`  ${'a'.repeat(300)}  `)).toBe('a'.repeat(200))
  })
})
