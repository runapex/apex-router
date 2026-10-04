// Constants and empty views only. NO atoms here: the engine's scan requires every `atom(...)` that
// `read`/`update` use to be a const of the SAME file (verified: a shared atoms file fails to load
// with "the state library's update takes a source the scan can read ... written there or in a
// const of this file"). Each module declares the atoms it uses, with the same literal plugin/key.

import type { BackendView, InjectView, ProfileStore, ProfileView, SignalsView } from '../types/index.d.ts'

export const PLUGIN = 'datapce'
export const PANE_ID = 'datapce'

export const EMPTY_SIGNALS: SignalsView = {
  level: 'GREEN',
  burnShort: null,
  burnLong: null,
  budgetBurn: null,
  budgetBurnShort: null,
  budgetBurnLong: null,
  minutesToExhaust: null,
  breakers: {},
  anomaly: null,
  fanoutRisk: null,
  admissionRate: 10,
}

export const EMPTY_BACKEND: BackendView = {
  present: false,
  conformance: null,
  families: {},
  lanes: null,
  drainEtaS: null,
  health: null,
  verdicts: {},
  handoffTokens: null,
}

export const EMPTY_PROFILE_STORE: ProfileStore = {
  repos: [],
  taskMix: {},
  skills: {},
  hours: Array.from({ length: 24 }, () => 0),
  backend: false,
}

export const profileView = (p: ProfileStore): ProfileView => ({
  taskMix: p.taskMix,
  skills: p.skills,
  repos: p.repos.length,
  hours: p.hours,
  backend: p.backend,
})

export const EMPTY_INJECT: InjectView = { arm: 'evidence', bytes: 0, sections: 0 }
