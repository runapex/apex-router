"""P6 world model (E3): training, checkpoints, collapse diagnostics, linear probes, ``predict``.

Data: ``<data home>/steps.jsonl`` + ``tasks.jsonl`` (contract §1, written by E1; this module only
reads them) or the built-in synthetic generator (``synthetic_dataset``). Train/val come from the
tasks' ``split`` field; the ``test`` split is never read here (E4 scores it once).

A run writes ``<data home>/runs/<run_id>/``: ``config.json``, ``metrics.jsonl`` (one line per
epoch), ``weights.safetensors`` (best epoch by val next-action CE) and ``summary.json`` (params,
throughput, best-epoch metrics, collapse diagnostics, linear probes, unigram prior). Directories
are 0700, files 0600. MLX is imported lazily inside the functions that need it.
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import numpy as np

from . import features as F
from .jepa import collapse_diagnostics, random_directions

SPLITS = ("train", "val", "test")


@dataclass
class TrainConfig:
    seed: int = 0
    # model
    d: int = 64
    d_control: int = 16
    hidden: int = 256
    layers: int = 2
    heads: int = 4
    pred_hidden: int = 256
    act_dim: int = 16
    lookback: int = 2
    # objective
    k: int = 4
    L: int = 32
    regularizer: str = "sigreg"          # "sigreg" | "vicreg" (ablation)
    sigreg_dirs: int = 64
    sigreg_knots: int = 17
    sigreg_tmax: float = 3.0
    w_pred: float = 1.0
    # SIGReg here is the per-sample statistic (see jepa.py); LeJEPA's λ≈0.05 on the N-scaled
    # statistic is ~λ·N per sample, so the weight is O(10). w_reg=1 collapsed on synthetic data
    # (effective rank 2); 10 kept the rank at ~30 of 64.
    w_reg: float = 10.0
    w_next: float = 1.0
    w_value: float = 0.5
    # optimisation
    lr: float = 5e-4
    warmup: int = 200                    # linear LR warm-up steps (capped at 10% of all updates)
    clip: float = 1.0                    # global grad-norm clip (0 = off)
    weight_decay: float = 0.01
    epochs: int = 20
    batch: int = 32
    patience: int = 3
    # data
    macro: bool = False
    max_macro: int | None = 6
    embed: bool = False
    value_gold_only: bool = False
    # preset collapse bounds (G1 criterion 4)
    erank_min: float = 8.0
    sigreg_max: float = 0.10

    @classmethod
    def from_dict(cls, d: dict) -> "TrainConfig":
        names = {f.name for f in fields(cls)}
        unknown = set(d) - names
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        return cls(**d)


# ---- paths and I/O -------------------------------------------------------------------------------

def data_home() -> Path:
    return Path(os.environ.get("APEX_ROUTER_HOME") or Path.home() / ".apex-router") / "worldmodel"


def _mkdir_private(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _append_private(path: Path, line: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(line + "\n")


def read_jsonl(path: Path) -> list[dict]:
    """Fail-open JSONL reader: malformed lines and non-objects are skipped."""
    out = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    out.append(r)
    except OSError:
        pass
    return out


def _json_default(o):
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o).__name__)


def _dumps(o) -> str:
    return json.dumps(o, default=_json_default, sort_keys=True)


# ---- datasets ------------------------------------------------------------------------------------

@dataclass
class Dataset:
    """Steps grouped by task plus the per-task record (outcome, outcome_src, split)."""
    tasks: dict[str, list[dict]]
    meta: dict[str, dict]
    source: str = "steps.jsonl"

    def split(self, name: str) -> tuple[dict[str, list[dict]], dict[str, str]]:
        ids = [t for t in self.tasks if self.meta.get(t, {}).get("split") == name]
        return {t: self.tasks[t] for t in ids}, {t: self.meta[t].get("outcome") for t in ids}

    def stats(self) -> dict:
        out = {"source": self.source, "tasks": len(self.tasks),
               "steps": sum(len(v) for v in self.tasks.values())}
        for s in SPLITS:
            ids = [t for t in self.tasks if self.meta.get(t, {}).get("split") == s]
            out[f"tasks_{s}"] = len(ids)
            out[f"labelled_{s}"] = sum(1 for t in ids
                                       if F.outcome_target(self.meta[t].get("outcome"))[1])
        return out


def load_dataset(home: Path | None = None) -> Dataset:
    """Read E1's ``steps.jsonl`` + ``tasks.jsonl`` (fail-open). Steps whose task has no
    ``tasks.jsonl`` record (hence no split) are dropped."""
    home = home or data_home()
    meta = {r["task"]: r for r in read_jsonl(home / "tasks.jsonl") if isinstance(r.get("task"), str)}
    tasks = {t: s for t, s in F.group_steps(read_jsonl(home / "steps.jsonl")).items() if t in meta}
    return Dataset(tasks=tasks, meta=meta, source=str(home / "steps.jsonl"))


def _phase_for(acts: list[str], t: int, first_edit: int | None) -> str:
    a = acts[t]
    if a in ("edit", "write"):
        return "edit"
    if a in ("search", "read") and (first_edit is None or t < first_edit):
        return "explore"
    if a in ("test", "build", "run") and first_edit is not None and t > first_edit:
        return "verify"
    if a in ("vcs", "ask") and t >= len(acts) - 2:
        return "deliver"
    return "other"


def synthetic_dataset(n_tasks: int = 300, seed: int = 0) -> Dataset:
    """Contract-schema synthetic tasks with learnable structure (for smoke tests and the CLI).

    - Actions follow an order-2 chain: ``P(a_t | a_{t-2}, a_{t-1})`` rows drawn from a sparse
      Dirichlet, so order 2 carries information beyond order 1 and the unigram prior.
    - A hidden per-task difficulty h raises error rates and the initial failing-test count; while
      tests are failing the next action is pulled towards ``edit``.
    - The outcome is tied to the trajectory: with tests, success iff the last test run had 0
      failing and the error share is < 0.3; without tests, a Bernoulli on errors and h. 15% of
      tasks are ``unknown`` (unlabelled) to exercise the value-head mask.
    - Sessions hold 1–4 tasks; split by session order 70/10/20 (train/val/test), as the contract.
    """
    rng = np.random.default_rng(seed)
    A = F.N_ACT
    trans = rng.dirichlet(np.full(A, 0.25), size=(A, A))           # [a_{t-2}, a_{t-1}] -> a_t
    start = rng.dirichlet(np.full(A, 1.0))
    edit_i, test_i = F.ACT_INDEX["edit"], F.ACT_INDEX["test"]
    typical_out = rng.integers(0, 4, A)
    steps_out: list[dict] = []
    meta: dict[str, dict] = {}
    sessions: list[list[str]] = []
    ts = 1.7e9
    k = 0
    while k < n_tasks:
        s = len(sessions)
        sid = f"syn-{seed}-{s:05d}"
        ids = []
        for o in range(int(rng.integers(1, 5))):
            if k >= n_tasks:
                break
            tid = f"{sid}:{o}"
            ids.append(tid)
            k += 1
            h = float(rng.uniform())
            n = int(min(60, 4 + rng.geometric(0.07)))
            acts: list[int] = []
            failing = None
            recs = []
            errs = 0
            t0 = ts
            for i in range(n):
                if i == 0:
                    p = start
                else:
                    p = trans[acts[-2] if i >= 2 else acts[-1], acts[-1]].copy()
                    if failing:
                        p = 0.6 * p
                        p[edit_i] += 0.4
                a = int(rng.choice(A, p=p / p.sum()))
                acts.append(a)
                name = F.ACTIONS[a]
                err = int(name in ("run", "test", "build", "remote") and rng.uniform() < 0.05 + 0.35 * h)
                errs += err
                tests = {"ran": 0, "failed": None, "passed": None}
                if a == test_i:
                    if failing is None:
                        failing = int(rng.poisson(6 * h))
                    else:
                        fixed = int(rng.binomial(failing, max(0.05, 0.6 - 0.5 * h)))
                        failing = max(0, failing - fixed + int(rng.uniform() < 0.1 * h))
                    tests = {"ran": 1, "failed": failing, "passed": int(rng.integers(5, 200))}
                dt = float(rng.exponential(20.0))
                ts += dt
                recs.append({"sid": sid, "agent": None, "src": "pi", "task": tid, "i": i, "ts": ts,
                             "act": name, "tool": "synthetic", "err": err, "tests": tests,
                             "out_b": int(min(3, max(0, typical_out[a] + rng.integers(-1, 2)))),
                             "in_b": int(rng.integers(0, 2)), "dt": None if i == 0 else dt,
                             "model": None, "spawn": int(name == "delegate")})
            first_edit = next((i for i, a in enumerate(acts) if F.ACTIONS[a] in ("edit", "write")), None)
            names = [F.ACTIONS[a] for a in acts]
            for i, r in enumerate(recs):
                r["phase"] = _phase_for(names, i, first_edit)
            steps_out.extend(recs)
            if failing is not None:
                ok = failing == 0 and errs / n < 0.3
            else:
                ok = rng.uniform() < 1 / (1 + math.exp(-(2.0 - 8.0 * errs / n - 2.0 * h)))
            outcome = "success" if ok else ("partial" if failing == 1 else "fail")
            if rng.uniform() < 0.15:
                outcome = "unknown"
            meta[tid] = {"task": tid, "sid": sid, "src": "pi", "t0": t0, "t1": ts, "steps": n,
                         "outcome": outcome, "outcome_src": "none" if outcome == "unknown" else "weak"}
            ts += 600
        sessions.append(ids)
    S = len(sessions)
    for si, ids in enumerate(sessions):
        split = "train" if si < 0.7 * S else "val" if si < 0.8 * S else "test"
        for t in ids:
            meta[t]["split"] = split
    return Dataset(tasks=F.group_steps(steps_out), meta=meta, source=f"synthetic:{n_tasks}:seed{seed}")


def _windows(ds: Dataset, split: str, cfg: TrainConfig) -> F.Windows:
    tasks, outcomes = ds.split(split)
    if cfg.value_gold_only:
        outcomes = {t: (o if ds.meta[t].get("outcome_src") == "gold" else None)
                    for t, o in outcomes.items()}
    extra = None
    if cfg.embed:
        from .embed import task_embeddings
        extra = task_embeddings(sorted(tasks))
    return F.build_windows(tasks, outcomes, L=cfg.L, macro=cfg.macro, max_macro=cfg.max_macro,
                           extra=extra)


def n_features(cfg: TrainConfig) -> int:
    from .embed import EMBED_DIM
    return F.N_FEAT + (EMBED_DIM + 1 if cfg.embed else 0)


# ---- evaluation ----------------------------------------------------------------------------------

def _forward_all(model, W: F.Windows, batch: int = 256):
    """Run the encoder + heads over every window; returns numpy ``z (W, L, d)``, next-action
    log-probs ``(W, L, A)`` and value probabilities ``(W, L)``."""
    import mlx.core as mx
    zs, lps, vs = [], [], []
    for b in range(0, len(W), batch):
        x = mx.array(W.X[b:b + batch])
        z, logits, vlogit = model(x)
        lp = logits - mx.logsumexp(logits, axis=-1, keepdims=True)
        v = mx.sigmoid(vlogit)
        mx.eval(z, lp, v)
        zs.append(np.array(z))
        lps.append(np.array(lp))
        vs.append(np.array(v))
    d = model.d
    if not zs:
        return (np.zeros((0, W.X.shape[1], d), np.float32), np.zeros((0, W.X.shape[1], F.N_ACT)),
                np.zeros((0, W.X.shape[1])))
    return np.concatenate(zs), np.concatenate(lps), np.concatenate(vs)


def _last_positions(W: F.Windows) -> dict[int, tuple[int, int]]:
    """task_idx -> (window, position) of the task's last real step."""
    last: dict[int, tuple[int, int]] = {}
    for w in range(len(W)):
        n = int(W.mask[w].sum())
        if n == 0:
            continue
        ti = int(W.task_idx[w])
        cur = last.get(ti)
        if cur is None or W.start[w] > W.start[cur[0]]:
            last[ti] = (w, n - 1)
    return last


def evaluate(model, W: F.Windows, cfg: TrainConfig, y_train: np.ndarray) -> tuple[dict, dict]:
    """Val metrics + collapse diagnostics. Returns ``(metrics, arrays)``; ``arrays`` holds the
    flattened real-position latents and targets for the probes."""
    import mlx.core as mx
    from .jepa import jepa_losses
    z, lp, v = _forward_all(model, W)
    m = W.mask.astype(bool)
    yv = W.y_next
    ym = (yv >= 0) & m
    ce = float(-np.take_along_axis(lp, np.maximum(yv, 0)[..., None], -1)[..., 0][ym].mean()) \
        if ym.any() else float("nan")
    # step-level value metrics on labelled tasks
    lab = (W.has_label[:, None] > 0) & m
    tgt = np.broadcast_to(W.outcome[:, None], v.shape)
    vp = np.clip(v[lab], 1e-6, 1 - 1e-6)
    vt = tgt[lab]
    bce = float(-(vt * np.log(vp) + (1 - vt) * np.log(1 - vp)).mean()) if vp.size else float("nan")
    # task-level Brier at the task's last step (protocol: P(success) vs label per task)
    last = _last_positions(W)
    tb = [(float(v[w, p]), float(W.outcome[w])) for ti, (w, p) in last.items() if W.has_label[w] > 0]
    brier_task = float(np.mean([(a - b) ** 2 for a, b in tb])) if tb else float("nan")
    base_rate = float(np.mean([b for _, b in tb])) if tb else float("nan")
    # latent prediction / regulariser losses on val (fixed directions for comparability)
    dirs = mx.array(random_directions(np.random.default_rng(10_000 + cfg.seed), cfg.d, cfg.sigreg_dirs))
    preds, n = [], 0
    for b in range(0, len(W), 256):
        sl = slice(b, b + 256)
        _, parts = jepa_losses(model, mx.array(W.X[sl]), mx.array(W.mask[sl]), mx.array(W.act[sl]),
                               mx.array(W.y_next[sl]), mx.array(W.outcome[sl]),
                               mx.array(W.has_label[sl]), dirs, cfg)
        mx.eval(parts["pred"])
        nb = float(W.mask[sl].sum())
        preds.append(float(parts["pred"]) * nb)
        n += nb
    zf = z[m]
    diag = collapse_diagnostics(zf, erank_min=cfg.erank_min, sigreg_max=cfg.sigreg_max, seed=cfg.seed)
    metrics = {
        "val_next_ce": ce, "val_next_ppl": math.exp(ce) if math.isfinite(ce) else float("nan"),
        "val_unigram_ce": F.unigram_ce(y_train, yv[ym]),
        "val_next_acc": float((lp.argmax(-1)[ym] == yv[ym]).mean()) if ym.any() else float("nan"),
        "val_value_bce": bce, "val_brier_task": brier_task, "val_brier_task_baserate":
            (float(np.mean([(base_rate - b) ** 2 for _, b in tb])) if tb else float("nan")),
        "val_tasks_labelled": len(tb),
        "val_pred": sum(preds) / max(n, 1.0),
        "val_steps": int(m.sum()),
        "collapse": diag,
    }
    arrays = {"z": zf, "y_next": yv[m], "phase": W.phase[m], "fail_b": W.fail_b[m], "x": W.X[m],
              "z_last": np.array([z[w, p] for _, (w, p) in last.items() if W.has_label[w] > 0]),
              "x_last": np.array([W.X[w, p] for _, (w, p) in last.items() if W.has_label[w] > 0]),
              "outcome_last": np.array([W.outcome[w] for _, (w, p) in last.items() if W.has_label[w] > 0])}
    return metrics, arrays


# ---- linear probes (numpy) -----------------------------------------------------------------------

def fit_logistic(X: np.ndarray, y: np.ndarray, n_classes: int, l2: float = 1e-3,
                 iters: int = 500):
    """Multinomial logistic regression by full-batch gradient descent on standardised inputs
    (numpy only). The step is 1/L with L = λ_max(XᵀX/n)/2 + l2, the Lipschitz bound of the
    softmax-CE gradient, so it converges even when columns are collinear (a low-rank z).
    Returns ``predict_proba(X) -> (n, n_classes)``."""
    X = np.asarray(X, np.float64)
    mu, sd = X.mean(0), X.std(0) + 1e-6
    Xs = np.hstack([(X - mu) / sd, np.ones((X.shape[0], 1))])
    Y = np.eye(n_classes)[y]
    Wt = np.zeros((Xs.shape[1], n_classes))
    reg = np.ones((Xs.shape[1], 1))
    reg[-1] = 0.0
    n = max(Xs.shape[0], 1)
    lam = float(np.linalg.eigvalsh(Xs.T @ Xs / n)[-1])
    lr = 1.0 / (lam / 2 + l2)
    for _ in range(iters):
        S = Xs @ Wt
        S -= S.max(1, keepdims=True)
        P = np.exp(S)
        P /= P.sum(1, keepdims=True)
        Wt -= lr * (Xs.T @ (P - Y) / n + l2 * reg * Wt)

    def predict_proba(Xn):
        Xn = np.hstack([(np.asarray(Xn, np.float64) - mu) / sd, np.ones((len(Xn), 1))])
        S = Xn @ Wt
        S -= S.max(1, keepdims=True)
        P = np.exp(S)
        return P / P.sum(1, keepdims=True)

    return predict_proba


def _probe(Xtr, ytr, Xva, yva, n_classes: int) -> dict:
    if len(ytr) < 2 or len(yva) == 0 or len(np.unique(ytr)) < 2:
        return {"n_train": int(len(ytr)), "n_val": int(len(yva)), "skipped": True}
    P = fit_logistic(Xtr, ytr, n_classes)(Xva)
    p_true = np.clip(P[np.arange(len(yva)), yva], 1e-9, 1)
    prior = (np.bincount(ytr, minlength=n_classes) + 0.5) / (len(ytr) + 0.5 * n_classes)
    out = {"n_train": int(len(ytr)), "n_val": int(len(yva)),
           "ce": float(-np.log(p_true).mean()), "prior_ce": float(-np.log(prior[yva]).mean()),
           "acc": float((P.argmax(1) == yva).mean())}
    if n_classes == 2:
        out["brier"] = float(((P[:, 1] - yva) ** 2).mean())
        out["prior_brier"] = float(((prior[1] - yva) ** 2).mean())
    return out


def linear_probes(tr: dict, va: dict, d_control: int, max_rows: int = 20_000, seed: int = 0) -> dict:
    """Linear probes on frozen z (train-split fit, val-split score) for next action, phase,
    failing-tests bucket (per step) and outcome (per labelled task, from ``z_control`` at the
    task's last step); each with the same probe on the raw step features as a reference."""
    rng = np.random.default_rng(seed)

    def cap(a: dict, keys):
        n = len(a[keys[0]])
        idx = rng.choice(n, max_rows, replace=False) if n > max_rows else np.arange(n)
        return [a[k][idx] for k in keys]

    out = {}
    ztr, xtr, ytr, ptr, ftr = cap(tr, ["z", "x", "y_next", "phase", "fail_b"])
    zva, xva, yva, pva, fva = cap(va, ["z", "x", "y_next", "phase", "fail_b"])
    k_tr, k_va = ytr >= 0, yva >= 0
    out["next_action"] = {"z": _probe(ztr[k_tr], ytr[k_tr], zva[k_va], yva[k_va], F.N_ACT),
                          "raw": _probe(xtr[k_tr], ytr[k_tr], xva[k_va], yva[k_va], F.N_ACT)}
    out["phase"] = {"z": _probe(ztr, ptr, zva, pva, F.N_PHASE)}
    out["fail_bucket"] = {"z": _probe(ztr, ftr, zva, fva, F.N_FAIL_B)}
    if len(tr["z_last"]) and len(va["z_last"]):
        zc = slice(tr["z_last"].shape[1] - d_control, None)
        out["outcome"] = {
            "z_control": _probe(tr["z_last"][:, zc], tr["outcome_last"].astype(int),
                                va["z_last"][:, zc], va["outcome_last"].astype(int), 2),
            "z": _probe(tr["z_last"], tr["outcome_last"].astype(int),
                        va["z_last"], va["outcome_last"].astype(int), 2),
            "raw": _probe(tr["x_last"], tr["outcome_last"].astype(int),
                          va["x_last"], va["outcome_last"].astype(int), 2)}
    return out


# ---- training ------------------------------------------------------------------------------------

def _run_id(source: str, seed: int) -> str:
    tag = "synthetic" if source.startswith("synthetic") else "steps"
    return f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{tag}-s{seed}"


def train(cfg: TrainConfig, ds: Dataset, run_dir: Path | None = None, run_id: str | None = None,
          log=print) -> dict:
    """Train one model; returns the summary dict (also written to ``summary.json``)."""
    import mlx.core as mx
    import mlx.nn as nn
    import mlx.optimizers as optim
    from .jepa import build_model, count_params, jepa_losses

    mx.random.seed(cfg.seed)
    rng = np.random.default_rng(cfg.seed)
    Wtr, Wva = _windows(ds, "train", cfg), _windows(ds, "val", cfg)
    if len(Wtr) == 0 or len(Wva) == 0:
        raise ValueError(f"need train and val windows (train={len(Wtr)}, val={len(Wva)})")
    y_train = Wtr.y_next[(Wtr.y_next >= 0) & (Wtr.mask > 0)]

    model = build_model(n_features(cfg), cfg)
    mx.eval(model.parameters())
    n_params = count_params(model)
    warm = min(cfg.warmup, (cfg.epochs * math.ceil(len(Wtr) / cfg.batch)) // 10)
    sched = (optim.join_schedules([optim.linear_schedule(cfg.lr * 0.01, cfg.lr, warm),
                                   lambda _: cfg.lr], [warm]) if warm > 0 else cfg.lr)
    opt = optim.AdamW(learning_rate=sched, weight_decay=cfg.weight_decay)

    def loss_fn(mdl, x, m, a, y, o, h, dirs):
        return jepa_losses(mdl, x, m, a, y, o, h, dirs, cfg)

    step_fn = nn.value_and_grad(model, loss_fn)

    run_id = run_id or _run_id(ds.source, cfg.seed)
    run_dir = run_dir or (data_home() / "runs" / run_id)
    _mkdir_private(run_dir.parent)
    _mkdir_private(run_dir)
    _write_private(run_dir / "config.json", _dumps(asdict(cfg)))
    metrics_path = run_dir / "metrics.jsonl"
    if metrics_path.exists():
        metrics_path.unlink()

    best, best_epoch, bad = float("inf"), -1, 0
    best_metrics: dict = {}
    train_time, train_windows, train_steps = 0.0, 0, 0
    for epoch in range(cfg.epochs):
        perm = rng.permutation(len(Wtr))
        sums: dict[str, float] = {}
        nb = 0
        t0 = time.perf_counter()
        for b in range(0, len(perm), cfg.batch):
            idx = np.sort(perm[b:b + cfg.batch])
            dirs = mx.array(random_directions(rng, cfg.d, cfg.sigreg_dirs))
            (total, parts), grads = step_fn(
                model, mx.array(Wtr.X[idx]), mx.array(Wtr.mask[idx]), mx.array(Wtr.act[idx]),
                mx.array(Wtr.y_next[idx]), mx.array(Wtr.outcome[idx]), mx.array(Wtr.has_label[idx]),
                dirs)
            if cfg.clip > 0:
                grads, _ = optim.clip_grad_norm(grads, cfg.clip)
            opt.update(model, grads)
            mx.eval(model.parameters(), opt.state, total, parts)
            sums["total"] = sums.get("total", 0.0) + float(total)
            for kname, v in parts.items():
                sums[kname] = sums.get(kname, 0.0) + float(v)
            nb += 1
            train_windows += len(idx)
            train_steps += int(Wtr.mask[idx].sum())
        train_time += time.perf_counter() - t0
        tr = {f"train_{k}": v / max(nb, 1) for k, v in sorted(sums.items())}
        ev, _ = evaluate(model, Wva, cfg, y_train)
        row = {"epoch": epoch, **tr, **ev}
        _append_private(metrics_path, _dumps(row))
        c = ev["collapse"]
        log(f"epoch {epoch}: train total {tr['train_total']:.4f} next_ce {tr['train_next_ce']:.4f} "
            f"| val next_ce {ev['val_next_ce']:.4f} (unigram {ev['val_unigram_ce']:.4f}) "
            f"brier {ev['val_brier_task']:.4f} | erank {c['effective_rank']:.1f} "
            f"sigreg {c['sigreg']:.4f} cos {c['mean_cosine']:.3f}"
            + ("" if c["within_bounds"] else "  [OUTSIDE collapse bounds]"))
        if ev["val_next_ce"] < best - 1e-6:
            best, best_epoch, bad, best_metrics = ev["val_next_ce"], epoch, 0, row
            model.save_weights(str(run_dir / "weights.safetensors"))
            os.chmod(run_dir / "weights.safetensors", 0o600)
        else:
            bad += 1
            if bad >= cfg.patience:
                log(f"early stop at epoch {epoch} (best {best_epoch})")
                break

    # probes + final diagnostics on the best checkpoint
    if best_epoch < 0:                       # val CE never finite: keep the last weights
        model.save_weights(str(run_dir / "weights.safetensors"))
        os.chmod(run_dir / "weights.safetensors", 0o600)
    model.load_weights(str(run_dir / "weights.safetensors"))
    _, arr_tr = evaluate(model, Wtr, cfg, y_train)
    ev_va, arr_va = evaluate(model, Wva, cfg, y_train)
    probes = linear_probes(arr_tr, arr_va, cfg.d_control, seed=cfg.seed)
    prior = (np.bincount(y_train, minlength=F.N_ACT) + 0.5) / (y_train.size + 0.5 * F.N_ACT)
    summary = {
        "run_id": run_id, "config": asdict(cfg), "data": ds.stats(), "params": n_params,
        "n_features": n_features(cfg),
        "windows": {"train": len(Wtr), "val": len(Wva)},
        "throughput": {"train_seconds": train_time,
                       "windows_per_s": train_windows / max(train_time, 1e-9),
                       "steps_per_s": train_steps / max(train_time, 1e-9)},
        "best_epoch": best_epoch, "best": best_metrics, "final_val": ev_va,
        "probes": probes, "unigram_prior": prior.tolist(),
        "train_unigram_ce": F.unigram_ce(y_train, y_train),
        "collapse_outside_bounds_epochs": [
            r["epoch"] for r in read_jsonl(metrics_path) if not r["collapse"]["within_bounds"]],
    }
    _write_private(run_dir / "summary.json", _dumps(summary))
    return summary


# ---- inference API (E2 / E4) ---------------------------------------------------------------------

class WorldModel:
    """A trained run, loaded for inference. ``WorldModel.load(run_id)`` (or a run directory).

    - ``predict(steps)`` -> ``{"z", "z_control", "next_action_probs", "p_success", "steps"}``:
      one row per (macro-)step; row t of ``next_action_probs`` is P(act_{t+1} | steps <= t);
      ``p_success[t]`` is the value head P(success | z_control_t) — the progress signal v_t.
    - ``action_logprobs(steps)`` -> ``(n,)`` log P(act_t | steps < t); t = 0 uses the run's
      unigram prior (fit on train), so a per-step CE is ``-mean(...)`` over every step.
    - ``score(tasks)`` -> per-task dict of the above for a ``{task: steps}`` mapping.
    """

    def __init__(self, run_dir: Path):
        from .jepa import build_model
        self.run_dir = Path(run_dir)
        self.cfg = TrainConfig.from_dict(json.loads((self.run_dir / "config.json").read_text()))
        summ = self.run_dir / "summary.json"
        self.summary = json.loads(summ.read_text()) if summ.exists() else {}
        self.prior = np.asarray(self.summary.get("unigram_prior") or np.full(F.N_ACT, 1 / F.N_ACT))
        self.model = build_model(n_features(self.cfg), self.cfg)
        self.model.load_weights(str(self.run_dir / "weights.safetensors"))

    @classmethod
    def load(cls, run: str | Path) -> "WorldModel":
        p = Path(run)
        if not p.is_dir():
            p = data_home() / "runs" / str(run)
        return cls(p)

    def _windows(self, tasks: dict[str, list[dict]]) -> F.Windows:
        extra = None
        if self.cfg.embed:
            from .embed import task_embeddings
            extra = task_embeddings(sorted(tasks))
        return F.build_windows(tasks, None, L=self.cfg.L, macro=self.cfg.macro,
                               max_macro=self.cfg.max_macro, extra=extra)

    def score(self, tasks: dict[str, list[dict]]) -> dict[str, dict]:
        W = self._windows(tasks)
        z, lp, v = _forward_all(self.model, W)
        out: dict[str, dict] = {}
        order = np.lexsort((W.start, W.task_idx))
        for w in order:
            tid = W.task_ids[int(W.task_idx[w])]
            n = int(W.mask[w].sum())
            o = out.setdefault(tid, {"z": [], "next_action_logprobs": [], "p_success": []})
            o["z"].append(z[w, :n])
            o["next_action_logprobs"].append(lp[w, :n])
            o["p_success"].append(v[w, :n])
        dc = self.cfg.d_control
        for tid, o in out.items():
            zz = np.concatenate(o["z"])
            lpp = np.concatenate(o["next_action_logprobs"])
            o.update(z=zz, z_control=zz[:, -dc:], next_action_probs=np.exp(lpp),
                     p_success=np.concatenate(o["p_success"]))
            del o["next_action_logprobs"]
            steps = sorted(tasks[tid], key=lambda s: F._int(s.get("i")))
            o["steps"] = F.macro_steps(steps, self.cfg.max_macro) if self.cfg.macro else steps
        return out

    def predict(self, steps: list[dict]) -> dict:
        tid = next((s.get("task") for s in steps if isinstance(s.get("task"), str)), "_task")
        return self.score({tid: [dict(s, task=tid) for s in steps]})[tid]

    def action_logprobs(self, steps: list[dict]) -> np.ndarray:
        p = self.predict(steps)
        acts = np.array([F.act_index(s) for s in p["steps"]], dtype=np.int64)
        out = np.empty(len(acts))
        if len(acts):
            out[0] = math.log(self.prior[acts[0]])
            out[1:] = np.log(np.clip(p["next_action_probs"][np.arange(len(acts) - 1), acts[1:]],
                                     1e-12, 1.0))
        return out


def probe_run(run: str | Path, ds: Dataset | None = None) -> dict:
    """Recompute collapse diagnostics + linear probes for a saved run on its data source."""
    wm = WorldModel.load(run)
    cfg = wm.cfg
    if ds is None:
        src = (wm.summary.get("data") or {}).get("source", "")
        if src.startswith("synthetic:"):
            _, n, sd = src.split(":")
            ds = synthetic_dataset(int(n), int(sd.replace("seed", "")))
        else:
            ds = load_dataset()
    Wtr, Wva = _windows(ds, "train", cfg), _windows(ds, "val", cfg)
    y_train = Wtr.y_next[(Wtr.y_next >= 0) & (Wtr.mask > 0)]
    _, arr_tr = evaluate(wm.model, Wtr, cfg, y_train)
    ev, arr_va = evaluate(wm.model, Wva, cfg, y_train)
    return {"run": str(wm.run_dir.name), "val": ev,
            "probes": linear_probes(arr_tr, arr_va, cfg.d_control, seed=cfg.seed)}
