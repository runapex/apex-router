"""Cell state machine at its boundaries (n = 29/30/31, Wilson, streaks, CUSUM, rebaseline) and the
cost/budget formulas against hand-computed values."""
import math

from apex_router.core import cost, routing


def run(seq, cell=None, **kw):
    c = cell if cell is not None else routing.cell_new()
    states = []
    for x in seq:
        c = routing.cell_observe(c, bool(x), **kw)
        states.append(c["state"])
    return c, states


def ready_cell():
    c, states = run([1] * 40)
    assert states[34] == "READY" and c["p0"] == 1.0
    return c


def test_new_cell_is_cold():
    assert routing.cell_new()["state"] == "COLD"


def test_all_pass_needs_35_at_target_0_9():
    _, states = run([1] * 35)
    assert set(states[:34]) == {"WARMING"}          # 30/30 → Wilson-lo 0.886 < 0.9
    assert states[34] == "READY"                     # 35/(35+1.96²) = 0.9011


def test_floor_29_30_31_at_target_0_85():
    _, states = run([1] * 31, target=0.85)
    assert states[28] == "WARMING" and states[29] == "READY" and states[30] == "READY"


def test_one_failure_is_an_anomaly_not_a_regime():
    c, states = run([1] * 9 + [0], cell=ready_cell())
    assert states[-1] == "READY"


def test_two_bad_windows_demote_then_three_good_restore():
    c, states = run(([1] * 8 + [0] * 2) * 2, cell=ready_cell())
    assert states[-1] == "DRIFTING"
    c, states = run([1] * 30, cell=c)
    assert states[-1] == "READY" and c["cusum"] == 0.0


def test_cusum_demotes_a_small_sustained_regression():
    c, states = run(([1] * 9 + [0]) * 3, cell=ready_cell())
    assert states[-1] == "READY"
    c, states = run([1] * 9 + [0], cell=c)
    assert states[-1] == "DRIFTING" and c["below"] == 0


def window(good):
    return [1] * good + [0] * (10 - good)


def drifting_cell():
    c, states = run(window(8) * 2, cell=ready_cell())
    assert states[-1] == "DRIFTING"
    return c


def test_exit_needs_exactly_three_good_windows():
    c = drifting_cell()
    for _ in range(2):
        c, states = run(window(10), cell=c)
        assert states[-1] == "DRIFTING"
    c, states = run(window(10), cell=c)
    assert states[-1] == "READY"


def test_enter_needs_exactly_two_bad_windows_without_cusum():
    # target 0.95: 9/10 windows are "below" yet the CUSUM (p0 capped 0.98) stays under h after 2 windows,
    # so only the below-streak can demote.
    c, _ = run([1] * 80, target=0.95)
    assert c["state"] == "READY"
    c, states = run(window(9), cell=c, target=0.95)
    assert states[-1] == "READY" and c["below"] == 1
    c, states = run(window(10), cell=c, target=0.95)          # a good window resets the streak
    assert states[-1] == "READY" and c["below"] == 0
    c, _ = run(window(9), cell=c, target=0.95)
    c, states = run(window(9), cell=c, target=0.95)
    assert c["cusum"] < 4.0
    assert states[-1] == "DRIFTING"


def test_drifting_windows_reset_the_opposite_streak():
    c = drifting_cell()
    c, _ = run(window(10) * 2, cell=c)
    assert c["above"] == 2
    c, states = run(window(8), cell=c)                         # bad window resets above
    assert states[-1] == "DRIFTING" and c["above"] == 0 and c["below"] == 1
    c, states = run(window(10) * 2, cell=c)
    assert states[-1] == "DRIFTING" and c["below"] == 0        # good window resets below
    c, states = run(window(10), cell=c)
    assert states[-1] == "READY"
    c = drifting_cell()
    c, _ = run(window(8) * 2 + window(10) + window(8) * 2, cell=c)
    assert c["state"] == "DRIFTING" and c["below"] == 2        # not rebaselined: streak was broken
    c, _ = run(window(8), cell=c)
    assert c["state"] == "COLD"


def test_stable_new_regime_rebaselines():
    c, _ = run([1] * 5 + [0] * 5, cell=ready_cell())
    assert c["state"] == "DRIFTING"
    c, states = run(([1] * 5 + [0] * 5) * 3, cell=c)
    assert states[-1] == "COLD" and c["n"] == 0 and c["p0"] is None


def test_reexports_reuse_the_existing_gate():
    from apex_router import gate
    assert routing.run_gate is gate.run_gate
    assert math.isclose(routing.break_even(5.0), 0.8)


def test_fit_cost_exact_line():
    xs = [1000, 2000, 3000, 4000, 5000, 6000]
    ys = [0.02 + 0.00003 * x for x in xs]
    fit = cost.fit_cost(xs, ys)
    assert abs(fit["a"] - 0.02) < 1e-6 and abs(fit["b"] - 0.00003) < 1e-9
    assert fit["r2"] > 0.999999 and abs(fit["trend"]) < 1e-7 and fit["n"] == 6  # ridge 1e-6 bias
    assert cost.fit_cost([1, 2], [1, 2]) is None


def test_budget_burn():
    b = cost.budget_burn(5.0, 360.0, 10.0)
    assert math.isclose(b["burn"], 2.0) and math.isclose(b["minutes_to_exhaust"], 360.0)
    assert cost.budget_burn(5.0, 360.0, 0.0) is None
    assert cost.budget_burn(0.0, 10.0, 10.0) == {"burn": 0.0, "minutes_to_exhaust": None}
