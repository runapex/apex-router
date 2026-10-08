"""P6 world model (E3): an action-conditioned JEPA over agent tool steps, in MLX.

Nothing in this module imports ``mlx`` at import time: the model classes are built inside
``_mlx_classes()`` on first use, so the rest of apex-router (and its test suite) never needs the
``worldmodel`` extra. The collapse diagnostics at the bottom are numpy-only and always available.

Architecture (default config, 1.75M parameters; 2.34M with the request embedding; ceiling 12M):

- **Encoder.** Step features (``features.N_FEAT`` columns, + the optional 769-d request embedding)
  with the previous ``lookback`` (2) steps concatenated (a causal conv stem: plain attention
  at lr 1e-3 learned only the order-1 signal on synthetic order-2 data)
  -> linear to ``hidden`` + learned position embedding -> causal pre-norm transformer
  (``layers`` x ``heads``) -> linear to ``z`` (``d = 64``). Position t sees steps <= t only, so
  ``z_t`` is a filtering state usable online. ``z = [z_percept (d - d_control = 48),
  z_control (16)]`` (H-JEPA split): the value head reads ``z_control`` only.
- **Predictor.** ``P(z_t, a_{t+1}) -> ẑ_{t+1}``: an MLP on ``[z, embed(action)]`` with a residual
  connection, conditioned on the action class of the next step. Applied recursively for
  k = 1..K (``ẑ^{(k)} = P(ẑ^{(k-1)}, a_{t+k})``), each against the **stop-gradient** encoder
  output ``sg(z_{t+k})``. There is no EMA teacher: collapse is prevented by the regulariser.
- **Regulariser.** SIGReg (default) or a VICReg-style variance + covariance term (ablation).
- **Heads.** Next-action logits from ``z`` (cross-entropy; G1 criterion 1). Value head
  ``P(success | z_control)`` (BCE, masked to tasks with an outcome label).

Never predicts text: every target is structured task state (latents, action class, outcome).

SIGReg — the variant implemented here
-------------------------------------
Sketched Isotropic Gaussian Regularisation from LeJEPA (Balestriero & LeCun, 2025, "LeJEPA:
Provable and Scalable Self-Supervised Learning Without the Heuristics"; arXiv id recalled as
2511.08544, not re-read for this file). The target distribution of the embeddings is the
isotropic Gaussian N(0, I). A multivariate goodness-of-fit test is replaced by M random unit
directions u_m (the "sketch", resampled every step): every 1-D projection of N(0, I) is N(0, 1),
and by Cramér–Wold matching all 1-D projections matches the distribution. For each projection
x = z·u_m the univariate Epps–Pulley statistic (Epps & Pulley, 1983, Biometrika 70(3), "A test
for normality based on the empirical characteristic function") compares the empirical
characteristic function φ̂(t) = mean_j exp(i t x_j) with the N(0, 1) one φ(t) = exp(-t²/2):

    EP_m = ∫ |φ̂(t) - φ(t)|² w(t) dt,   w(t) = exp(-t²/2)
         = ∫ [(mean cos(t x) - e^{-t²/2})² + (mean sin(t x))²] w(t) dt

integrated by the trapezoid rule on ``knots`` points over [0, t_max] and doubled (the integrand
is even in t). ``sigreg = mean_m EP_m``. It is differentiable, needs no EMA/stop-gradient trick,
is O(N·M·knots), and is 0 iff every sketched projection is exactly N(0, 1). The classical test
statistic multiplies by N; the loss and the reported diagnostic use the per-sample form above so
the number is comparable across batch and validation sizes (a constant z scores about 0.40 with
the defaults, N(0, 0.25·I) about 0.15, N(0, I) samples ~1/N). Padded positions get weight 0.

Preset collapse bounds (G1 criterion 4, fixed before any real-data run): effective rank >= 8
(= d/8) and SIGReg <= 0.10 (a quarter of the fully collapsed value).
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np

from .features import N_ACT


# ---- numpy reference math (always available) -----------------------------------------------------

def _t_grid(knots: int = 17, t_max: float = 3.0) -> np.ndarray:
    return np.linspace(0.0, t_max, knots)


def _trapz_weights(t: np.ndarray) -> np.ndarray:
    """Trapezoid weights on grid ``t`` times the Gaussian window, doubled for t < 0."""
    dt = np.diff(t)
    w = np.zeros_like(t)
    w[:-1] += dt / 2
    w[1:] += dt / 2
    return 2.0 * w * np.exp(-t ** 2 / 2)


def random_directions(rng: np.random.Generator, d: int, m: int) -> np.ndarray:
    """``(d, m)`` float32 matrix of unit columns (uniform on the sphere)."""
    u = rng.standard_normal((d, m))
    return (u / np.linalg.norm(u, axis=0, keepdims=True)).astype(np.float32)


def sigreg_np(z: np.ndarray, dirs: np.ndarray | None = None, weights: np.ndarray | None = None,
              knots: int = 17, t_max: float = 3.0, n_dirs: int = 256, seed: int = 0) -> float:
    """SIGReg (Epps–Pulley over random projections), numpy reference. ``z (N, d)``."""
    z = np.asarray(z, dtype=np.float64)
    if z.ndim != 2 or z.shape[0] == 0:
        return float("nan")
    if dirs is None:
        dirs = random_directions(np.random.default_rng(seed), z.shape[1], n_dirs)
    w = np.ones(z.shape[0]) if weights is None else np.asarray(weights, np.float64)
    w = w / max(w.sum(), 1e-12)
    t = _t_grid(knots, t_max)
    proj = z @ np.asarray(dirs, np.float64)                     # (N, M)
    tx = proj[:, :, None] * t[None, None, :]                    # (N, M, T)
    c = np.einsum("n,nmt->mt", w, np.cos(tx))
    s = np.einsum("n,nmt->mt", w, np.sin(tx))
    err = (c - np.exp(-t ** 2 / 2)[None, :]) ** 2 + s ** 2
    return float((err @ _trapz_weights(t)).mean())


def effective_rank(z: np.ndarray) -> float:
    """Roy & Vetterli (2007) effective rank: exp(entropy of the normalised singular values) of the
    centred ``z (N, d)``. 1 for a rank-1 (collapsed) code, d for an isotropic one."""
    z = np.asarray(z, dtype=np.float64)
    if z.ndim != 2 or z.shape[0] < 2:
        return float("nan")
    s = np.linalg.svd(z - z.mean(0, keepdims=True), compute_uv=False)
    if s.sum() <= 1e-12:
        return 1.0                                   # all rows identical: fully collapsed
    p = s / s.sum()
    p = p[p > 0]
    return float(np.exp(-(p * np.log(p)).sum()))


def mean_pairwise_cosine(z: np.ndarray, max_rows: int = 2000, seed: int = 0) -> float:
    """Mean cosine similarity over distinct pairs of rows (subsampled to ``max_rows``). Near 1
    means every state maps to the same direction (collapse); near 0 for a spread code."""
    z = np.asarray(z, dtype=np.float64)
    if z.shape[0] > max_rows:
        z = z[np.random.default_rng(seed).choice(z.shape[0], max_rows, replace=False)]
    n = z.shape[0]
    if n < 2:
        return float("nan")
    u = z / np.maximum(np.linalg.norm(z, axis=1, keepdims=True), 1e-12)
    g = u @ u.T
    return float((g.sum() - np.trace(g)) / (n * (n - 1)))


def collapse_diagnostics(z: np.ndarray, erank_min: float = 8.0, sigreg_max: float = 0.10,
                         seed: int = 0) -> dict:
    """Collapse readout for one checkpoint: effective rank, SIGReg statistic, mean pairwise
    cosine, and ``within_bounds`` against the preset bounds (G1 criterion 4)."""
    er = effective_rank(z)
    sg = sigreg_np(z, seed=seed)
    cos = mean_pairwise_cosine(z, seed=seed)
    ok = bool(np.isfinite(er) and np.isfinite(sg) and er >= erank_min and sg <= sigreg_max)
    return {"effective_rank": er, "sigreg": sg, "mean_cosine": cos, "n": int(np.asarray(z).shape[0]),
            "erank_min": erank_min, "sigreg_max": sigreg_max, "within_bounds": ok}


def vicreg_np(z: np.ndarray, gamma: float = 1.0, eps: float = 1e-4) -> float:
    """VICReg-style variance hinge + off-diagonal covariance (Bardes, Ponce & LeCun, 2022),
    numpy reference of the ablation regulariser."""
    z = np.asarray(z, np.float64)
    zc = z - z.mean(0, keepdims=True)
    std = np.sqrt(zc.var(0) + eps)
    var = np.maximum(0.0, gamma - std).mean()
    cov = zc.T @ zc / max(z.shape[0] - 1, 1)
    off = cov - np.diag(np.diag(cov))
    return float(var + (off ** 2).sum() / z.shape[1])


# ---- MLX model -----------------------------------------------------------------------------------

def mlx_available() -> bool:
    try:
        import mlx.core  # noqa: F401
    except Exception:
        return False
    return True


@lru_cache(maxsize=1)
def _mlx_classes():
    import mlx.core as mx
    import mlx.nn as nn

    class Predictor(nn.Module):
        def __init__(self, d: int, act_dim: int, hidden: int):
            super().__init__()
            self.act_emb = nn.Embedding(N_ACT, act_dim)
            self.l1 = nn.Linear(d + act_dim, hidden)
            self.l2 = nn.Linear(hidden, hidden)
            self.l3 = nn.Linear(hidden, d)

        def __call__(self, z, a):
            h = mx.concatenate([z, self.act_emb(a)], axis=-1)
            h = nn.gelu(self.l1(h))
            h = nn.gelu(self.l2(h))
            return z + self.l3(h)

    class JEPA(nn.Module):
        def __init__(self, n_feat: int, cfg):
            super().__init__()
            self.d, self.d_control = cfg.d, cfg.d_control
            self.lookback = cfg.lookback
            self.in_proj = nn.Linear(n_feat * (1 + cfg.lookback), cfg.hidden)
            self.pos = nn.Embedding(cfg.L, cfg.hidden)
            self.encoder = nn.TransformerEncoder(cfg.layers, cfg.hidden, cfg.heads,
                                                 mlp_dims=4 * cfg.hidden, dropout=0.0,
                                                 norm_first=True)
            self.to_z = nn.Linear(cfg.hidden, cfg.d)
            self.predictor = Predictor(cfg.d, cfg.act_dim, cfg.pred_hidden)
            self.next_head = nn.Linear(cfg.d, N_ACT)
            self.value_l1 = nn.Linear(cfg.d_control, 32)
            self.value_l2 = nn.Linear(32, 1)

        def encode(self, x):
            B, L, Fd = x.shape
            # causal "conv" stem: step t also sees steps t-1 .. t-lookback directly (zeros
            # before the window start), so short-range order is available without attention
            # having to learn it first; the transformer carries the longer range.
            cols = [x]
            for j in range(1, self.lookback + 1):
                pad = mx.zeros((B, min(j, L), Fd), dtype=x.dtype)
                cols.append(mx.concatenate([pad, x[:, :L - j]], axis=1) if j < L else pad)
            h = self.in_proj(mx.concatenate(cols, axis=-1)) + self.pos(mx.arange(L))
            mask = nn.MultiHeadAttention.create_additive_causal_mask(L).astype(h.dtype)
            return self.to_z(self.encoder(h, mask))

        def z_control(self, z):
            return z[..., self.d - self.d_control:]

        def value_logit(self, z):
            return self.value_l2(nn.gelu(self.value_l1(self.z_control(z))))[..., 0]

        def __call__(self, x):
            z = self.encode(x)
            return z, self.next_head(z), self.value_logit(z)

    return Predictor, JEPA


def build_model(n_feat: int, cfg):
    """Instantiate the JEPA (imports mlx). Seed ``mx.random`` before calling for a fixed init."""
    _, JEPA = _mlx_classes()
    return JEPA(n_feat, cfg)


def count_params(model) -> int:
    from mlx.utils import tree_flatten
    return int(sum(v.size for _, v in tree_flatten(model.parameters())))


def sigreg_mx(z, w, dirs, knots: int = 17, t_max: float = 3.0):
    """SIGReg in MLX (same math as ``sigreg_np``). ``z (N, d)``, ``w (N,)`` non-negative weights,
    ``dirs (d, M)`` unit columns."""
    import mlx.core as mx
    t_np = _t_grid(knots, t_max)
    t = mx.array(t_np.astype(np.float32))
    tw = mx.array(_trapz_weights(t_np).astype(np.float32))
    w = w / mx.maximum(w.sum(), 1e-6)
    proj = z @ dirs                                             # (N, M)
    tx = proj[:, :, None] * t[None, None, :]                    # (N, M, T)
    c = (w[:, None, None] * mx.cos(tx)).sum(0)
    s = (w[:, None, None] * mx.sin(tx)).sum(0)
    err = (c - mx.exp(-(t ** 2) / 2)[None, :]) ** 2 + s ** 2
    return (err @ tw).mean()


def vicreg_mx(z, w, gamma: float = 1.0, eps: float = 1e-4):
    """VICReg-style variance + covariance ablation in MLX, weighted by ``w (N,)``."""
    import mlx.core as mx
    wn = w / mx.maximum(w.sum(), 1e-6)
    mu = (wn[:, None] * z).sum(0, keepdims=True)
    zc = z - mu
    cov = (wn[:, None] * zc).T @ zc
    std = mx.sqrt(mx.diag(cov) + eps)
    var = mx.maximum(0.0, gamma - std).mean()
    off = cov * (1.0 - mx.eye(z.shape[1]))
    return var + (off ** 2).sum() / z.shape[1]


def jepa_losses(model, x, mask, act, y_next, outcome, has_label, dirs, cfg):
    """All loss terms for one batch. Returns ``(total, parts)``; ``parts`` holds the unweighted
    terms (``pred``, ``pred_k1..K``, ``reg``, ``next_ce``, ``value_bce``).

    Shapes: ``x (B, L, F)``, ``mask/act/y_next (B, L)``, ``outcome/has_label (B,)``,
    ``dirs (d, M)``. Padded positions carry ``mask = 0`` and are excluded from every term.
    """
    import mlx.core as mx
    import mlx.nn as nn

    z, logits, vlogit = model(x)
    B, L, d = z.shape
    parts = {}

    # k-step latent prediction against stop-gradient targets (no EMA teacher).
    zt = mx.stop_gradient(z)
    zh = z
    pred_terms = []
    for k in range(1, cfg.k + 1):
        if k >= L:
            break
        zh = model.predictor(zh[:, :-1], act[:, k:])
        mk = mask[:, :-k] * mask[:, k:]
        se = ((zh - zt[:, k:]) ** 2).mean(-1)
        lk = (se * mk).sum() / mx.maximum(mk.sum(), 1.0)
        parts[f"pred_k{k}"] = lk
        pred_terms.append(lk)
    pred = sum(pred_terms) / max(len(pred_terms), 1)
    parts["pred"] = pred

    zf = z.reshape(B * L, d)
    wf = mask.reshape(B * L)
    reg = (sigreg_mx(zf, wf, dirs, cfg.sigreg_knots, cfg.sigreg_tmax)
           if cfg.regularizer == "sigreg" else vicreg_mx(zf, wf))
    parts["reg"] = reg

    ymask = (y_next >= 0).astype(z.dtype) * mask
    ce = nn.losses.cross_entropy(logits, mx.maximum(y_next, 0), reduction="none")
    next_ce = (ce * ymask).sum() / mx.maximum(ymask.sum(), 1.0)
    parts["next_ce"] = next_ce

    vmask = mask * has_label[:, None]
    tgt = mx.broadcast_to(outcome[:, None], vlogit.shape)
    bce = nn.losses.binary_cross_entropy(vlogit, tgt, with_logits=True, reduction="none")
    value_bce = (bce * vmask).sum() / mx.maximum(vmask.sum(), 1.0)
    parts["value_bce"] = value_bce

    total = (cfg.w_pred * pred + cfg.w_reg * reg + cfg.w_next * next_ce
             + cfg.w_value * value_bce)
    return total, parts
