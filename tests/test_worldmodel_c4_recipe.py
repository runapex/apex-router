"""P6 loop 2: the C4 warm-up recipe options (docs/research/2026-10-08-p6-c4-warmup-recipe.md) —
config round-trip and validation, the w_reg anneal schedule, LR warm-up length, orthogonal and
centred z-projection init, the z_norm layers (shapes, near-isotropy, padded rows ignored,
train-set statistics at evaluation), and that the defaults replay attempt 2 exactly."""
from __future__ import annotations

import json
import math
from dataclasses import asdict

import numpy as np
import pytest

from apex_router.worldmodel import features as F
from apex_router.worldmodel import jepa as J
from apex_router.worldmodel import train as T

HAS_MLX = J.mlx_available()
needs_mlx = pytest.mark.skipif(not HAS_MLX, reason="mlx not installed ([worldmodel] extra)")

_RECIPE_ON = dict(z_norm="center", z_norm_stats="renorm", w_reg_schedule="anneal",
                  w_reg_start=100.0, w_reg_anneal_epochs=3, rank_floor_init="centered",
                  lr_warmup_epochs=1.5)


def _tiny(**kw):
    base = dict(hidden=32, layers=1, heads=2, pred_hidden=32, d=16, d_control=4, L=8, k=3,
                sigreg_dirs=8, batch=4)
    base.update(kw)
    return T.TrainConfig(**base)


# ---- config --------------------------------------------------------------------------------------

def test_recipe_defaults_are_off_and_round_trip():
    d = T.TrainConfig()
    assert (d.z_norm, d.z_norm_stats, d.w_reg_schedule, d.rank_floor_init, d.lr_warmup_epochs) == \
        (None, "batch", "const", False, None)
    assert T.recipe_of(d)["default"] is True
    on = T.TrainConfig.from_dict({**asdict(T.TrainConfig()), **_RECIPE_ON})
    back = T.TrainConfig.from_dict(json.loads(json.dumps(asdict(on))))
    assert back == on
    r = T.recipe_of(back)
    assert r["default"] is False and {k: r[k] for k in _RECIPE_ON} == _RECIPE_ON
    for v in (True, "orthogonal", "centered", False):
        assert T.TrainConfig.from_dict({"rank_floor_init": v}).rank_floor_init == v


@pytest.mark.parametrize("bad", [{"z_norm": "batchnorm"}, {"z_norm_stats": "ema"},
                                 {"w_reg_schedule": "cosine"}, {"rank_floor_init": "pca"},
                                 {"lr_warmup_epochs": -1}, {"lr_warmup_epochs": float("nan")},
                                 {"w_reg_schedule": "anneal", "w_reg_start": 0},
                                 {"w_reg_schedule": "anneal", "w_reg_anneal_epochs": 0}])
def test_recipe_validation(bad):
    with pytest.raises(ValueError):
        T.TrainConfig.from_dict(bad)


def test_w_reg_anneal_schedule_per_epoch():
    c = T.TrainConfig(w_reg=1.0, w_reg_schedule="anneal", w_reg_start=30.0, w_reg_anneal_epochs=4)
    got = [T.w_reg_at(c, e) for e in range(7)]
    want = [30.0, 30 * (1 / 30) ** 0.25, 30 * (1 / 30) ** 0.5, 30 * (1 / 30) ** 0.75, 1.0, 1.0, 1.0]
    assert got == pytest.approx(want, rel=1e-12)
    assert got[1] == pytest.approx(12.819, abs=1e-3) and got[2] == pytest.approx(5.477, abs=1e-3)
    assert all(a > b for a, b in zip(got[:4], got[1:5]))          # strictly decreasing to w_reg
    c10 = T.TrainConfig(w_reg=10.0, w_reg_schedule="anneal", w_reg_start=30.0)
    assert [T.w_reg_at(c10, e) for e in (0, 2, 4, 9)] == pytest.approx([30.0, 30 / 3 ** 0.5, 10.0, 10.0])
    assert [T.w_reg_at(T.TrainConfig(w_reg=3.0), e) for e in range(3)] == [3.0, 3.0, 3.0]


def test_lr_warmup_steps():
    assert T.warmup_steps(T.TrainConfig(epochs=12), 34) == 40              # min(200, 408 // 10)
    assert T.warmup_steps(T.TrainConfig(epochs=20), 34) == 68
    assert T.warmup_steps(T.TrainConfig(epochs=200), 34) == 200
    assert T.warmup_steps(T.TrainConfig(lr_warmup_epochs=0), 34) == 0
    assert T.warmup_steps(T.TrainConfig(lr_warmup_epochs=2.5), 34) == 85   # no 10% cap


def test_orthogonal_rows_numpy():
    w = J.orthogonal_rows(16, 64, seed=3)
    assert w.shape == (16, 64) and w.dtype == np.float32
    assert np.allclose(w @ w.T, np.eye(16), atol=1e-5)
    assert np.linalg.matrix_rank(w) == 16
    assert J.effective_rank(w.T) > 15.0
    assert np.array_equal(w, J.orthogonal_rows(16, 64, seed=3))
    assert not np.allclose(w, J.orthogonal_rows(16, 64, seed=4))
    tall = J.orthogonal_rows(64, 16, seed=0)
    assert np.allclose(tall.T @ tall, np.eye(16), atol=1e-5)


# ---- mlx: init and z_norm layers -------------------------------------------------------------------

@needs_mlx
def test_orthogonal_init_rank_and_rng_untouched():
    import mlx.core as mx
    mx.random.seed(0)
    m0 = J.build_model(F.N_FEAT, _tiny())
    mx.random.seed(0)
    m1 = J.build_model(F.N_FEAT, _tiny(rank_floor_init=True))
    w = np.array(m1.to_z.weight)
    assert w.shape == (16, 32)
    assert np.allclose(w @ w.T, np.eye(16), atol=1e-5) and np.linalg.matrix_rank(w) == 16
    assert np.array(m1.to_z.bias).tolist() == [0.0] * 16
    s = np.linalg.svd(w, compute_uv=False)
    assert s.min() == pytest.approx(1.0, abs=1e-5)                # every direction kept
    s0 = np.linalg.svd(np.array(m0.to_z.weight), compute_uv=False)
    assert s0.max() / s0.min() > 1.2                              # the default init is not
    # only to_z changed: the rest of the init (mx.random draws) is identical
    assert np.array_equal(np.array(m0.in_proj.weight), np.array(m1.in_proj.weight))
    assert np.array_equal(np.array(m0.next_head.weight), np.array(m1.next_head.weight))


def _real_mask(B, L, rng):
    lens = rng.integers(2, L + 1, B)
    return (np.arange(L)[None, :] < lens[:, None]).astype(np.float32)


@needs_mlx
def test_center_init_puts_initial_z_in_bounds():
    import mlx.core as mx
    rng = np.random.default_rng(0)
    cfg = _tiny(rank_floor_init="centered")
    mx.random.seed(0)
    m = J.build_model(F.N_FEAT, cfg)
    real = _real_mask(64, 8, rng)
    X = (rng.uniform(size=(64, 8, F.N_FEAT)) < 0.1).astype(np.float32) * real[..., None]
    X[..., 0] = real                                           # every real step is non-zero
    info = J.center_z_projection(m, X, real)
    assert info["n"] == int(real.sum()) and info["scale"] > 0
    z = np.array(m.encode_pre(mx.array(X)))[real.astype(bool)]
    assert np.abs(z.mean(0)).max() < 1e-3
    assert np.trace(np.cov(z.T)) == pytest.approx(16.0, rel=1e-3)


@needs_mlx
@pytest.mark.parametrize("norm", ["layernorm", "whiten", "center"])
def test_z_norm_shapes_and_near_isotropy_at_init(norm):
    import mlx.core as mx
    rng = np.random.default_rng(1)
    mx.random.seed(0)
    m = J.build_model(F.N_FEAT, _tiny(z_norm=norm))
    real = _real_mask(48, 8, rng)
    X = rng.standard_normal((48, 8, F.N_FEAT)).astype(np.float32) * real[..., None]
    m.train()
    z = np.array(m.encode(mx.array(X)))
    assert z.shape == (48, 8, 16) and np.isfinite(z).all()
    zr = z[real.astype(bool)].astype(np.float64)
    zp = np.array(m.encode_pre(mx.array(X)))[real.astype(bool)].astype(np.float64)
    if norm == "layernorm":                                    # per row: mean 0, variance 1
        assert np.abs(zr.mean(1)).max() < 1e-4
        assert np.allclose(zr.var(1), 1.0, atol=1e-2)
    else:                                                      # over real rows: centred
        assert np.abs(zr.mean(0)).max() < 1e-3
    if norm == "center":
        assert np.trace(np.cov(zr.T, bias=True)) == pytest.approx(16.0, rel=1e-3)
    if norm == "whiten":                                       # partial ZCA: flatter spectrum
        ev = np.linalg.eigvalsh(np.cov(zr.T, bias=True))
        ev0 = np.linalg.eigvalsh(np.cov(zp.T, bias=True))
        assert ev.max() < 1.3 and ev.max() / ev.min() < ev0.max() / ev0.min()
    assert J.effective_rank(zr) >= J.effective_rank(zp) - 1e-6 or norm == "layernorm"
    assert J.effective_rank(zr) > 8.0                          # d/2 floor on random input


@needs_mlx
@pytest.mark.parametrize("norm", ["whiten", "center"])
def test_z_norm_ignores_padded_rows_and_evaluates_with_buffers(norm):
    import mlx.core as mx
    rng = np.random.default_rng(2)
    mx.random.seed(0)
    m = J.build_model(F.N_FEAT, _tiny(z_norm=norm))
    real = _real_mask(16, 8, rng)
    X = rng.standard_normal((16, 8, F.N_FEAT)).astype(np.float32) * real[..., None]
    m.train()
    a = np.array(m.encode(mx.array(X)))
    # padded rows are all-zero inputs; their (input-independent) z must not move the statistics
    real2 = real.copy()
    real2[0, :] = 0.0
    X2 = X * real2[..., None]
    b = np.array(m.encode(mx.array(X2)))
    assert not np.allclose(a[1:], b[1:])                       # dropping a sequence changes stats
    # buffers: refresh -> exact train statistics; eval uses them (a fixed affine map)
    assert J.refresh_z_stats(m, X, real)
    zp = np.array(m.encode_pre(mx.array(X)))[real.astype(bool)].astype(np.float64)
    assert np.allclose(np.array(m.z_wh.running_mean), zp.mean(0), atol=1e-4)
    assert np.allclose(np.array(m.z_wh.running_sq), zp.T @ zp / len(zp), atol=1e-3)
    m.eval()
    e1 = np.array(m.encode(mx.array(X)))
    e2 = np.array(m.encode(mx.array(X[:3])))                   # batch-independent in eval
    assert np.allclose(e1[:3], e2, atol=1e-5)
    if norm == "center":
        zr = e1[real.astype(bool)]
        assert np.abs(zr.mean(0)).max() < 1e-3
    assert not J.refresh_z_stats(J.build_model(F.N_FEAT, _tiny()), X, real)   # no layer: no-op


@needs_mlx
def test_jepa_losses_w_reg_override():
    import mlx.core as mx
    cfg = _tiny(w_reg=1.0)
    mx.random.seed(0)
    m = J.build_model(F.N_FEAT, cfg)
    steps = [{"sid": "s", "task": "t", "i": i, "act": F.ACTIONS[i % 4], "phase": "explore"}
             for i in range(6)]
    W = F.build_windows({"t": steps}, {"t": "success"}, L=8)
    dirs = mx.array(J.random_directions(np.random.default_rng(0), cfg.d, cfg.sigreg_dirs))
    args = (m, mx.array(W.X), mx.array(W.mask), mx.array(W.act), mx.array(W.y_next),
            mx.array(W.outcome), mx.array(W.has_label), dirs, cfg)
    t1, p1 = J.jepa_losses(*args)
    t7, _ = J.jepa_losses(*args, w_reg=7.0)
    assert float(t7) - float(t1) == pytest.approx(6.0 * float(p1["reg"]), rel=1e-4)


# ---- training: defaults replay attempt 2, recipe runs record themselves ---------------------------

# Pre-change code (f7b5162), CPU, this exact config: per-epoch values and summary keys.
_GOLDEN_ROWS = [
    {"train_total": 3.3096740885478693, "train_next_ce": 2.4346313360260754,
     "val_next_ce": 2.508674383163452, "erank": 6.524402712616655, "sigreg": 0.3104075396707229},
    {"train_total": 2.967795773250301, "train_next_ce": 2.3326622974581834,
     "val_next_ce": 2.4432780742645264, "erank": 6.487436881769272, "sigreg": 0.2286816658878863},
]
_ATTEMPT2_SUMMARY_KEYS = {
    "ablations", "best", "best_epoch", "collapse_outside_bounds_epochs", "config", "data",
    "features", "final_val", "n_features", "params", "probes", "regime", "run_id", "start_prior",
    "throughput", "train_unigram_ce", "unigram_prior", "view", "windows"}
_GOLDEN_CFG = dict(epochs=2, batch=8, warmup=10, lr=1e-3, hidden=32, layers=1, heads=2,
                   pred_hidden=32, d=16, d_control=4, L=8, k=3, sigreg_dirs=8, patience=5,
                   device="cpu")


@pytest.fixture(scope="module")
def syn120():
    return T.synthetic_dataset(120, seed=0)


@needs_mlx
def test_defaults_replay_attempt_2(syn120, tmp_path):
    s = T.train(T.TrainConfig(**_GOLDEN_CFG), syn120, run_dir=tmp_path / "g", run_id="g",
                log=lambda m: None)
    assert set(s) == _ATTEMPT2_SUMMARY_KEYS | {"recipe"}
    assert s["recipe"]["default"] is True and s["recipe"]["w_reg_per_epoch"] == [1.0, 1.0]
    assert "pre_norm_collapse" not in s["recipe"] and "centered_init" not in s["recipe"]
    rows = [json.loads(x) for x in (tmp_path / "g" / "metrics.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    for r, g in zip(rows, _GOLDEN_ROWS):
        assert r["train_total"] == pytest.approx(g["train_total"], rel=1e-6)
        assert r["train_next_ce"] == pytest.approx(g["train_next_ce"], rel=1e-6)
        assert r["val_next_ce"] == pytest.approx(g["val_next_ce"], rel=1e-6)
        assert r["collapse"]["effective_rank"] == pytest.approx(g["erank"], rel=1e-6)
        assert r["collapse"]["sigreg"] == pytest.approx(g["sigreg"], rel=1e-5)


@needs_mlx
def test_recipe_run_records_and_reloads(syn120, tmp_path):
    cfg = T.TrainConfig(**{**_GOLDEN_CFG, "epochs": 3, "z_norm": "center",
                           "w_reg_schedule": "anneal", "w_reg_start": 8.0,
                           "w_reg_anneal_epochs": 2, "rank_floor_init": "centered",
                           "lr_warmup_epochs": 0.5})
    s = T.train(cfg, syn120, run_dir=tmp_path / "r", run_id="r", log=lambda m: None)
    rec = s["recipe"]
    assert rec["default"] is False and rec["z_norm"] == "center"
    assert rec["w_reg_per_epoch"] == pytest.approx([8.0, 8.0 ** 0.5, 1.0][:len(rec["w_reg_per_epoch"])])
    nb = math.ceil(s["windows"]["train"] / cfg.batch)
    assert rec["lr_warmup_steps"] == round(0.5 * nb)
    assert rec["centered_init"]["n"] > 0
    assert rec["init_collapse"]["sigreg"] < 0.2                     # centred + scaled start
    for k in ("effective_rank", "sigreg", "within_bounds"):
        assert k in rec["pre_norm_collapse"]
    saved = json.loads((tmp_path / "r" / "summary.json").read_text())["recipe"]
    assert saved["z_norm"] == "center" and saved["rank_floor_init"] == "centered"
    wm = T.WorldModel.load(tmp_path / "r")
    assert wm.cfg == cfg and wm.model.training is False
    # the checkpoint carries the train-set statistics; inference reproduces the best epoch's val z
    steps = next(iter(syn120.split("val")[0].values()))
    p = wm.predict(steps)
    assert p["z"].shape == (len(steps), cfg.d) and np.isfinite(p["next_action_probs"]).all()
    assert np.abs(np.array(wm.model.z_wh.running_mean)).sum() > 0
