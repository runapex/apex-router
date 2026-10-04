import type { Tier } from '../types/index.d.ts'

/** Frontier tier order (route_log.TIER_RANK): a strictly higher rank is an escalation. */
export const TIERS: readonly Tier[] = ['haiku', 'sonnet', 'opus', 'fable']

export function tierOf(name: string | null | undefined): Tier | null {
  if (typeof name !== 'string') return null
  const low = name.toLowerCase()
  for (const t of TIERS) if (low.includes(t)) return t
  return null
}

export const rankOf = (t: Tier): number => TIERS.indexOf(t)

export const tiersBelow = (t: Tier): Tier[] => TIERS.slice(0, rankOf(t))
