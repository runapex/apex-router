"""Calibration-sensitivity sweep + ordinal-invariance gate. §8 (internal review item 1).

The cachesim's mechanics are proven exact, but the ledger corpus under-represents deep sessions
(6–7:1 read:write vs the reference proxy's real 21:1), and the tuner's re-compaction-class decisions are
calibration-sensitive: a knob that loses on shallow sessions can win on deep ones (break-even
k* ≈ 1.25/(0.1·f) turns). So a single-regime `grid_search` result is not trustworthy on its own.

This converts the calibration GAP into a robustness CERTIFICATE: sweep the corpus's turn-depth
(hence its read:write regime) across the plausible band [6:1 … 30:1] and require the tuner's
ORDINAL decision for each knob — is it in the winning vector? — to be INVARIANT across the band.
A knob whose inclusion flips is tagged `calibration_sensitive` and deferred to live measurement;
a knob stable across the whole band is `robust` and safe to trust offline.

We sweep depth (not the sim's pricing) because depth is the real-world unknown the ledger can't
observe; pricing (P_write/P_read) is a known Anthropic constant.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from apex_router.proxy_engine.tuner.replay import PipelineFn, Request, grid_search


@dataclass
class KnobVerdict:
    name: str
    decisions: dict                    # regime read:write ratio → chosen value at that regime
    robust: bool                       # same chosen value across every swept regime
    calibration_sensitive: bool        # inclusion/value flips somewhere in the band


@dataclass
class SweepResult:
    regimes: list[float]
    per_knob: dict[str, KnobVerdict] = field(default_factory=dict)
    all_robust: bool = True

    def sensitive_knobs(self) -> list[str]:
        return [k for k, v in self.per_knob.items() if v.calibration_sensitive]


# Plausible calibration band: the ledger yields ~6-7:1; the reference proxy's real profile is ~21:1; we
# sweep a bit past it to 30:1 (internal review's [6:1, 30:1]).
DEFAULT_BAND = (6.0, 10.0, 14.0, 21.0, 30.0)


def _corpus_for_regime(base_corpus_fn, ratio: float) -> list[Request]:
    """Build a corpus whose read:write regime ≈ `ratio` by scaling session DEPTH. For a linear-
    growth session, read:write ≈ (turns-1)/2, so turns ≈ 2·ratio + 1. base_corpus_fn(turns)
    returns a corpus at that depth."""
    turns = max(2, int(round(2 * ratio + 1)))
    return base_corpus_fn(turns)


def sweep(base_corpus_fn, byte_knobs: dict[str, tuple[float, float, float]],
          pipeline: PipelineFn, baseline_knobs: dict,
          band: tuple[float, ...] = DEFAULT_BAND) -> SweepResult:
    """Run grid_search at each regime in `band`; report per-knob ordinal stability.

    `base_corpus_fn(turns)` → corpus at the given session depth. Returns a SweepResult whose
    `sensitive_knobs()` must be empty for the tuner's offline decisions to be trusted.
    """
    per_regime_choice: dict[float, dict] = {}
    for ratio in band:
        corpus = _corpus_for_regime(base_corpus_fn, ratio)
        prop = grid_search(corpus, byte_knobs, pipeline, baseline_knobs)
        # only trust an admissible (zero-bust) proposal; an inadmissible one contributes no
        # decision (the knob stays at baseline there)
        chosen = prop.knob_vector if prop.admissible else baseline_knobs
        per_regime_choice[ratio] = chosen

    result = SweepResult(regimes=list(band))
    for knob in byte_knobs:
        decisions = {ratio: per_regime_choice[ratio].get(knob) for ratio in band}
        distinct = {v for v in decisions.values()}
        robust = len(distinct) == 1
        result.per_knob[knob] = KnobVerdict(
            name=knob, decisions=decisions, robust=robust,
            calibration_sensitive=not robust,
        )
        if not robust:
            result.all_robust = False
    return result
