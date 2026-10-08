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

Warm-up recipe options (loop 2, ``docs/research/2026-10-08-p6-c4-warmup-recipe.md``; all off by
default, so attempts 1-2 replay bit-exactly on CPU). They change the MODEL / schedule so the latent
starts near the target distribution; the bounds themselves are untouched:

- ``z_norm="layernorm"``: a final LayerNorm on z WITHOUT affine parameters (a learned scale
  would reopen the shrink-z path the latent-prediction MSE rewards); every row has mean 0 and
  variance 1 across its d coordinates.
- ``z_norm="whiten"``: ZCA whitening of z, ``(z - m) @ C^{-1/2}``, ``C^{-1/2}`` by
  ``WHITEN_ITERS`` (5) Newton-Schulz steps (IterNorm; Huang et al., 2019, "Iterative
  Normalization: Beyond Standardization towards Efficient Whitening", CVPR — recalled, not
  re-read). Batch-norm semantics (``z_norm_stats="batch"``, default): training normalises with
  the batch statistics of the REAL positions (padded rows are all-zero inputs and are excluded)
  with gradients through them; evaluation uses buffers that ``train`` sets to the exact
  train-set statistics before every checkpoint (``refresh_z_stats``), so a val/test checkpoint
  is a fixed affine map of the encoder output, estimated on train only. Few NS steps whiten
  tiny-variance directions only partially, so a rank-deficient z does not get noise amplified
  to unit scale. No affine parameters.
- ``z_norm="center"``: the same machinery with centring plus ONE global scale,
  ``(z - m) * sqrt(d / tr C)`` — total variance d, the shape of z left to the encoder and SIGReg.
  Added after diagnosing the real-data runs: the control's out-of-bounds SIGReg is mostly mean
  offset (val SIGReg 0.19 raw vs 0.05 centred), while 5-step ZCA leaves ~12 unit directions
  and ~52 near-zero ones, which SIGReg reads as a too-small scale. Caveat (recorded per run as
  ``recipe.pre_norm_collapse``): the encoder output UNDER the map stays far outside the SIGReg
  bound (~0.8); the effective rank is the same either way (it is computed on centred z and is
  scale-free), so the map moves SIGReg only.
- ``rank_floor_init``: ``True`` / ``"orthogonal"`` — the z projection (``to_z``) starts with
  orthonormal rows (numpy QR of a Gaussian matrix, seeded by ``cfg.seed``), bias 0
  (``orthogonal_rows``). ``"centered"`` — the same rows, then a data-dependent init on TRAIN
  windows (``center_z_projection``; LSUV-style, Mishkin & Matas 2016, recalled): bias = minus
  the mean of the initial z and one global scale so the total variance is d. Diagnosed on the
  real data: the default init's z is nearly the same vector for every step (val SIGReg 0.39,
  mean cosine 0.61 — the "constant z" value), and orthogonal rows alone do not change that
  (0.43); centring + scaling them gives 0.03 with effective rank 47 — inside the bounds before
  the first update. It is an initialisation only: nothing constrains z afterwards.
"""
from __future__ import annotations

import math
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


Z_NORMS = (None, "layernorm", "whiten", "center")
Z_NORM_STATS = ("batch", "running", "renorm")
WHITEN_ITERS = 5
WHITEN_MOMENTUM = 0.1
WHITEN_EPS = 1e-5


RANK_FLOOR_INITS = (False, True, "orthogonal", "centered")


def check_rank_floor_init(v) -> None:
    if not any(v is x or (isinstance(v, str) and v == x) for x in RANK_FLOOR_INITS):
        raise ValueError(f"rank_floor_init must be one of {RANK_FLOOR_INITS}, not {v!r}")


def check_z_norm(z_norm) -> None:
    if z_norm not in Z_NORMS:
        raise ValueError(f"z_norm must be one of {Z_NORMS}, not {z_norm!r}")


def orthogonal_rows(rows: int, cols: int, seed: int = 0, gain: float = 1.0) -> np.ndarray:
    """``(rows, cols)`` float32 with orthonormal rows (rows <= cols) or orthonormal columns
    (rows > cols): Q of a QR of a seeded Gaussian matrix, sign-fixed so it is uniform (Haar)."""
    rng = np.random.default_rng(seed)
    a = rng.standard_normal((max(rows, cols), min(rows, cols)))
    q, r = np.linalg.qr(a)
    q = q * np.sign(np.diag(r))[None, :]
    w = q.T if rows <= cols else q
    return (gain * w).astype(np.float32)


def inv_sqrt_ns(cov, iters: int = WHITEN_ITERS, eps: float = WHITEN_EPS):
    """``cov^{-1/2}`` (symmetric PSD, mlx) by ``iters`` Newton-Schulz steps on the trace-normalised
    matrix (IterNorm). Few steps whiten the large-variance directions fully and the tiny ones only
    partially, so a rank-deficient z does not get its noise blown up to unit scale."""
    import mlx.core as mx
    d = cov.shape[-1]
    c = cov + eps * mx.eye(d)
    tr = mx.trace(c)
    cn = c / tr
    p = mx.eye(d)
    for _ in range(iters):
        p = 0.5 * (3.0 * p - p @ p @ p @ cn)
    return p / mx.sqrt(tr)


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

    class ZWhiten(nn.Module):
        """Normalisation of ``z (B, L, d)`` with statistics of the REAL positions.
        ``mode="zca"``: ``(z - mean) @ C^{-1/2}``; ``mode="center"``: ``(z - mean) * sqrt(d / tr C)``.
        Every training batch updates running buffers (momentum, stop-gradient); evaluation uses
        the buffers (``train`` overwrites them with exact train-set statistics before every
        checkpoint, ``refresh_z_stats``), so a val/test checkpoint is a fixed affine map. In
        training the map uses ``stats="batch"`` — the batch's own statistics, gradients through
        them (batch-norm) — ``"running"`` — the buffers, stop-gradient (the evaluation map) — or
        ``"renorm"`` — the buffers' value with the batch map's gradient (batch renorm)."""

        def __init__(self, d: int, mode: str = "zca", stats: str = "batch",
                     iters: int = WHITEN_ITERS, momentum: float = WHITEN_MOMENTUM,
                     eps: float = WHITEN_EPS):
            super().__init__()
            if mode not in ("zca", "center"):
                raise ValueError(f"ZWhiten mode must be 'zca' or 'center', not {mode!r}")
            if stats not in Z_NORM_STATS:
                raise ValueError(f"ZWhiten stats must be one of {Z_NORM_STATS}, not {stats!r}")
            self.mode, self.stats, self.iters = mode, stats, iters
            self.momentum, self.eps = momentum, eps
            self.running_mean = mx.zeros((d,))
            self.running_sq = mx.eye(d)                  # E[z z^T]; with mean 0 -> cov = I
            self.freeze(keys=["running_mean", "running_sq"], recurse=False)

        def transform(self, mean, cov):
            """``(shift, matrix-or-scalar)`` for the given mean and covariance."""
            d = mean.shape[-1]
            if self.mode == "center":
                return mean, mx.sqrt(d / mx.maximum(mx.trace(cov), self.eps))
            return mean, inv_sqrt_ns(cov, self.iters, self.eps)

        def running_transform(self):
            m = self.running_mean
            return self.transform(m, self.running_sq - m[:, None] * m[None, :])

        def set_stats(self, mean, sq):
            """Replace the running buffers (``refresh_z_stats``: exact train-set statistics)."""
            self.running_mean = mx.array(np.asarray(mean, np.float32))
            self.running_sq = mx.array(np.asarray(sq, np.float32))

        def __call__(self, z, real):
            B, L, d = z.shape
            zf = z.reshape(B * L, d)
            if self.training:
                w = mx.stop_gradient(real.reshape(B * L).astype(z.dtype))
                n = mx.maximum(w.sum(), 1.0)
                mu = (w[:, None] * zf).sum(0) / n
                sq = (w[:, None] * zf).T @ zf / n
                k = self.momentum
                self.running_mean = (1 - k) * self.running_mean + k * mx.stop_gradient(mu)
                self.running_sq = (1 - k) * self.running_sq + k * mx.stop_gradient(sq)
            if self.training and self.stats in ("batch", "renorm"):
                shift, a = self.transform(mu, sq - mu[:, None] * mu[None, :])
                if self.stats == "renorm":
                    # batch renormalisation (Ioffe, 2017, recalled): the forward VALUE is the
                    # running map (no batch-composition noise), the gradient the batch map's
                    r_shift, r_a = self.running_transform()
                    shift = shift + mx.stop_gradient(r_shift - shift)
                    a = a + mx.stop_gradient(r_a - a)
            else:
                shift, a = (mx.stop_gradient(t) for t in self.running_transform())
            zc = zf - shift
            return (zc * a if self.mode == "center" else zc @ a).reshape(B, L, d)

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
            self.z_norm = getattr(cfg, "z_norm", None)
            check_z_norm(self.z_norm)
            if self.z_norm == "layernorm":
                self.z_ln = nn.LayerNorm(cfg.d, affine=False)
            elif self.z_norm in ("whiten", "center"):
                self.z_wh = ZWhiten(cfg.d, mode="zca" if self.z_norm == "whiten" else "center",
                                    stats=getattr(cfg, "z_norm_stats", "batch"))

        def encode(self, x):
            z = self.encode_pre(x)
            if self.z_norm == "layernorm":
                return self.z_ln(z)
            if self.z_norm in ("whiten", "center"):
                # padded positions are all-zero inputs (a real step always has its action one-hot)
                return self.z_wh(z, mx.abs(x).sum(-1) > 0)
            return z

        def encode_pre(self, x):
            """The encoder output before the optional ``z_norm`` (the latent itself without it)."""
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
    """Instantiate the JEPA (imports mlx). Seed ``mx.random`` before calling for a fixed init.
    With ``cfg.rank_floor_init`` the z projection is re-initialised with orthonormal rows (from a
    numpy generator seeded by ``cfg.seed``, so ``mx.random`` draws are unchanged)."""
    _, JEPA = _mlx_classes()
    model = JEPA(n_feat, cfg)
    rfi = getattr(cfg, "rank_floor_init", False)
    check_rank_floor_init(rfi)
    if rfi is not False:
        import mlx.core as mx
        w = orthogonal_rows(cfg.d, cfg.hidden, seed=10_007 + int(cfg.seed))
        model.to_z.weight = mx.array(w)
        model.to_z.bias = mx.zeros((cfg.d,))
    return model


def center_z_projection(model, X: np.ndarray, real: np.ndarray, batch: int = 256) -> dict:
    """Data-dependent part of ``rank_floor_init="centered"``: with ``X (W, L, F)`` training windows
    and ``real (W, L)`` their real positions, rescale ``to_z`` so the encoder output over those
    positions has mean 0 and total variance d: ``W <- s W``, ``b <- -s m`` (m, the mean of
    ``W h``; s = sqrt(d / tr cov)). Returns ``{"mean_norm", "scale", "n"}``."""
    import mlx.core as mx
    zs = []
    for b in range(0, len(X), batch):
        z = model.encode_pre(mx.array(X[b:b + batch]))
        mx.eval(z)
        zs.append(np.array(z)[np.asarray(real[b:b + batch], bool)])
    z = np.concatenate(zs).astype(np.float64) if zs else np.zeros((0, model.d))
    if len(z) < 2:
        return {"mean_norm": float("nan"), "scale": 1.0, "n": int(len(z))}
    w0, b0 = np.array(model.to_z.weight, np.float64), np.array(model.to_z.bias, np.float64)
    m = z.mean(0) - b0                               # mean of W h (the bias-free part)
    tr = float(np.trace(np.cov(z.T)))
    s = math.sqrt(z.shape[1] / tr) if tr > 1e-12 else 1.0
    model.to_z.weight = mx.array((s * w0).astype(np.float32))
    model.to_z.bias = mx.array((-s * m).astype(np.float32))
    mx.eval(model.parameters())
    return {"mean_norm": float(np.linalg.norm(z.mean(0))), "scale": s, "n": int(len(z))}


def refresh_z_stats(model, X: np.ndarray, real: np.ndarray, batch: int = 256) -> bool:
    """For ``z_norm`` "whiten" / "center": set the running buffers to the EXACT statistics of the
    pre-norm z over ``X (W, L, F)`` (training windows) at ``real (W, L)`` positions, with the
    current weights ("precise BN"; Wu & Johnson, 2021, "Rethinking 'Batch' in BatchNorm",
    recalled). Called before every evaluation checkpoint: in the first epochs the encoder moves
    faster than a momentum buffer can follow (measured: the running mean lagged the true one by
    ~3 in norm through epoch 0), and a stale buffer is itself an offset SIGReg reads. Returns
    False (no-op) for a model without such a layer."""
    import mlx.core as mx
    wh = getattr(model, "z_wh", None)
    if wh is None:
        return False
    n, s1, s2 = 0, None, None
    for b in range(0, len(X), batch):
        z = model.encode_pre(mx.array(X[b:b + batch]))
        mx.eval(z)
        z = np.array(z)[np.asarray(real[b:b + batch], bool)].astype(np.float64)
        s1 = z.sum(0) if s1 is None else s1 + z.sum(0)
        s2 = z.T @ z if s2 is None else s2 + z.T @ z
        n += len(z)
    if n == 0:
        return False
    wh.set_stats(s1 / n, s2 / n)
    mx.eval(wh.running_mean, wh.running_sq)
    return True


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


def jepa_losses(model, x, mask, act, y_next, outcome, has_label, dirs, cfg, w_reg=None):
    """All loss terms for one batch. Returns ``(total, parts)``; ``parts`` holds the unweighted
    terms (``pred``, ``pred_k1..K``, ``reg``, ``next_ce``, ``value_bce``). ``w_reg`` overrides
    ``cfg.w_reg`` (the per-epoch value of an annealed schedule).

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

    wr = cfg.w_reg if w_reg is None else w_reg
    total = (cfg.w_pred * pred + wr * reg + cfg.w_next * next_ce
             + cfg.w_value * value_bce)
    return total, parts
