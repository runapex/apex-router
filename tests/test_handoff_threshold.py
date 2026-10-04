"""The nudge threshold must not recede: p80 of cumulative reads rises with every long session,
so the nudge fired later and later. Use the median, clamped to [FLOOR, CAP]."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import handoff_threshold as ht  # noqa: E402


def test_cap_is_100m():
    assert ht.CAP == 100_000_000
    assert ht.FLOOR == 25_000_000


def test_no_sessions_is_floor():
    t, basis, *_ = ht.compute_threshold({}, 14)
    assert t == ht.FLOOR and basis == "insufficient-data"


def test_below_min_sessions_is_floor():
    t, basis, *_ = ht.compute_threshold({f"s{i}": 10**9 for i in range(ht.MIN_SESSIONS - 1)}, 14)
    assert t == ht.FLOOR and basis == "insufficient-data"


def test_exactly_min_sessions_uses_clamped_median():
    totals = {f"s{i}": 40_000_000 for i in range(ht.MIN_SESSIONS)}
    t, basis, *_ = ht.compute_threshold(totals, 14)
    assert t == 40_000_000
    assert basis == f"median of {ht.MIN_SESSIONS} sessions over 14d (clamped {ht.FLOOR}-{ht.CAP})"


def test_uses_median_not_p80():
    totals = {"a": 1_000, "b": 2_000, "c": 40_000_000, "d": 600_000_000, "e": 700_000_000}
    t, basis, p50, p80, mx, n = ht.compute_threshold(totals, 14)
    assert n == 5 and p50 == 40_000_000 and p80 == 600_000_000
    assert t == 40_000_000
    assert basis.startswith("median of 5 sessions")


def test_median_is_clamped_both_ways():
    low = {f"s{i}": 10 for i in range(ht.MIN_SESSIONS)}
    high = {f"s{i}": 10**10 for i in range(ht.MIN_SESSIONS)}
    assert ht.compute_threshold(low, 14)[0] == ht.FLOOR
    assert ht.compute_threshold(high, 14)[0] == ht.CAP


def test_threshold_never_zero():
    zeros = {f"s{i}": 0 for i in range(ht.MIN_SESSIONS)}
    assert ht.compute_threshold(zeros, 14)[0] == ht.FLOOR
    assert ht.compute_threshold({}, 14)[0] > 0
