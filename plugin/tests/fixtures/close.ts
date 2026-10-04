import { expect } from 'claude-code/testing'

/** Float parity at `tol` relative (absolute below 1). */
export function close(actual: number | null | undefined, expected: number, tol = 1e-9, label = ''): void {
  const ok = typeof actual === 'number' && Math.abs(actual - expected) <= tol * Math.max(1, Math.abs(expected))
  expect(ok, `${label} ${String(actual)} is not within ${tol} of ${expected}`).toBe(true)
}
