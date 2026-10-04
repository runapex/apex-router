"""pce-core drift — regime change, not anomalies (A7): principal-angle and variance-share drift,
Page CUSUM on standardized residuals (κ=0.5, h=4), and the penalty state machine (ramp +
enter/exit streaks). plugin/hooks/core/drift.ts mirrors this file."""
from __future__ import annotations

from apex_router.core.linalg import principal_angle as subspace_angle  # noqa: F401  (re-exported)


def variance_l1(l0, l1) -> float:
    """L1 distance between the two spectra's variance shares."""
    s0 = 0.0
    for x in l0:
        s0 += x
    s1 = 0.0
    for x in l1:
        s1 += x
    if s0 <= 0 or s1 <= 0:
        return 0.0
    total = 0.0
    for a, b in zip(l0, l1):
        total += abs(a / s0 - b / s1)
    return total


def cusum_lower(s: float, z: float, kappa: float = 0.5, h: float = 4.0):
    """One-sided (downward) Page CUSUM: S ← max(0, S − z − κ); alarm when S > h."""
    nxt = max(0.0, s - z - kappa)
    return nxt, nxt > h


def penalty_ramp(T: float, t_lo: float = 2.0, t_hi: float = 6.0, p_max: int = 10) -> int:
    return int(p_max * min(1.0, max(0.0, (T - t_lo) / (t_hi - t_lo))))


def penalty_run(scores, enter_streak: int = 2, exit_streak: int = 3, warm_after: int = 3):
    """COLD until warm_after samples; WARM→DRIFTING after enter_streak scores ≥ 2.0;
    DRIFTING→WARM after exit_streak scores < 2.0. Penalty is the ramp while DRIFTING, else 0."""
    state, streak, seen, out = "COLD", 0, 0, []
    for T in scores:
        seen += 1
        if state == "COLD" and seen >= warm_after:
            state = "WARM"
        elif state == "WARM":
            streak = streak + 1 if T >= 2.0 else 0
            if streak >= enter_streak:
                state, streak = "DRIFTING", 0
        elif state == "DRIFTING":
            streak = streak + 1 if T < 2.0 else 0
            if streak >= exit_streak:
                state, streak = "WARM", 0
        pen = penalty_ramp(T) if state == "DRIFTING" else 0
        out.append((T, state, pen))
    return out
