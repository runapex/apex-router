"""G1 baselines (DESIGN-worldmodel-P6.md §3): unigram prior, Markov order 1/2(/3), logistic regression.

Every model has the same three calls:

    fit(ds, train_ids, val_ids=None)   # val only picks a hyper-parameter; test is never read
    predict_proba(history, task)       # P(next action) over protocol.ACTIONS given steps so far
    outcome_proba(steps, task)         # P(success) from the task's steps (protocol scores the
                                       # whole task)

`history` is the list of the task's steps so far (empty → the START context); `task_logprobs`
scores a whole task in one pass (the protocol uses it when present).

Modelling choices (and why):
- **Dirichlet α = 0.5**, with the pseudo-count mass α·V centred on the next-lower order (order 2 on
  order 1, order 1 on the unigram, the unigram on uniform) instead of on uniform. Same total prior
  mass as add-½, but an unseen order-2 context falls back to its order-1 row, not to a flat 1/13.
  `backoff=False` gives plain add-α.
- **Hierarchy**: per-task-type row = (c_type + β·p_pooled)/(n_type + β). β is picked on val from
  {1, 4, 16, 64} (default 8 with no val). A task without a type, or a type unseen in train, uses
  the pooled chain. With no types at all the model is the pooled chain.
- **BIC** on the maximum-likelihood counts (no smoothing), every order scoring the same events
  (sequences padded with START); free parameters = observed contexts × (observed classes − 1).
- **Logistic regression**: L2 on weights (not biases), features standardised on train, fitted by
  L-BFGS from zeros (deterministic); λ picked on val from {1e-3, 1e-2, 1e-1, 1}.
- **Outcome**: the prior and LR use train labels (success = 1; partial/fail = 0; unknown skipped,
  gold and weak alike — scoring separates them). The LR outcome model is fitted on every prefix
  of every labeled task (weight 1/(L+1) per prefix, so a task counts once): P(success | state so
  far), readable at any step, like the JEPA value head; its λ is picked on val at the protocol's
  outcome prefix. The Markov baseline reads P(SUCCESS) off the absorbing chain (chains.py) from
  the last action seen (no step seen → the train base rate).
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

from . import chains
from .protocol import A_INDEX, ACTIONS, EPS, PHASES, V, act_id, label, task_type

START = V  # context symbol for "before the first step"


def _base_rate(ds, ids) -> float:
    ys = [label(ds.tasks[t]) for t in ids]
    ys = [y for y in ys if y is not None]
    return (sum(ys) + 0.5) / (len(ys) + 1.0)


def _val_ce(model, ds, val_ids) -> float:
    tot, n = 0.0, 0
    for tid in val_ids:
        lp = model.task_logprobs(ds.steps[tid], ds.tasks[tid])
        tot -= float(np.sum(lp))
        n += len(lp)
    return tot / n if n else math.inf


# ---- unigram -----------------------------------------------------------------------------------

class Unigram:
    name = "prior (unigram)"

    def __init__(self, alpha: float = 0.5):
        self.alpha = alpha

    def fit(self, ds, train_ids, val_ids=None):
        c = np.zeros(V)
        for tid in train_ids:
            for s in ds.steps[tid]:
                c[act_id(s)] += 1
        self.p = (c + self.alpha) / (c.sum() + self.alpha * V)
        self.base = _base_rate(ds, train_ids)
        return self

    def predict_proba(self, history, task=None):
        return self.p

    def task_logprobs(self, steps, task=None):
        return np.log(self.p[[act_id(s) for s in steps]])

    def outcome_proba(self, steps, task=None):
        return self.base


# ---- Markov order k ----------------------------------------------------------------------------

def _contexts(acts, k):
    """(context, next) pairs for one task, padded with START."""
    pad = [START] * k + list(acts)
    return [(tuple(pad[i:i + k]), pad[i + k]) for i in range(len(acts))]


class Markov:
    BETAS = (1.0, 4.0, 16.0, 64.0)

    def __init__(self, order: int = 1, alpha: float = 0.5, beta=None, backoff: bool = True,
                 by_type: bool = True, err_states: bool = False):
        if order < 1:
            raise ValueError("order must be >= 1")
        self.order, self.alpha, self.beta, self.backoff, self.by_type = (
            order, alpha, beta, backoff, by_type)
        self.name = f"markov order {order}"
        self.err_states = bool(err_states)  # A2-E: the outcome chain has <class>!err states

    def fit(self, ds, train_ids, val_ids=None):
        k = self.order
        self.counts = [defaultdict(lambda: np.zeros(V)) for _ in range(k + 1)]  # by order 0..k
        self.tcounts = defaultdict(lambda: defaultdict(lambda: np.zeros(V)))     # type -> ctx -> c
        for tid in train_ids:
            acts = [act_id(s) for s in ds.steps[tid]]
            ty = task_type(ds.tasks[tid]) if self.by_type else None
            for j in range(k + 1):
                for ctx, a in _contexts(acts, j):
                    self.counts[j][ctx][a] += 1
            if ty is not None:
                for ctx, a in _contexts(acts, k):
                    self.tcounts[ty][ctx][a] += 1
        self._cache = {}
        self.chain = chains.fit_tasks(ds, train_ids, err_states=self.err_states)
        self.base = _base_rate(ds, train_ids)
        if self.beta is None:
            self.beta = 8.0
            if val_ids and self.tcounts:
                best = None
                for b in self.BETAS:
                    self.beta, self._cache = b, {}
                    ce = _val_ce(self, ds, val_ids)
                    if best is None or ce < best[0]:
                        best = (ce, b)
                self.beta, self._cache = best[1], {}
        return self

    def _pooled(self, ctx):
        key = (None, ctx)
        if key in self._cache:
            return self._cache[key]
        j = len(ctx)
        mass = self.alpha * V
        if j == 0:
            c = self.counts[0].get((), np.zeros(V))
            p = (c + self.alpha) / (c.sum() + mass)
        else:
            c = self.counts[j].get(ctx)
            prior = self._pooled(ctx[1:]) if self.backoff else np.full(V, 1.0 / V)
            p = prior if c is None else (c + mass * prior) / (c.sum() + mass)
        self._cache[key] = p
        return p

    def _dist(self, ctx, ty):
        pool = self._pooled(ctx)
        if ty is None or ty not in self.tcounts:
            return pool
        key = (ty, ctx)
        if key not in self._cache:
            c = self.tcounts[ty].get(ctx)
            self._cache[key] = pool if c is None else (c + self.beta * pool) / (c.sum() + self.beta)
        return self._cache[key]

    def predict_proba(self, history, task=None):
        acts = [act_id(s) for s in history]
        ctx = tuple(([START] * self.order + acts)[-self.order:])
        return self._dist(ctx, task_type(task) if (task and self.by_type) else None)

    def task_logprobs(self, steps, task=None):
        ty = task_type(task) if (task and self.by_type) else None
        acts = [act_id(s) for s in steps]
        return np.array([math.log(max(float(self._dist(ctx, ty)[a]), EPS))
                         for ctx, a in _contexts(acts, self.order)])

    def outcome_proba(self, steps, task=None):
        if not steps:
            return self.base
        p = chains.p_success_from(self.chain, chains.state_of(steps[-1], self.chain.err_states)
                                  if self.chain is not None else None)
        return self.base if p is None else p


def bic_order(ds, ids, orders=(1, 2, 3)) -> dict:
    """Markov order test: ML log-likelihood, free parameters, BIC per order, and the winner."""
    seqs = [[act_id(s) for s in ds.steps[t]] for t in ids]
    n = sum(len(s) for s in seqs)
    v_obs = len({a for s in seqs for a in s})
    rows = []
    for k in orders:
        c = defaultdict(lambda: np.zeros(V))
        for s in seqs:
            for ctx, a in _contexts(s, k):
                c[ctx][a] += 1
        ll = 0.0
        for row in c.values():
            nz = row[row > 0]
            ll += float(np.sum(nz * np.log(nz / row.sum())))
        params = len(c) * max(v_obs - 1, 0)
        rows.append({"order": k, "loglik": ll, "params": params,
                     "bic": -2 * ll + params * math.log(max(n, 1)), "contexts": len(c)})
    if not n:
        return {"n": 0, "orders": rows, "best": None, "delta_bic": None}
    srt = sorted(rows, key=lambda r: r["bic"])
    return {"n": n, "orders": rows, "best": srt[0]["order"],
            "delta_bic": (srt[1]["bic"] - srt[0]["bic"]) if len(srt) > 1 else None}


# ---- logistic regression -----------------------------------------------------------------------

def _dt_bucket(dt) -> int:
    if not isinstance(dt, (int, float)):
        return 0
    return 1 if dt < 5 else 2 if dt < 30 else 3 if dt < 120 else 4


N_FEAT = V + 1 + 2 + 2 * (V + 1) + len(PHASES) + 4 + 4 + 5 + 4 + 1
N_REGIME = 2                         # A2-D columns appended with regime=True


def n_features(regime: bool = False) -> int:
    return N_FEAT + (N_REGIME if regime else 0)


def prefix_features(steps, regime: bool = False) -> np.ndarray:
    """(len(steps)+1, n_features(regime)): row i describes the task after its first i steps.

    task-so-far class shares (V), log1p(i), error share, last step errored, last and second-last
    action one-hot (V+1 each, START included), last phase (5), last out/in size bucket (4+4), last
    dt bucket (5), tests: any run so far / last run failing / progress 1 − failing/initial /
    progress defined, any subagent spawned so far.

    ``regime=True`` (attempt 2, A2-D) appends the day regime of the last step seen
    (``features.regime_values`` of its ``_regime`` annotation — log1p(session errors in the last
    15 min), session-day error rate so far; 0, 0 in row 0), the same two numbers the JEPA reads.
    """
    L = len(steps)
    X = np.zeros((L + 1, n_features(regime)))
    if regime:
        from .features import regime_values
        for i in range(1, L + 1):
            X[i, N_FEAT:] = regime_values(steps[i - 1])
    counts = np.zeros(V)
    errs = 0
    ran = failing_last = 0
    f_ref = None
    prog = 0.0
    has_prog = 0
    spawned = 0
    for i in range(L + 1):
        o = 0
        if i:
            counts[act_id(steps[i - 1])] += 1
        X[i, o:o + V] = counts / i if i else 0.0
        o += V
        X[i, o] = math.log1p(i)
        o += 1
        if i:
            s = steps[i - 1]
            errs += 1 if s.get("err") else 0
            t = s.get("tests") or {}
            if t.get("ran") and isinstance(t.get("failed"), int):
                ran = 1
                f = t["failed"]
                failing_last = 1 if f > 0 else 0
                if f_ref is None and f > 0:
                    f_ref = f
                if f_ref:
                    prog, has_prog = 1.0 - f / f_ref, 1
            spawned = max(spawned, 1 if s.get("spawn") else 0)
        X[i, o] = errs / i if i else 0.0
        X[i, o + 1] = 1.0 if (i and steps[i - 1].get("err")) else 0.0
        o += 2
        last = act_id(steps[i - 1]) if i >= 1 else START
        last2 = act_id(steps[i - 2]) if i >= 2 else START
        X[i, o + last] = 1.0
        o += V + 1
        X[i, o + last2] = 1.0
        o += V + 1
        if i:
            s = steps[i - 1]
            ph = s.get("phase")
            if ph in PHASES:
                X[i, o + PHASES.index(ph)] = 1.0
            o += len(PHASES)
            for key in ("out_b", "in_b"):
                b = s.get(key)
                if isinstance(b, int) and 0 <= b <= 3:
                    X[i, o + b] = 1.0
                o += 4
            X[i, o + _dt_bucket(s.get("dt"))] = 1.0
            o += 5
        else:
            o += len(PHASES) + 8 + 5
        X[i, o:o + 4] = (ran, failing_last, prog, has_prog)
        o += 4
        X[i, o] = spawned
    return X


def _fit_softmax(X, y, n_cls, lam):
    """Multinomial logistic regression, L2 on W only; L-BFGS from zeros (deterministic)."""
    from scipy.optimize import minimize
    n, f = X.shape
    Y = np.zeros((n, n_cls))
    Y[np.arange(n), y] = 1.0

    def obj(w):
        W = w[:f * n_cls].reshape(f, n_cls)
        b = w[f * n_cls:]
        Z = X @ W + b
        Z -= Z.max(axis=1, keepdims=True)
        lse = np.log(np.exp(Z).sum(axis=1, keepdims=True))
        logp = Z - lse
        loss = -np.sum(Y * logp) / n + 0.5 * lam * np.sum(W * W)
        G = (np.exp(logp) - Y) / n
        gW = X.T @ G + lam * W
        return loss, np.concatenate([gW.ravel(), G.sum(axis=0)])

    w0 = np.zeros(f * n_cls + n_cls)
    res = minimize(obj, w0, jac=True, method="L-BFGS-B", options={"maxiter": 500})
    return res.x[:f * n_cls].reshape(f, n_cls), res.x[f * n_cls:]


def _fit_binary(X, y, lam, sw=None):
    """Weighted binary logistic regression (weights normalised to mean 1), L2 on w only."""
    from scipy.optimize import minimize
    n, f = X.shape
    sw = np.ones(n) if sw is None else np.asarray(sw, dtype=float) * n / np.sum(sw)

    def obj(w):
        z = X @ w[:f] + w[f]
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -50, 50)))
        loss = float(sw @ (np.logaddexp(0.0, z) - y * z)) / n + 0.5 * lam * float(w[:f] @ w[:f])
        g = sw * (p - y) / n
        return loss, np.concatenate([X.T @ g + lam * w[:f], [g.sum()]])

    res = minimize(obj, np.zeros(f + 1), jac=True, method="L-BFGS-B", options={"maxiter": 500})
    return res.x[:f], res.x[f]


class Logistic:
    name = "logistic (hand features)"
    LAMS = (1e-3, 1e-2, 1e-1, 1.0)

    def __init__(self, lam=None, lam_outcome=None, select_prefix=None, regime: bool = False):
        from .protocol import OUTCOME_PREFIX
        self.lam, self.lam_outcome = lam, lam_outcome
        self.regime = bool(regime)          # A2-D: + day-regime columns (steps must be annotated)
        if self.regime:
            self.name = "logistic (hand features + regime)"
        self.select_prefix = OUTCOME_PREFIX if select_prefix is None else select_prefix

    def _xy(self, ds, ids):
        Xs, ys = [], []
        for tid in ids:
            st = ds.steps[tid]
            Xs.append(prefix_features(st, self.regime)[:-1])
            ys.extend(act_id(s) for s in st)
        return (np.vstack(Xs) if Xs else np.zeros((0, n_features(self.regime)))), np.array(ys, dtype=int)

    def _task_x(self, steps):
        return prefix_features(steps, self.regime)[-1]

    def _outcome_xy(self, ds, ids):
        """Every prefix of every labeled task is a sample of P(success | state so far) — the value
        head's job — weighted 1/(L+1) so each task counts once."""
        Xs, ys, ws = [], [], []
        for t in ids:
            y = label(ds.tasks[t])
            if y is None:
                continue
            X = prefix_features(ds.steps[t], self.regime)
            Xs.append(X)
            ys.extend([y] * len(X))
            ws.extend([1.0 / len(X)] * len(X))
        if not Xs:
            return np.zeros((0, n_features(self.regime))), np.zeros(0), np.zeros(0)
        return np.vstack(Xs), np.array(ys, dtype=float), np.array(ws)

    def fit(self, ds, train_ids, val_ids=None):
        if self.regime:
            from .features import has_regime
            if not any(has_regime(s) for t in train_ids for s in ds.steps[t]):
                raise ValueError("regime=True needs steps annotated by features.annotate_regime")
        X, y = self._xy(ds, train_ids)
        self.mu = X.mean(axis=0) if len(X) else np.zeros(n_features(self.regime))
        sd = X.std(axis=0) if len(X) else np.ones(n_features(self.regime))
        self.sd = np.where(sd > 0, sd, 1.0)
        Z = (X - self.mu) / self.sd
        lams = (self.lam,) if self.lam is not None else (self.LAMS if val_ids else (1e-2,))
        best = None
        for lam in lams:
            self.W, self.b = _fit_softmax(Z, y, V, lam)
            if len(lams) == 1:
                best = (0.0, lam, self.W, self.b)
                break
            ce = _val_ce(self, ds, val_ids)
            if best is None or ce < best[0]:
                best = (ce, lam, self.W, self.b)
        _, self.lam, self.W, self.b = best
        self._fit_outcome(ds, train_ids, val_ids)
        return self

    def _fit_outcome(self, ds, train_ids, val_ids):
        from .protocol import prefix_of
        self.base = _base_rate(ds, train_ids)
        self.ow = None
        X, ys, ws = self._outcome_xy(ds, train_ids)
        if len(set(ys.tolist())) < 2 or sum(label(ds.tasks[t]) is not None for t in train_ids) < 10:
            return
        self.omu = X.mean(axis=0)
        sd = X.std(axis=0)
        self.osd = np.where(sd > 0, sd, 1.0)
        Z = (X - self.omu) / self.osd
        vlab = [t for t in (val_ids or []) if label(ds.tasks[t]) is not None]
        lams = ((self.lam_outcome,) if self.lam_outcome is not None
                else (self.LAMS if len(vlab) >= 10 else (1e-1,)))
        best = None
        for lam in lams:
            self.ow, self.ob = _fit_binary(Z, ys, lam, ws)
            if len(lams) == 1:
                best = (0.0, lam, self.ow, self.ob)
                break
            br = np.mean([(self.outcome_proba(prefix_of(ds.steps[t], self.select_prefix))
                           - label(ds.tasks[t])) ** 2 for t in vlab])
            if best is None or br < best[0]:
                best = (br, lam, self.ow, self.ob)
        _, self.lam_outcome, self.ow, self.ob = best

    def _logsoftmax(self, X):
        Z = ((X - self.mu) / self.sd) @ self.W + self.b
        Z -= Z.max(axis=1, keepdims=True)
        return Z - np.log(np.exp(Z).sum(axis=1, keepdims=True))

    def predict_proba(self, history, task=None):
        return np.exp(self._logsoftmax(prefix_features(history, self.regime)[-1:])[0])

    def task_logprobs(self, steps, task=None):
        if not steps:
            return np.zeros(0)
        lp = self._logsoftmax(prefix_features(steps, self.regime)[:-1])
        return lp[np.arange(len(steps)), [act_id(s) for s in steps]]

    def outcome_proba(self, steps, task=None):
        if self.ow is None:
            return self.base
        z = float(((self._task_x(steps) - self.omu) / self.osd) @ self.ow + self.ob)
        return 1.0 / (1.0 + math.exp(-max(min(z, 50.0), -50.0)))


def all_baselines(orders=(1, 2), regime: bool = False, err_states: bool = False) -> list:
    """G1's baseline set. Attempt 2: ``regime`` (A2-D) gives the logistic model the day-regime
    columns, ``err_states`` (A2-E) gives the Markov outcome readout the error-conditioned chain;
    the next-action Markov chains are unchanged."""
    return ([Unigram()] + [Markov(order=k, err_states=err_states) for k in orders]
            + [Logistic(regime=regime)])


__all__ = ["ACTIONS", "A_INDEX", "Unigram", "Markov", "Logistic", "bic_order", "prefix_features",
           "all_baselines"]
