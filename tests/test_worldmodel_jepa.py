"""P6 E3: features / windows / macro-steps, SIGReg + collapse diagnostics, probes, and (with mlx)
loss math, a synthetic training smoke, the predict API and seed determinism."""
from __future__ import annotations

import json
import os
import time

import numpy as np
import pytest

from apex_router.worldmodel import features as F
from apex_router.worldmodel import jepa as J
from apex_router.worldmodel import train as T
from apex_router.worldmodel import embed as E

HAS_MLX = J.mlx_available()
needs_mlx = pytest.mark.skipif(not HAS_MLX, reason="mlx not installed ([worldmodel] extra)")


def _step(i, act="read", **kw):
    r = {"sid": "s", "agent": None, "src": "pi", "task": "t1", "i": i, "ts": 1.0 + i, "act": act,
         "tool": "x", "err": 0, "tests": {"ran": 0, "failed": None, "passed": None}, "out_b": 0,
         "in_b": 0, "dt": 1.0, "phase": "explore", "model": None, "spawn": 0}
    r.update(kw)
    return r


# ---- features ------------------------------------------------------------------------------------

def test_feature_vector_layout():
    steps = [_step(0, "read", dt=None), _step(1, "edit", phase="edit", out_b=2, dt=30.0),
             _step(2, "test", phase="verify", err=1, dt=400.0,
                   tests={"ran": 1, "failed": 3, "passed": 9})]
    X = F.task_features(steps)
    assert X.shape == (3, F.N_FEAT) == (3, len(F.FEATURE_NAMES))
    names = F.FEATURE_NAMES
    col = {n: i for i, n in enumerate(names)}
    assert X[0, col["act=read"]] == 1 and X[1, col["act=edit"]] == 1 and X[2, col["act=test"]] == 1
    # one-hot blocks sum to 1 per row
    groups = {"act": [f"act={a}" for a in F.ACTIONS], "phase": [f"phase={p}" for p in F.PHASES],
              "in_b": [f"in_b={b}" for b in range(4)], "out_b": [f"out_b={b}" for b in range(4)],
              "dt": [n for n in names if n.startswith("dt")],
              "i": ["i=0", "i=1", "i<4", "i<8", "i<16", "i<32", "i>=32"],
              "run": ["run=1", "run=2", "run<5", "run>=5"]}
    for g, cols in groups.items():
        assert np.allclose(X[:, [col[c] for c in cols]].sum(1), 1.0), g
    assert X[0, col["dt=unknown"]] == 1 and X[1, col["dt<60s"]] == 1 and X[2, col["dt>=300s"]] == 1
    assert X[2, col["err"]] == 1 and X[2, col["tests.ran"]] == 1 and X[2, col["tests.any_failing"]] == 1
    assert X[2, col["tests.failing_share"]] == pytest.approx(0.25)
    # task-so-far counts are inclusive and normalised by position
    assert X[2, col["so_far=read"]] == pytest.approx(1 / 3)
    assert X[1, col["so_far=edit"]] == pytest.approx(1 / 2)
    assert X[2, col["i<4"]] == 1
    assert list(F.next_action_targets(steps)) == [F.ACT_INDEX["edit"], F.ACT_INDEX["test"], -1]


def test_features_fail_open_on_malformed_fields():
    bad = {"task": "t", "i": "x", "act": "bogus", "phase": None, "in_b": "big", "out_b": 99,
           "dt": "nan", "err": None, "tests": "oops", "spawn": "yes"}
    X = F.task_features([bad])
    assert X.shape == (1, F.N_FEAT) and np.isfinite(X).all()
    assert F.act_index(bad) == F.ACT_INDEX["other"] and F.phase_index(bad) == F.PHASE_INDEX["other"]
    assert F.fail_bucket(bad) == 0
    assert [F.fail_bucket({"tests": {"ran": 1, "failed": f}}) for f in (0, 1, 4, 9)] == [1, 2, 3, 4]


def test_windows_cut_pad_and_mask():
    steps = [_step(i, F.ACTIONS[i % 5]) for i in range(70)]
    W = F.build_windows({"t1": steps[::-1], "t2": steps[:3]}, {"t1": "success", "t2": "unknown"}, L=32)
    assert len(W) == 4 and W.X.shape == (4, 32, F.N_FEAT)
    by_task = {}
    for w in range(len(W)):
        by_task.setdefault(W.task_ids[W.task_idx[w]], []).append(int(W.mask[w].sum()))
    assert by_task == {"t1": [32, 32, 6], "t2": [3]}
    w_last = [w for w in range(len(W)) if W.task_ids[W.task_idx[w]] == "t1" and W.start[w] == 64][0]
    # sorted by i despite reversed input; last real step has no target; padding is -1
    assert W.act[w_last, 0] == F.ACT_INDEX[F.ACTIONS[64 % 5]]
    assert W.y_next[w_last, 5] == -1 and (W.y_next[w_last, 6:] == -1).all()
    assert W.y_next[0, 31] >= 0          # a window boundary is not a task end
    t1 = [w for w in range(len(W)) if W.task_ids[W.task_idx[w]] == "t1"]
    t2 = [w for w in range(len(W)) if W.task_ids[W.task_idx[w]] == "t2"]
    assert all(W.has_label[w] == 1 and W.outcome[w] == 1 for w in t1)
    assert all(W.has_label[w] == 0 for w in t2)
    assert (W.X[W.mask == 0] == 0).all()


def test_windows_extra_vector_appended():
    W = F.build_windows({"t1": [_step(0), _step(1)]}, L=4, extra={"t1": np.arange(3, dtype=np.float32)})
    assert W.X.shape == (1, 4, F.N_FEAT + 3)
    assert np.allclose(W.X[0, :2, -3:], [[0, 1, 2], [0, 1, 2]]) and (W.X[0, 2:] == 0).all()


def test_macro_steps_run_length_and_cap():
    acts = ["read", "read", "search", "edit", "edit", "edit", "test", "edit", "test", "vcs"]
    steps = [_step(i, a, err=int(i == 4), dt=1.0) for i, a in enumerate(acts)]
    raw = F.macro_steps(steps, max_macro=None)
    assert [m["act"] for m in raw] == ["read", "search", "edit", "test", "edit", "test", "vcs"]
    assert [m["_run"] for m in raw] == [2, 1, 3, 1, 1, 1, 1]
    assert raw[2]["err"] == 1 and raw[2]["dt"] == pytest.approx(3.0)
    capped = F.macro_steps(steps, max_macro=4)
    assert 3 <= len(capped) <= 4
    assert sum(m["_run"] for m in capped) == len(steps)
    # consecutive same-class steps never survive an uncapped merge
    assert all(a["act"] != b["act"] for a, b in zip(raw, raw[1:]))
    # run-length feature reflects the merge
    X = F.task_features(raw)
    assert X[2, F.FEATURE_NAMES.index("run<5")] == 1 and X[1, F.FEATURE_NAMES.index("run=1")] == 1
    W = F.build_windows({"t1": steps}, L=8, macro=True, max_macro=6)
    assert int(W.mask.sum()) <= 6


def test_unigram_ce():
    y = np.array([0, 0, 1, -1])
    ce = F.unigram_ce(y, y, alpha=0.0)
    assert ce == pytest.approx(-(2 / 3 * np.log(2 / 3) + 1 / 3 * np.log(1 / 3)))


# ---- collapse diagnostics / SIGReg (numpy) -------------------------------------------------------

def test_collapse_diagnostics_collapsed_vs_healthy():
    rng = np.random.default_rng(0)
    healthy = rng.standard_normal((1500, 64))
    collapsed = np.outer(rng.standard_normal(1500), rng.standard_normal(64)) + 5.0
    h = J.collapse_diagnostics(healthy)
    c = J.collapse_diagnostics(collapsed)
    assert h["within_bounds"] and not c["within_bounds"]
    assert h["effective_rank"] > 50 and c["effective_rank"] < 2
    assert h["sigreg"] < 0.01 < 0.1 < c["sigreg"]
    assert abs(h["mean_cosine"]) < 0.05 and c["mean_cosine"] > 0.5
    assert J.effective_rank(np.zeros((10, 4))) == 1.0
    assert J.sigreg_np(np.zeros((500, 8))) == pytest.approx(0.40, abs=0.02)


def test_vicreg_np_penalises_collapse():
    rng = np.random.default_rng(1)
    assert J.vicreg_np(rng.standard_normal((2000, 16))) < 0.1 < J.vicreg_np(np.zeros((2000, 16)))


# ---- probes (numpy) ------------------------------------------------------------------------------

def test_fit_logistic_and_probe_api():
    rng = np.random.default_rng(0)
    X = rng.standard_normal((600, 5))
    y = (X[:, 0] + 0.2 * rng.standard_normal(600) > 0).astype(int)
    P = T.fit_logistic(X[:400], y[:400], 2)(X[400:])
    assert P.shape == (200, 2) and np.allclose(P.sum(1), 1)
    assert ((P[:, 1] > 0.5) == y[400:]).mean() > 0.9
    # collinear columns must not make the probe diverge
    Xc = np.hstack([X[:, :1]] * 30)
    Pc = T.fit_logistic(Xc[:400], y[:400], 2)(Xc[400:])
    assert np.isfinite(Pc).all() and ((Pc[:, 1] > 0.5) == y[400:]).mean() > 0.9

    def arrays(n, d=8):
        z = rng.standard_normal((n, d))
        yn = (z[:, 0] > 0).astype(int)
        return {"z": z, "x": rng.standard_normal((n, 4)), "y_next": yn,
                "phase": (z[:, 1] > 0).astype(int), "fail_b": np.zeros(n, int),
                "z_last": z[:50], "x_last": rng.standard_normal((50, 4)),
                "outcome_last": (z[:50, -1] > 0).astype(float)}
    out = T.linear_probes(arrays(500), arrays(200), d_control=2)
    assert set(out) == {"next_action", "phase", "fail_bucket", "outcome"}
    assert out["next_action"]["z"]["acc"] > 0.9 and out["phase"]["z"]["acc"] > 0.9
    assert out["fail_bucket"]["z"]["skipped"]          # one class only -> skipped, not a crash
    assert "brier" in out["outcome"]["z_control"]


# ---- synthetic data ------------------------------------------------------------------------------

def test_synthetic_dataset_schema_and_split():
    ds = T.synthetic_dataset(120, seed=3)
    st = ds.stats()
    assert st["tasks"] == 120 and st["tasks_train"] > st["tasks_val"] > 0 and st["tasks_test"] > 0
    rec = next(iter(ds.tasks.values()))[0]
    assert set(rec) >= {"sid", "agent", "src", "task", "i", "ts", "act", "tool", "err", "tests",
                        "out_b", "in_b", "dt", "phase", "model", "spawn"}
    assert all(r["act"] in F.ACTIONS and r["phase"] in F.PHASES
               for steps in ds.tasks.values() for r in steps)
    # split by session time: every train session starts before every val session
    first = {}
    for t, m in ds.meta.items():
        first.setdefault(m["split"], []).append(m["t0"])
    assert max(first["train"]) < min(first["val"]) < min(first["test"])
    # same seed, same data
    assert T.synthetic_dataset(120, seed=3).meta == ds.meta


# ---- embedding (no network) ----------------------------------------------------------------------

def test_embed_fail_open_and_cache(tmp_path):
    v, ok = E.task_embedding("task-a", "hello", home=tmp_path, post_fn=lambda t: 1 / 0)
    assert not ok and v.shape == (768,) and not v.any()
    v, ok = E.task_embedding("task-a", None, home=tmp_path)
    assert not ok
    vec = np.linspace(-1, 1, 768).tolist()
    v, ok = E.task_embedding("task-a", "secret request text", home=tmp_path, post_fn=lambda t: vec)
    assert ok and np.allclose(v, vec)
    files = list((tmp_path / "embed").iterdir())
    assert len(files) == 1 and "task-a" not in files[0].name
    assert np.load(files[0]).dtype == np.float16
    assert b"secret" not in files[0].read_bytes()
    assert oct(files[0].stat().st_mode & 0o777) == "0o600"
    v2, ok2 = E.task_embedding("task-a", None, home=tmp_path)          # served from cache
    assert ok2 and np.allclose(v2, vec, atol=1e-3)
    emb = E.task_embeddings(["task-a", "task-b"], home=tmp_path)
    assert emb["task-a"].shape == (769,) and emb["task-a"][-1] == 0 and emb["task-b"][-1] == 1


def test_package_import_does_not_import_mlx():
    import subprocess
    import sys
    code = ("import sys, apex_router.cli, apex_router.worldmodel.features, apex_router.worldmodel.jepa, "
            "apex_router.worldmodel.train, apex_router.worldmodel.cli; "
            "print(any(m == 'mlx' or m.startswith('mlx.') for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ}, check=True)
    assert out.stdout.strip() == "False"


# ---- MLX: loss math ------------------------------------------------------------------------------

@needs_mlx
def test_sigreg_and_vicreg_mx_match_numpy():
    import mlx.core as mx
    rng = np.random.default_rng(0)
    z = rng.standard_normal((300, 8)).astype(np.float32) * 0.7 + 0.3
    w = np.ones(300, np.float32)
    w[250:] = 0                                  # masked rows must not count
    dirs = J.random_directions(rng, 8, 16)
    got = float(J.sigreg_mx(mx.array(z), mx.array(w), mx.array(dirs)))
    assert got == pytest.approx(J.sigreg_np(z[:250], dirs=dirs), rel=1e-4, abs=1e-6)
    got_v = float(J.vicreg_mx(mx.array(z[:250]), mx.array(np.ones(250, np.float32))))
    zc = z[:250] - z[:250].mean(0)
    # vicreg_mx uses the biased (1/N) covariance; compare against that form
    cov = zc.T @ zc / 250
    std = np.sqrt(np.diag(cov) + 1e-4)
    off = cov - np.diag(np.diag(cov))
    assert got_v == pytest.approx(np.maximum(0, 1 - std).mean() + (off ** 2).sum() / 8, rel=1e-4)


def _tiny_cfg(**kw):
    base = dict(hidden=32, layers=1, heads=2, pred_hidden=32, d=16, d_control=4, L=8, k=3,
                sigreg_dirs=8, batch=4)
    base.update(kw)
    return T.TrainConfig(**base)


@needs_mlx
def test_jepa_losses_masking_and_stop_gradient():
    import mlx.core as mx
    import mlx.nn as nn
    cfg = _tiny_cfg()
    mx.random.seed(0)
    model = J.build_model(F.N_FEAT, cfg)
    steps = [_step(i, F.ACTIONS[i % 4]) for i in range(5)]
    W = F.build_windows({"t1": steps}, {"t1": "fail"}, L=8)
    dirs = mx.array(J.random_directions(np.random.default_rng(0), cfg.d, cfg.sigreg_dirs))

    def run(X):
        return J.jepa_losses(model, mx.array(X), mx.array(W.mask), mx.array(W.act),
                             mx.array(W.y_next), mx.array(W.outcome), mx.array(W.has_label), dirs, cfg)

    total, parts = run(W.X)
    assert set(parts) >= {"pred", "pred_k1", "pred_k3", "reg", "next_ce", "value_bce"}
    assert all(np.isfinite(float(v)) for v in parts.values())
    # garbage in padded positions does not change any loss (causal encoder + masks)
    Xg = W.X.copy()
    Xg[0, 5:] = 7.0
    total_g, _ = run(Xg)
    assert float(total_g) == pytest.approx(float(total), rel=1e-5)
    # no labelled task -> value term is exactly 0
    _, p2 = J.jepa_losses(model, mx.array(W.X), mx.array(W.mask), mx.array(W.act), mx.array(W.y_next),
                          mx.array(W.outcome), mx.array(np.zeros(1, np.float32)), dirs, cfg)
    assert float(p2["value_bce"]) == 0.0

    # stop-gradient: with only the latent-prediction loss, the target branch contributes nothing,
    # so the gradient equals that of a version where targets are precomputed constants.
    cfg_p = _tiny_cfg(w_reg=0.0, w_next=0.0, w_value=0.0)

    def f_jepa(m):
        return J.jepa_losses(m, mx.array(W.X), mx.array(W.mask), mx.array(W.act), mx.array(W.y_next),
                             mx.array(W.outcome), mx.array(W.has_label), dirs, cfg_p)[0]
    z_const = mx.stop_gradient(model.encode(mx.array(W.X)))

    def f_const(m):
        z = m.encode(mx.array(W.X))
        zh, mask, terms = z, mx.array(W.mask), []
        act = mx.array(W.act)
        for k in range(1, cfg_p.k + 1):
            zh = m.predictor(zh[:, :-1], act[:, k:])
            mk = mask[:, :-k] * mask[:, k:]
            terms.append((((zh - z_const[:, k:]) ** 2).mean(-1) * mk).sum() / mx.maximum(mk.sum(), 1.0))
        return sum(terms) / len(terms)
    g1 = nn.value_and_grad(model, f_jepa)(model)[1]
    g2 = nn.value_and_grad(model, f_const)(model)[1]
    a = np.array(g1["in_proj"]["weight"])
    b = np.array(g2["in_proj"]["weight"])
    assert np.allclose(a, b, atol=1e-6) and np.abs(a).sum() > 0


@needs_mlx
def test_default_config_param_budget():
    import mlx.core as mx
    mx.random.seed(0)
    cfg = T.TrainConfig()
    n = J.count_params(J.build_model(T.n_features(cfg), cfg))
    n_embed = J.count_params(J.build_model(T.n_features(T.TrainConfig(embed=True)), cfg))
    assert n <= 12_000_000 and n_embed <= 12_000_000


# ---- MLX: training smoke, collapse, predict, determinism -----------------------------------------

_SMOKE = dict(epochs=2, batch=8, warmup=10, lr=1e-3, hidden=64, layers=1, heads=2, pred_hidden=64,
              sigreg_dirs=32, patience=5)


@pytest.fixture(scope="module")
def smoke_ds():
    return T.synthetic_dataset(400, seed=0)


@pytest.fixture(scope="module")
def smoke_run(smoke_ds, tmp_path_factory):
    if not HAS_MLX:
        pytest.skip("mlx not installed")
    d = tmp_path_factory.mktemp("wm") / "run-a"
    t0 = time.perf_counter()
    s = T.train(T.TrainConfig(**_SMOKE), smoke_ds, run_dir=d, run_id="run-a", log=lambda m: None)
    return d, s, time.perf_counter() - t0


@needs_mlx
def test_training_smoke_learns_without_collapse(smoke_run):
    d, s, secs = smoke_run
    assert secs < 60
    rows = [json.loads(line) for line in (d / "metrics.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    # next-action CE falls below the unigram prior (train), and keeps falling
    assert rows[-1]["train_next_ce"] < s["train_unigram_ce"]
    assert rows[-1]["train_next_ce"] < rows[0]["train_next_ce"]
    # no collapse: effective rank above a floor at every checkpoint
    assert all(r["collapse"]["effective_rank"] >= 4.0 for r in rows)
    assert all(np.isfinite(r["collapse"]["sigreg"]) for r in rows)
    for f in ("config.json", "metrics.jsonl", "weights.safetensors", "summary.json"):
        assert (d / f).exists() and oct((d / f).stat().st_mode & 0o777) == "0o600", f
    assert oct(d.stat().st_mode & 0o777) == "0o700"
    assert s["params"] > 0 and s["throughput"]["windows_per_s"] > 0
    assert {"next_action", "phase", "fail_bucket", "outcome"} <= set(s["probes"])


@needs_mlx
def test_predict_api(smoke_run, smoke_ds):
    d, s, _ = smoke_run
    wm = T.WorldModel.load(d)
    tid = sorted(smoke_ds.tasks)[0]
    steps = smoke_ds.tasks[tid]
    p = wm.predict(steps)
    n = len(steps)
    assert p["z"].shape == (n, wm.cfg.d) and p["z_control"].shape == (n, wm.cfg.d_control)
    assert p["next_action_probs"].shape == (n, F.N_ACT)
    assert np.allclose(p["next_action_probs"].sum(1), 1, atol=1e-4)
    assert p["p_success"].shape == (n,) and ((p["p_success"] > 0) & (p["p_success"] < 1)).all()
    lp = wm.action_logprobs(steps)
    assert lp.shape == (n,) and (lp <= 0).all()
    # a long task (> L) is stitched from several windows in step order
    long = [dict(_step(i, F.ACTIONS[i % 3]), task="long") for i in range(wm.cfg.L + 7)]
    assert wm.predict(long)["z"].shape[0] == wm.cfg.L + 7
    # causal: the latent of step t does not depend on later steps
    short = wm.predict(long[:10])
    assert np.allclose(short["z"], wm.predict(long)["z"][:10], atol=1e-5)
    many = wm.score({t: smoke_ds.tasks[t] for t in sorted(smoke_ds.tasks)[:3]})
    assert len(many) == 3
    pr = T.probe_run(d, ds=smoke_ds)
    assert pr["val"]["collapse"]["n"] > 0 and "next_action" in pr["probes"]


@needs_mlx
def test_training_is_deterministic(smoke_ds, smoke_run, tmp_path):
    d, s, _ = smoke_run
    s2 = T.train(T.TrainConfig(**_SMOKE), smoke_ds, run_dir=tmp_path / "run-b", run_id="run-b",
                 log=lambda m: None)
    r1 = [json.loads(x) for x in (d / "metrics.jsonl").read_text().splitlines()]
    r2 = [json.loads(x) for x in (tmp_path / "run-b" / "metrics.jsonl").read_text().splitlines()]
    for a, b in zip(r1, r2):
        for k in ("train_total", "train_next_ce", "val_next_ce", "val_value_bce"):
            assert a[k] == pytest.approx(b[k], rel=1e-5, abs=1e-6), k
    assert s2["params"] == s["params"]


@needs_mlx
def test_vicreg_ablation_and_macro_flag_train(smoke_ds, tmp_path):
    cfg = T.TrainConfig(**{**_SMOKE, "epochs": 1, "regularizer": "vicreg", "macro": True})
    s = T.train(cfg, smoke_ds, run_dir=tmp_path / "r", run_id="r", log=lambda m: None)
    assert np.isfinite(s["best"]["val_next_ce"])
    wm = T.WorldModel.load(tmp_path / "r")
    p = wm.predict(smoke_ds.tasks[sorted(smoke_ds.tasks)[0]])
    assert len(p["steps"]) <= 6 and p["z"].shape[0] == len(p["steps"])


@needs_mlx
def test_cli_train_and_probe(tmp_path, monkeypatch, capsys):
    from apex_router.cli import main
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path))
    cfg = json.dumps({k: v for k, v in _SMOKE.items() if k != "epochs"})
    assert main(["worldmodel", "train", "--synthetic", "120", "--epochs", "1", "--config", cfg,
                 "--run-id", "cli-run"]) == 0
    out = capsys.readouterr().out
    assert "params" in out and "effective rank" in out and "probe next_action" in out
    assert (tmp_path / "worldmodel" / "runs" / "cli-run" / "summary.json").exists()
    assert main(["worldmodel", "probe", "cli-run"]) == 0
    assert "collapse" in capsys.readouterr().out
    assert main(["worldmodel", "nope"]) == 2
