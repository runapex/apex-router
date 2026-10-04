import { describe, expect, test } from 'claude-code/testing'

import type { Cell, Level, Tier } from '../types/index.d.ts'
import { welfordNew, welfordPush } from '../hooks/core/stats.ts'
import {
  adviceFor, asCells, asStats, cellKey, cellViews, evidenceRows, parseKey, recordOutcome, type Stats,
} from '../hooks/evidence.ts'
import { tierOf, tiersBelow } from '../hooks/tiers.ts'

function feed(cells: Record<string, Cell>, task: string, level: Level, tier: Tier, passes: number, fails = 0): string {
  const key = cellKey(task, level, tier)
  for (let i = 0; i < passes; i++) recordOutcome(cells, key, true)
  for (let i = 0; i < fails; i++) recordOutcome(cells, key, false)
  return key
}

function tokens(stats: Stats, key: string, mean: number): void {
  stats[key] = { tokens: welfordPush(welfordNew(), mean) }
}

describe('tiers', () => {
  test('tierOf mirrors route_log.tier_of', () => {
    expect(tierOf('claude-sonnet-5-5')).toBe('sonnet')
    expect(tierOf('OPUS')).toBe('opus')
    expect(tierOf('gpt-5')).toBeNull()
    expect(tierOf(undefined)).toBeNull()
    expect(tiersBelow('opus')).toEqual(['haiku', 'sonnet'])
    expect(tiersBelow('haiku')).toEqual([])
  })
})

describe('evidence', () => {
  test('keys round-trip; malformed keys are rejected', () => {
    expect(parseKey(cellKey('explore', 'AMBER', 'sonnet'))).toEqual({ taskType: 'explore', level: 'AMBER', tier: 'sonnet' })
    expect(parseKey('explore|PURPLE|sonnet')).toBeNull()
    expect(parseKey('explore|GREEN')).toBeNull()
  })

  test('no evidence, no advice', () => {
    expect(adviceFor({}, {}, 'explore', 'GREEN', 'opus')).toBeNull()
    expect(adviceFor({}, {}, 'explore', 'GREEN', null)).toBeNull()
  })

  test('an own READY cell advises its tier with a one-clause basis', () => {
    const cells: Record<string, Cell> = {}
    const stats: Stats = {}
    const key = feed(cells, 'explore', 'GREEN', 'sonnet', 35)
    tokens(stats, key, 41000)
    expect(adviceFor(cells, stats, 'explore', 'GREEN', 'opus')).toEqual({
      tier: 'sonnet', effort: null, confidence: 'READY', own: true, basis: 'opus→sonnet: 35/35 pass, 41k tok; GREEN now',
    })
  })

  test('the cheapest READY tier wins', () => {
    const cells: Record<string, Cell> = {}
    feed(cells, 'explore', 'GREEN', 'sonnet', 35)
    feed(cells, 'explore', 'GREEN', 'haiku', 35)
    expect(adviceFor(cells, {}, 'explore', 'GREEN', 'opus')?.tier).toBe('haiku')
  })

  test('a COLD cell inherits from the same task type at other levels — advice only', () => {
    const cells: Record<string, Cell> = {}
    feed(cells, 'explore', 'AMBER', 'sonnet', 35)
    const a = adviceFor(cells, {}, 'explore', 'GREEN', 'opus')
    expect(a?.tier).toBe('sonnet')
    expect(a?.confidence).toBe('WARMING')
    expect(a?.own).toBe(false)
    expect(a?.basis).toBe('opus→sonnet: inherited from explore all levels, 35/35 pass')
  })

  test('P5: no "all tasks" inheritance — another task type never lends its evidence', () => {
    const cells: Record<string, Cell> = {}
    feed(cells, 'review', 'GREEN', 'sonnet', 35)
    feed(cells, 'debug', 'AMBER', 'sonnet', 35)
    expect(adviceFor(cells, {}, 'explore', 'GREEN', 'opus')).toBeNull()
  })

  test('P5: DRIFTING cells are excluded from the inheritance aggregate', () => {
    const cells: Record<string, Cell> = {}
    const amber = feed(cells, 'explore', 'AMBER', 'sonnet', 40)
    for (let i = 0; i < 20; i++) recordOutcome(cells, amber, i % 10 < 8)
    expect(cells[amber]?.state).toBe('DRIFTING')
    expect(adviceFor(cells, {}, 'explore', 'GREEN', 'opus')).toBeNull()
    feed(cells, 'explore', 'RED', 'sonnet', 35)
    expect(adviceFor(cells, {}, 'explore', 'GREEN', 'opus')?.basis).toBe('opus→sonnet: inherited from explore all levels, 35/35 pass')
  })

  test('a DRIFTING own cell is neither advised nor allowed to inherit', () => {
    const cells: Record<string, Cell> = {}
    feed(cells, 'review', 'AMBER', 'sonnet', 35)
    const key = feed(cells, 'review', 'GREEN', 'sonnet', 40)
    for (let i = 0; i < 20; i++) recordOutcome(cells, key, i % 10 < 8)
    expect(cells[key]?.state).toBe('DRIFTING')
    expect(adviceFor(cells, {}, 'review', 'GREEN', 'opus')).toBeNull()
  })

  test('AMBER sheds explore one tier down as COLD advice; GREEN and review do not shed', () => {
    expect(adviceFor({}, {}, 'explore', 'AMBER', 'opus')).toEqual({
      tier: 'sonnet', effort: null, confidence: 'COLD', own: false, basis: 'AMBER shed: opus→sonnet',
    })
    expect(adviceFor({}, {}, 'explore', 'GREEN', 'opus')).toBeNull()
    expect(adviceFor({}, {}, 'review', 'AMBER', 'opus')).toBeNull()
    expect(adviceFor({}, {}, 'explore', 'RED', 'haiku')).toBeNull()
  })

  test('evidence rows: COLD omitted, cheapest READY chosen, WARMING shown with n', () => {
    const cells: Record<string, Cell> = {}
    const stats: Stats = {}
    tokens(stats, feed(cells, 'explore', 'GREEN', 'sonnet', 35), 41000)
    feed(cells, 'review', 'GREEN', 'opus', 12)
    feed(cells, 'debug', 'AMBER', 'sonnet', 5)
    expect(evidenceRows(cells, stats, 'GREEN')).toEqual([
      { taskType: 'explore', tier: 'sonnet', n: 35, passPct: 100, tokMean: 41000, state: 'READY' },
      { taskType: 'review', tier: 'opus', n: 12, passPct: 100, tokMean: null, state: 'WARMING' },
    ])
  })

  test('cell views are sorted and carry the Wilson lower bound', () => {
    const cells: Record<string, Cell> = {}
    feed(cells, 'review', 'GREEN', 'opus', 3)
    feed(cells, 'explore', 'GREEN', 'sonnet', 35)
    const views = cellViews(cells, {})
    expect(views.map(v => v.key)).toEqual(['explore|GREEN|sonnet', 'review|GREEN|opus'])
    expect(views[0]!.wilsonLo).toBeGreaterThan(0.9)
  })

  test('malformed persisted cells and stats are dropped entry by entry', () => {
    const good = asCells({ 'explore|GREEN|sonnet': { n: 3, pass: 3, state: 'WARMING', winN: 3, winPass: 3, below: 0, above: 0, cusum: 0, p0: null } })
    expect(Object.keys(good)).toEqual(['explore|GREEN|sonnet'])
    expect(asCells({ 'bad key': { n: 1 }, 'review|GREEN|opus': { n: 2, pass: 5, state: 'WARMING' } })).toEqual({})
    expect(asCells('nonsense')).toEqual({})
    expect(asStats({ k: { tokens: { n: 2, mean: 10, m2: 1 }, broken: { n: 'x' } } })).toEqual({ k: { tokens: { n: 2, mean: 10, m2: 1 } } })
    expect(asStats(null)).toEqual({})
  })
})
