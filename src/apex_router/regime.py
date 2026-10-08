"""Regime model for L1 — a 2-state hidden Markov model over per-call outcomes {normal, degraded}.

Why: the burst chain (markov.py, zeno 1b) has memory of ONE call. On this machine failures also
cluster by DAY (the upstream has bad days: ~5% of calls failed on 09-30/10-01, ~0.4% on 10-03), which
neither p^n nor the chain can represent — both are one set of numbers for all time. A regime model
says the upstream is in one of two hidden states, each with its own per-call fail probability, and
the state persists:

    hidden s_t ∈ {normal, degraded},  P(s_t | s_{t-1}) = A,   P(fail_t | s_t) = f[s_t]

fitted by Baum–Welch (EM) on the time-ordered stream of ALL proxy calls (a regime is a property of
the upstream at a time, not of a session, so session-less rows inform it too). Viterbi decodes which
calls — hence which days/hours — were degraded; the stationary share of degraded time is
A[n→d] / (A[n→d] + A[d→n]). Per-state burst chains (a 4-state model) are NOT implemented.

Baseline without EM — the "day mixture": a day is degraded when its fail rate is significantly above
the pooled rate (Wilson lower bound > pooled); P(n-call session clean) = (1-w)(1-f_N)^n + w(1-f_D)^n,
w = share of calls on degraded days.

Held-out comparison (`holdout`) follows markov.holdout: sessions in first-appearance order, the first
70% fit, the rest scored on P(clean) (log-lik + Brier). iid p^n and the burst chain are fitted on the
training SESSIONS exactly as markov.holdout does. The HMM and the day mixture are fitted on the
stream rows strictly before the first test session's first call. Two HMM predictions:
  - `hmm` (stationary start): same information as iid/chain — a mixture over the regime;
  - `hmm_filtered`: the regime filtered causally on every call before the session starts (other
    sessions' outcomes included). This uses MORE information than iid/chain — it is the online
    predictor a router would actually run, and is reported as such, not as a like-for-like model.

Pure stdlib (2 states → scalar recursions; ~0.1 s per EM iteration on 90k calls). No I/O.
"""
from __future__ import annotations

import datetime as _dt
import math

from . import markov as mk
from .core.stats import paired_bootstrap_ci, wilson_ci

_EPS = 1e-12


# ---- fitting -----------------------------------------------------------------------------------

def _ftab(p) -> tuple:
    """Per-state fail rate after an ok call and after a failed call: ((fN_ok, fN_fail), (fD_ok, fD_fail)).
    A plain HMM has the two tied."""
    f, g = p["fail"], p["fail_after_fail"]
    return (f[0], g[0]), (f[1], g[1])


def _init_params(rate: float, k: int, burst: bool) -> dict:
    """Deterministic EM starting points: degraded = several multiples of the pooled rate."""
    fn = max(min(rate * 0.5, 0.2), 1e-4)
    fd = min(max(rate * (3, 10, 30)[k % 3], fn * 2), 0.6)
    sn, sd = ((0.999, 0.99), (0.9995, 0.995), (0.99, 0.95))[k % 3]
    after = [min(fn * 10, 0.5), min(fd * 3, 0.8)] if burst else [fn, fd]
    return {"pi": [0.9, 0.1], "A": [[sn, 1 - sn], [1 - sd, sd]], "fail": [fn, fd],
            "fail_after_fail": after, "burst": burst}


def _forward_backward(seq, p):
    """One sequence: (loglik, gamma sums, xi sums, emission sums, gamma at t=0). Scaled recursions.
    Emission counts are split by the previous outcome: n[s][prev] calls and k[s][prev] failures."""
    (a00, a01), (a10, a11) = p["A"]
    (f0o, f0f), (f1o, f1f) = _ftab(p)
    n = len(seq)
    al0 = [0.0] * n
    al1 = [0.0] * n
    c = [0.0] * n
    x = seq[0]
    b0 = p["pi"][0] * (f0o if x else 1.0 - f0o)
    b1 = p["pi"][1] * (f1o if x else 1.0 - f1o)
    s = b0 + b1
    al0[0], al1[0], c[0] = b0 / s, b1 / s, s
    prev = x
    for t in range(1, n):
        x = seq[t]
        r0, r1 = (f0f, f1f) if prev else (f0o, f1o)
        a0, a1 = al0[t - 1], al1[t - 1]
        b0 = (a0 * a00 + a1 * a10) * (r0 if x else 1.0 - r0)
        b1 = (a0 * a01 + a1 * a11) * (r1 if x else 1.0 - r1)
        s = b0 + b1
        al0[t], al1[t], c[t] = b0 / s, b1 / s, s
        prev = x
    loglik = sum(math.log(v) for v in c)
    be0 = be1 = 1.0
    nn = [[0.0, 0.0], [0.0, 0.0]]  # nn[s][prev]: expected calls in state s after outcome prev
    kk = [[0.0, 0.0], [0.0, 0.0]]  # kk[s][prev]: expected failures among them
    xi = [[0.0, 0.0], [0.0, 0.0]]
    for t in range(n - 1, 0, -1):
        x, pv = seq[t], seq[t - 1]
        g0, g1 = al0[t] * be0, al1[t] * be1
        nn[0][pv] += g0
        nn[1][pv] += g1
        if x:
            kk[0][pv] += g0
            kk[1][pv] += g1
        r0, r1 = (f0f, f1f) if pv else (f0o, f1o)
        w0 = be0 * (r0 if x else 1.0 - r0)
        w1 = be1 * (r1 if x else 1.0 - r1)
        p0, p1, ct = al0[t - 1], al1[t - 1], c[t]
        xi[0][0] += p0 * a00 * w0 / ct
        xi[0][1] += p0 * a01 * w1 / ct
        xi[1][0] += p1 * a10 * w0 / ct
        xi[1][1] += p1 * a11 * w1 / ct
        be0, be1 = (a00 * w0 + a01 * w1) / ct, (a10 * w0 + a11 * w1) / ct
    g0, g1 = al0[0] * be0, al1[0] * be1
    tot = g0 + g1
    g0, g1 = g0 / tot, g1 / tot
    nn[0][0] += g0                # the first call is treated as following an ok call
    nn[1][0] += g1
    if seq[0]:
        kk[0][0] += g0
        kk[1][0] += g1
    return loglik, nn, kk, xi, (g0, g1)


def _ordered(p: dict) -> dict:
    """Label the state with the lower fail rate after an ok call 'normal' (index 0)."""
    if p["fail"][0] <= p["fail"][1]:
        return p
    (a00, a01), (a10, a11) = p["A"]
    return {**p, "pi": p["pi"][::-1], "fail": p["fail"][::-1],
            "fail_after_fail": p["fail_after_fail"][::-1], "A": [[a11, a10], [a01, a00]]}


def fit_hmm(seqs, burst: bool = False, max_iter: int = 300, tol: float = 1e-6, prior: float = 0.5,
            restarts: int = 3) -> dict | None:
    """Baum–Welch on 0/1 sequences (1 = failed call), MAP with `prior` pseudo-counts per cell so no
    probability hits 0 or 1. Best of `restarts` deterministic starts by log-likelihood. None with no
    calls or no failures (nothing to separate).

    burst=False: the plain 2-state HMM, one fail rate per state. burst=True: each state carries its
    own burst chain — the fail rate depends on the state AND on whether the previous call failed
    (an autoregressive HMM), so second-scale bursts are explained by the chain and the hidden state
    is left to explain slower shifts in the base rate."""
    seqs = [list(s) for s in seqs if s]
    calls = sum(len(s) for s in seqs)
    fails = sum(sum(s) for s in seqs)
    if not calls or not fails:
        return None
    rate = fails / calls
    best = None
    for k in range(restarts):
        p = _init_params(rate, k, burst)
        prev = -math.inf
        it = 0
        converged = False
        for it in range(1, max_iter + 1):
            ll = 0.0
            N = [[0.0, 0.0], [0.0, 0.0]]
            K = [[0.0, 0.0], [0.0, 0.0]]
            XI = [[0.0, 0.0], [0.0, 0.0]]
            G0 = [0.0, 0.0]
            for s in seqs:
                l, nn, kk, xi, g_0 = _forward_backward(s, p)
                ll += l
                for i in range(2):
                    G0[i] += g_0[i]
                    for j in range(2):
                        N[i][j] += nn[i][j]
                        K[i][j] += kk[i][j]
                        XI[i][j] += xi[i][j]
            A = [[(XI[i][j] + prior) / (XI[i][0] + XI[i][1] + 2 * prior) for j in range(2)]
                 for i in range(2)]
            if burst:
                f = [(K[i][0] + prior) / (N[i][0] + 2 * prior) for i in range(2)]
                g = [(K[i][1] + prior) / (N[i][1] + 2 * prior) for i in range(2)]
            else:
                f = [(K[i][0] + K[i][1] + prior) / (N[i][0] + N[i][1] + 2 * prior) for i in range(2)]
                g = list(f)
            pi = [(G0[i] + prior) / (len(seqs) + 2 * prior) for i in range(2)]
            p = {"pi": pi, "A": A, "fail": f, "fail_after_fail": g, "burst": burst}
            if abs(ll - prev) <= tol * max(1.0, abs(ll)):
                converged = True
                prev = ll
                break
            prev = ll
        cand = {**_ordered(p), "loglik": prev, "iters": it, "converged": converged}
        if best is None or cand["loglik"] > best["loglik"]:
            best = cand
    best["n"] = calls
    best["fails"] = fails
    best["stationary_degraded"] = stationary_degraded(best["A"])
    # BIC against the matching memoryless baseline: one Bernoulli rate (plain, 5 vs 1 free
    # parameters) or one burst chain (burst, 7 vs 2). > 10 is strong evidence of hidden regimes.
    if burst:
        ch = mk.fit_chain(seqs, prior=0.0)
        c = ch["counts"]
        ll0 = 0.0
        for hit, miss in ((c["ok_fail"] + c["first_fail"], c["ok_ok"] + c["first_ok"]),
                          (c["fail_fail"], c["fail_ok"])):
            if hit and miss:
                q = hit / (hit + miss)
                ll0 += hit * math.log(q) + miss * math.log(1 - q)
        k0, k1 = 2, 7
    else:
        ll0 = fails * math.log(rate) + (calls - fails) * math.log(1 - rate) if 0 < rate < 1 else 0.0
        k0, k1 = 1, 5
    best["loglik_baseline"] = ll0
    best["delta_bic"] = 2 * (best["loglik"] - ll0) - (k1 - k0) * math.log(calls)
    return best


def stationary_degraded(A) -> float | None:
    """Long-run share of calls in the degraded state: a01 / (a01 + a10)."""
    a01, a10 = A[0][1], A[1][0]
    return a01 / (a01 + a10) if a01 + a10 > 0 else None


def expected_dwell(A) -> tuple:
    """Mean calls per visit to (normal, degraded): 1 / (1 - a_ii)."""
    return tuple(1.0 / (1.0 - A[i][i]) if A[i][i] < 1 else math.inf for i in range(2))


# ---- inference ---------------------------------------------------------------------------------

def filtered(seq, p) -> list:
    """Causal P(degraded) AFTER each observation (forward filter, normalised)."""
    (a00, a01), (a10, a11) = p["A"]
    (f0o, f0f), (f1o, f1f) = _ftab(p)
    out = []
    a0 = a1 = None
    prev = 0
    for t, x in enumerate(seq):
        r0, r1 = (f0f, f1f) if prev else (f0o, f1o)
        if t == 0:
            b0, b1 = p["pi"][0], p["pi"][1]
        else:
            b0, b1 = a0 * a00 + a1 * a10, a0 * a01 + a1 * a11
        b0 *= r0 if x else 1 - r0
        b1 *= r1 if x else 1 - r1
        s = b0 + b1
        a0, a1 = b0 / s, b1 / s
        out.append(a1)
        prev = x
    return out


def predict_next(p, post_degraded: float) -> list:
    """State distribution for the NEXT call given P(degraded) after the last observed one."""
    (a00, a01), (a10, a11) = p["A"]
    q = post_degraded
    return [(1 - q) * a00 + q * a10, (1 - q) * a01 + q * a11]


def viterbi(seq, p) -> list:
    """Most likely state path (0 = normal, 1 = degraded), log space."""
    if not seq:
        return []
    lA = [[math.log(max(p["A"][i][j], _EPS)) for j in range(2)] for i in range(2)]
    ft = _ftab(p)
    # lf[s][prev][x]
    lf = [[(math.log(max(1 - ft[s][pv], _EPS)), math.log(max(ft[s][pv], _EPS))) for pv in range(2)]
          for s in range(2)]
    lpi = [math.log(max(v, _EPS)) for v in p["pi"]]
    d0, d1 = lpi[0] + lf[0][0][seq[0]], lpi[1] + lf[1][0][seq[0]]
    back = []
    prev = seq[0]
    for x in seq[1:]:
        c00, c10 = d0 + lA[0][0], d1 + lA[1][0]
        c01, c11 = d0 + lA[0][1], d1 + lA[1][1]
        b0 = 0 if c00 >= c10 else 1
        b1 = 0 if c01 >= c11 else 1
        d0 = (c00 if b0 == 0 else c10) + lf[0][prev][x]
        d1 = (c01 if b1 == 0 else c11) + lf[1][prev][x]
        back.append((b0, b1))
        prev = x
    s = 0 if d0 >= d1 else 1
    path = [s]
    for b in reversed(back):
        s = b[s]
        path.append(s)
    return path[::-1]


def p_clean(p, n: int, start=None) -> float | None:
    """P(n consecutive clean calls) given the state distribution of the first call (`start`;
    default = the stationary distribution). Every call after the first follows an ok call, so only
    the after-ok rates enter; the first call is treated the same way (its predecessor is unknown)."""
    if n < 1:
        return None
    if start is None:
        sd = stationary_degraded(p["A"])
        start = [1 - sd, sd]
    (a00, a01), (a10, a11) = p["A"]
    k0, k1 = 1 - p["fail"][0], 1 - p["fail"][1]
    v0, v1 = start[0] * k0, start[1] * k1
    for _ in range(n - 1):
        v0, v1 = (v0 * a00 + v1 * a10) * k0, (v0 * a01 + v1 * a11) * k1
        if v0 + v1 < 1e-300:
            return 0.0
    return v0 + v1


# ---- day mixture (EM-free baseline) ------------------------------------------------------------

def day_key(ts: float) -> str:
    """Local calendar day (the machine's timezone — how the owner reads the days)."""
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def hour_key(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:00")


def fit_day_mixture(stream) -> dict | None:
    """`stream`: (ts, fail) in time order. Degraded day = Wilson lower bound on its fail rate above
    the pooled rate. None with no calls."""
    days = {}
    for ts, x in stream:
        d = days.setdefault(day_key(ts), [0, 0])
        d[0] += 1
        d[1] += x
    calls = sum(v[0] for v in days.values())
    if not calls:
        return None
    fails = sum(v[1] for v in days.values())
    pooled = fails / calls
    bad = {k for k, (n, f) in days.items() if wilson_ci(f, n)[0] > pooled}
    cn = sum(days[k][0] for k in days if k not in bad)
    fn = sum(days[k][1] for k in days if k not in bad)
    cd = sum(days[k][0] for k in bad)
    fdd = sum(days[k][1] for k in bad)
    return {"pooled": pooled, "degraded_days": sorted(bad), "days": len(days),
            "fail_normal": (fn + 0.5) / (cn + 1.0) if cn else None,
            "fail_degraded": (fdd + 0.5) / (cd + 1.0) if cd else None,
            "w": cd / calls}


def day_mixture_p_clean(m: dict, n: int) -> float | None:
    if m is None or n < 1:
        return None
    fn = m["fail_normal"] if m["fail_normal"] is not None else m["fail_degraded"]
    out = (1 - m["w"]) * (1 - fn) ** n
    if m["fail_degraded"] is not None:
        out += m["w"] * (1 - m["fail_degraded"]) ** n
    return out


# ---- held-out comparison -----------------------------------------------------------------------

def _score(preds) -> tuple:
    """(loglik, brier, per-session squared errors) for [(P(clean), clean 0/1)]."""
    ll, se = 0.0, []
    for pc, clean in preds:
        pc_c = min(max(pc, _EPS), 1 - _EPS)
        ll += math.log(pc_c if clean else 1 - pc_c)
        se.append((pc - clean) ** 2)
    return ll, (sum(se) / len(se) if se else None), se


def holdout(stream, sessions, train: float = 0.7, prior: float = 0.5, n_boot: int = 2000) -> dict:
    """`stream`: [(ts, fail)] for every call, time order. `sessions`: [(first_ts, seq)] in first-
    appearance order. See the module docstring for the protocol."""
    sessions = [(t, list(s)) for t, s in sessions if s]
    k = int(round(train * len(sessions)))
    tr, te = sessions[:k], sessions[k:]
    out = {"n_train": len(tr), "n_test": len(te), "cut_ts": None, "train_calls": 0, "models": {},
           "brier_diff_vs_iid": {}, "brier_diff_vs_markov": {}}
    if not tr or not te:
        return out
    cut = te[0][0]
    out["cut_ts"] = cut
    pre = [x for ts, x in stream if ts < cut]
    out["train_calls"] = len(pre)
    tr_seqs = [s for _, s in tr]
    p_iid = mk.iid_p(tr_seqs, prior)
    ch = mk.fit_chain(tr_seqs, prior)
    dm = fit_day_mixture([(ts, x) for ts, x in stream if ts < cut])
    xs, tss = [x for _, x in stream], [ts for ts, _ in stream]

    def _starts(hmm):
        # causal regime estimate just before each test session's first call: filter the whole
        # stream with the TRAINING parameters (no refit), read the posterior before that ts.
        post = filtered(xs, hmm)
        out_s, j, last = {}, 0, None
        for t0, _ in sorted(te, key=lambda z: z[0]):
            while j < len(tss) and tss[j] < t0:
                last = post[j]
                j += 1
            out_s[t0] = (predict_next(hmm, last) if last is not None
                         else [1 - hmm["stationary_degraded"], hmm["stationary_degraded"]])
        return out_s

    predictors = {
        "iid": lambda t0, n: None if p_iid is None else p_iid ** n,
        "markov": lambda t0, n: mk.p_clean(ch, n),
        "day_mixture": lambda t0, n: day_mixture_p_clean(dm, n),
    }
    fits = {}
    for name, burst in (("hmm", False), ("hmm_burst", True)):
        hmm = fit_hmm([pre], burst=burst) if pre else None
        fits[name] = hmm
        if hmm is None:
            predictors[name] = predictors[name + "_filtered"] = lambda t0, n: None
            continue
        st = _starts(hmm)
        predictors[name] = lambda t0, n, h=hmm: p_clean(h, n)
        predictors[name + "_filtered"] = lambda t0, n, h=hmm, st=st: p_clean(h, n, st[t0])
    errs = {}
    for name, f in predictors.items():
        preds = [(f(t0, len(s)), 0 if any(s) else 1) for t0, s in te]
        if any(pc is None for pc, _ in preds):
            out["models"][name] = None
            continue
        ll, br, se = _score(preds)
        out["models"][name] = {"loglik": ll, "brier": br}
        errs[name] = se
    # paired per-session Brier differences, session bootstrap: > 0 means the model beats the base
    for base in ("iid", "markov"):
        key = f"brier_diff_vs_{base}"
        out[key] = {}
        if base not in errs:
            continue
        for name, se in errs.items():
            if name in ("iid", base):
                continue
            deltas = [a - b for a, b in zip(errs[base], se)]
            lo, hi = paired_bootstrap_ci(deltas, n_boot=n_boot, seed=0)
            out[key][name] = {"diff": sum(deltas) / len(deltas), "ci": (lo, hi)}
    out["hmm_params"] = fits
    out["day_mixture"] = dm
    return out


# ---- decode ------------------------------------------------------------------------------------

def decode(stream, p, max_episodes: int = 8) -> dict:
    """Viterbi path aggregated to days, hours and contiguous degraded episodes."""
    xs = [x for _, x in stream]
    path = viterbi(xs, p)
    days, hours = {}, {}
    for (ts, x), s in zip(stream, path):
        for key, d in ((day_key(ts), days), (hour_key(ts), hours)):
            v = d.setdefault(key, [0, 0, 0, 0])  # calls, fails, degraded calls, fails in degraded
            v[0] += 1
            v[1] += x
            v[2] += s
            v[3] += x if s else 0
    episodes, cur = [], None
    for (ts, x), s in zip(stream, path):
        if s:
            if cur is None:
                cur = {"start": ts, "end": ts, "calls": 0, "fails": 0}
            cur["end"] = ts
            cur["calls"] += 1
            cur["fails"] += x
        elif cur is not None:
            episodes.append(cur)
            cur = None
    if cur is not None:
        episodes.append(cur)
    deg_calls = sum(path)
    deg_fails = sum(x for x, s in zip(xs, path) if s)
    n = len(xs)
    nrm_calls, nrm_fails = n - deg_calls, sum(xs) - deg_fails
    return {
        "calls": n, "degraded_calls": deg_calls, "degraded_share": deg_calls / n if n else None,
        "fail_degraded": (deg_fails / deg_calls) if deg_calls else None,
        "fail_degraded_ci": wilson_ci(deg_fails, deg_calls) if deg_calls else (None, None),
        "fail_normal": (nrm_fails / nrm_calls) if nrm_calls else None,
        "fail_normal_ci": wilson_ci(nrm_fails, nrm_calls) if nrm_calls else (None, None),
        "days": [{"day": k, "calls": v[0], "fails": v[1], "degraded_share": v[2] / v[0]}
                 for k, v in sorted(days.items())],
        "degraded_days": sorted(k for k, v in days.items() if v[2] / v[0] >= 0.5),
        "degraded_hours": sorted(k for k, v in hours.items() if v[2] / v[0] >= 0.5),
        "episodes": sorted(episodes, key=lambda e: -e["calls"])[:max_episodes],
        "n_episodes": len(episodes),
    }


def _verdict(fit, bic_threshold: float, min_dwell: float) -> str:
    if fit is None:
        return "insufficient"
    if not (fit["delta_bic"] > bic_threshold and fit["fail"][1] > fit["fail"][0]):
        return "no regime structure"
    if expected_dwell(fit["A"])[1] < min_dwell:
        return "bursts only"  # the degraded state lasts a few calls: a burst, not a regime
    return "regimes found"


def regime_report(stream, sessions, min_pairs: int = 200, bic_threshold: float = 10.0,
                  min_dwell: float = 30.0) -> dict:
    """Full-data fits (plain HMM and HMM with per-state burst chains) + Viterbi decode + held-out
    comparison. Verdict per model: "insufficient" (< min_pairs consecutive call pairs, or no
    failures); "no regime structure" (ΔBIC vs the matching memoryless model <= bic_threshold, or the
    degraded state is not worse); "bursts only" (a better fit, but the degraded state lasts fewer
    than `min_dwell` calls on average — that is the burst chain again); else "regimes found". The
    headline verdict and the day decode come from the burst variant: on bursty data the plain HMM
    spends its second state on the bursts themselves."""
    stream = [(float(ts), 1 if x else 0) for ts, x in stream]
    pairs = max(len(stream) - 1, 0)
    xs = [x for _, x in stream]
    models = {}
    for name, burst in (("hmm", False), ("hmm_burst", True)):
        fit = fit_hmm([xs], burst=burst) if pairs >= min_pairs else None
        models[name] = {"fit": fit, "verdict": _verdict(fit, bic_threshold, min_dwell),
                        "dwell": expected_dwell(fit["A"]) if fit else None,
                        "decode": decode(stream, fit) if fit else None}
    head = models["hmm_burst"]
    return {"calls": len(stream), "pairs": pairs, "min_pairs": min_pairs, "models": models,
            "verdict": head["verdict"],
            "holdout": holdout(stream, sessions) if head["fit"] else
            {"n_train": 0, "n_test": 0, "models": {}, "brier_diff_vs_iid": {},
             "brier_diff_vs_markov": {}},
            "day_mixture": fit_day_mixture(stream)}
