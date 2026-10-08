"""P6 loop 2, iteration 2b: `worldmodel evaluate` carries the C4 collapse recipe
(docs/research/2026-10-08-p6-c4-warmup-recipe.md) — flag parsing and merge precedence, the recipe
guard on --run, the card/ledger recipe block and the C4 wording. Synthetic data only."""
from __future__ import annotations

import json
import zlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from apex_router.worldmodel import evaluate as E
from apex_router.worldmodel import fixtures
from apex_router.worldmodel import jepa as J
from apex_router.worldmodel import protocol as P
from apex_router.worldmodel import train as T

needs_mlx = pytest.mark.skipif(not J.mlx_available(), reason="mlx not installed ([worldmodel] extra)")

A3 = {"z_norm": "center", "w_reg_schedule": "anneal", "w_reg_start": 30.0, "w_reg_anneal_epochs": 4}


def _cfg(argv):
    return E.eval_config(E.evaluate_parser().parse_args(argv))


# ---- parsing + precedence ------------------------------------------------------------------------

def test_no_flags_is_the_default_config():
    assert _cfg(["--train"]) == {}
    assert E.recipe_request({})["default"] is True
    assert T.TrainConfig.from_dict(_cfg(["--train"])) == T.TrainConfig()


def test_recipe_flags_parse():
    c = _cfg(["--train", "--z-norm", "center", "--w-reg-schedule", "anneal", "--w-reg-start", "30",
              "--w-reg-anneal-epochs", "4", "--lr-warmup-epochs", "0.5", "--rank-floor-init",
              "centered", "--features", "a2f", "--regime", "--epochs", "12"])
    assert c == {**A3, "lr_warmup_epochs": 0.5, "rank_floor_init": "centered", "features": "a2f",
                 "regime": True, "epochs": 12}
    assert _cfg(["--train", "--rank-floor-init", "off"])["rank_floor_init"] is False
    assert _cfg(["--train", "--lr-warmup-epochs", "none"])["lr_warmup_epochs"] is None
    assert _cfg(["--train", "--z-norm", "none"]) == {"z_norm": None}


def test_precedence_flag_over_config_over_default(tmp_path):
    js = json.dumps({"z_norm": "whiten", "w_reg_start": 10, "w_reg": 3, "features": "a2f",
                     "lr_warmup_epochs": 2})
    c = _cfg(["--train", "--config", js, "--z-norm", "center", "--w-reg-start", "30",
              "--lr-warmup-epochs", "none"])
    assert c["z_norm"] == "center" and c["w_reg_start"] == 30.0        # flag > config
    assert c["w_reg"] == 3 and c["features"] == "a2f"                  # config > default
    assert c["lr_warmup_epochs"] is None                               # explicit none wins too
    cfg = T.TrainConfig.from_dict(c)
    assert cfg.w_reg_schedule == "const" and cfg.w_reg_anneal_epochs == 4   # default
    # --z-norm none overrides a config's z_norm; a path works like inline JSON
    p = tmp_path / "c.json"
    p.write_text(json.dumps(A3))
    assert _cfg(["--train", "--config", str(p)]) == A3
    assert _cfg(["--train", "--config", str(p), "--z-norm", "none"])["z_norm"] is None


@pytest.mark.parametrize("argv", [
    ["--train", "--config", "{\"bogus\": 1}"],                       # unknown key
    ["--train", "--config", "[1, 2]"],                               # not an object
    ["--train", "--config", "{not json"],
    ["--train", "--config", "{\"seed\": 1}"],                        # set by --seed/--seeds
    ["--train", "--config", "{\"device\": \"cpu\"}"],
    ["--train", "--w-reg-schedule", "anneal", "--w-reg-start", "0"],  # recipe validation
    ["--train", "--lr-warmup-epochs", "soon"],
])
def test_bad_config_refused(argv, capsys):
    with pytest.raises(ValueError):
        _cfg(argv)
    assert E.cmd_evaluate(argv) == 2
    assert "apex-router worldmodel evaluate:" in capsys.readouterr().err


def test_cli_passes_the_merged_config_to_training(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path))
    monkeypatch.setattr(J, "mlx_available", lambda: True)
    seen = {}

    def fake_train_runs(ds, src, **kw):
        seen.update(kw)
        raise SystemExit(0)

    monkeypatch.setattr(E, "train_runs", fake_train_runs)
    with pytest.raises(SystemExit):
        E.cmd_evaluate(["--synthetic", "20", "--train", "--sweep", "--sweep-every-epoch",
                        "--w-reg-grid", "1,3,10", "--features", "a2f", "--regime",
                        "--config", json.dumps({"z_norm": "whiten", "w_reg_start": 10}),
                        "--z-norm", "center", "--w-reg-schedule", "anneal", "--w-reg-start", "30",
                        "--w-reg-anneal-epochs", "4", "--no-save"])
    assert seen["overrides"] == {**A3, "features": "a2f", "regime": True}
    assert seen["w_reg_grid"] == (1.0, 3.0, 10.0) and seen["sweep_every_epoch"] is True


def test_train_runs_applies_the_recipe_to_sweep_seeds_and_replay(monkeypatch, tmp_path):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path))
    trained = []

    def fake_train(cfg, ds, run_id=None, log=print, run_dir=None):
        trained.append((run_id, cfg))
        d = T.data_home() / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        s = {"run_id": run_id, "best_epoch": 0, "collapse_outside_bounds_epochs": [],
             "best": {"val_next_ce": 2.0 - cfg.w_reg / 100,
                      "collapse": {"effective_rank": 20.0, "sigreg": 0.05, "within_bounds": True}}}
        (d / "summary.json").write_text(json.dumps(s))
        return s

    monkeypatch.setattr(T, "train", fake_train)
    raw, _ = fixtures.dataset(10, seed=1)
    out = E.train_runs(E.stream_view(raw), "synthetic-fixtures:10", seeds=2, sweep=True, cpu=True,
                       overrides={**A3, "epochs": 3}, sweep_every_epoch=True,
                       w_reg_grid=(1.0, 3.0, 10.0), log=lambda m: None)
    assert len(trained) == 3 + 1 + 1                   # grid, seed 1 (seed 0 = winner), cpu replay
    for _, c in trained:
        assert (c.z_norm, c.w_reg_schedule, c.w_reg_start, c.w_reg_anneal_epochs, c.epochs) == \
            ("center", "anneal", 30.0, 4, 3)
    assert out["config"]["z_norm"] == "center" and out["config"]["w_reg"] == 10.0


# ---- the recipe guard ----------------------------------------------------------------------------

def test_run_recipe_reads_summary_then_config_then_defaults():
    assert E.run_recipe({})["default"] is True                                 # attempt 1/2 runs
    assert E.run_recipe({"config": {"z_norm": "center"}})["z_norm"] == "center"
    s = {"recipe": {**T.recipe_of(T.TrainConfig(**A3)), "w_reg_per_epoch": [30, 1]},
         "config": {"z_norm": None}}
    assert E.run_recipe(s)["z_norm"] == "center"                               # summary wins
    assert E.recipe_request({"rank_floor_init": True})["rank_floor_init"] == "orthogonal"


def test_recipe_guard_refuses_a_mismatch():
    a3 = {"recipe": T.recipe_of(T.TrainConfig(**A3))}
    assert E.check_recipe_match(a3, A3)["z_norm"] == "center"
    assert E.check_recipe_match({}, None)["default"] is True
    with pytest.raises(E.RecipeMismatch, match="--z-norm center"):
        E.check_recipe_match(a3, None)                       # A3 run scored as the default recipe
    with pytest.raises(E.RecipeMismatch, match="z_norm=none"):
        E.check_recipe_match({}, A3)                         # default run declared as A3
    with pytest.raises(E.RecipeMismatch, match="w_reg_start=30.*declares w_reg_start=10"):
        E.check_recipe_match(a3, {**A3, "w_reg_start": 10})
    with pytest.raises(E.RecipeMismatch, match="lr_warmup_epochs"):
        E.check_recipe_match(a3, {**A3, "lr_warmup_epochs": 0})
    with pytest.raises(E.ViewMismatch):                      # caught like the view guard
        E.check_recipe_match(a3, {**A3, "z_norm_stats": "renorm"})
    # inert knobs do not refuse: anneal settings under const, z_norm_stats without center/whiten
    assert E.check_recipe_match({"config": {"w_reg_start": 5.0, "z_norm_stats": "running"}}, None)


class _FakeWM:
    def __init__(self, rd, summary):
        self.run_dir = rd
        self.cfg = SimpleNamespace(macro=False, embed=False, d=8, d_control=4, erank_min=2.0,
                                   sigreg_max=10.0, seed=0, device="cpu", w_reg=10.0,
                                   features="a1", regime=False)
        self.summary = summary

    def score(self, tasks):
        out = {}
        for tid, st in tasks.items():
            n = len(st)
            z = np.random.default_rng(zlib.crc32(tid.encode())).normal(size=(n, 8))
            out[tid] = {"z": z, "z_control": z[:, -4:], "steps": st, "offline_only": False,
                        "next_action_probs": np.full((n, P.V), 1.0 / P.V),
                        "p_success": np.full(n, 0.5)}
        return out


def _run(tmp_path, monkeypatch, summary):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path))
    rd = tmp_path / "worldmodel" / "runs" / "fake-run"
    rd.mkdir(parents=True)
    (rd / "summary.json").write_text(json.dumps(summary))
    (rd / "metrics.jsonl").write_text(json.dumps(
        {"epoch": 0, "collapse": {"effective_rank": 5.0, "sigreg": 0.05, "within_bounds": True}}) + "\n")
    return rd


def _a3_summary():
    return {"best_epoch": 0, "best": {"val_next_ce": 2.0},
            "recipe": {**T.recipe_of(T.TrainConfig(**A3, w_reg=10.0)),
                       "w_reg_per_epoch": [30.0, 22.2, 16.4, 12.2, 10.0], "lr_warmup_steps": 40,
                       "pre_norm_collapse": {"effective_rank": 14.0, "sigreg": 0.8,
                                             "within_bounds": False}}}


def test_recipe_mismatch_refused_before_any_test_pass(tmp_path, monkeypatch, capsys):
    _run(tmp_path, monkeypatch, _a3_summary())
    loaded = []
    monkeypatch.setattr(T.WorldModel, "load", staticmethod(lambda r: loaded.append(r)))
    raw, _ = fixtures.dataset(40, seed=1)
    with pytest.raises(E.RecipeMismatch):
        E.scorecard(E.stream_view(raw), "synthetic: 40", ["fake-run"])           # declares default
    assert loaded == [] and not E.ledger_path().exists()
    # the CLI path: exit 2 with the flags to pass
    monkeypatch.setattr(J, "mlx_available", lambda: True)
    assert E.cmd_evaluate(["--synthetic", "40", "--run", "fake-run", "--no-save"]) == 2
    err = capsys.readouterr().err
    assert "--z-norm center" in err and "--w-reg-schedule anneal" in err and loaded == []


def test_card_and_ledger_record_the_recipe(tmp_path, monkeypatch):
    rd = _run(tmp_path, monkeypatch, _a3_summary())
    wm = _FakeWM(rd, _a3_summary())
    monkeypatch.setattr(T.WorldModel, "load", staticmethod(lambda r: wm))
    raw, _ = fixtures.dataset(40, seed=1)
    sw = {"grid": [1.0, 3.0, 10.0], "every_epoch": True, "note": None, "chosen_w_reg": 10.0,
          "rule": T.RULE_EVERY_EPOCH,
          "candidates": [{"w_reg": w, "val_next_ce": 1.8, "effective_rank": 25.0, "sigreg": 0.05,
                          "within_bounds": True, "chosen": w == 10.0} for w in (1.0, 3.0, 10.0)]}
    card = E.scorecard(E.stream_view(raw), "synthetic: 40", ["fake-run"], recipe=A3,
                       training={"sweep_every_epoch": True, "sweep": sw})
    rec = card["recipe"]
    assert set(rec) == set(T.RECIPE_KEYS) | {"default"} and rec["default"] is False
    assert (rec["z_norm"], rec["w_reg_schedule"], rec["w_reg_start"], rec["w_reg_anneal_epochs"]) == \
        ("center", "anneal", 30.0, 4)
    pr = card["per_run"][0]["recipe"]
    assert pr["w_reg_per_epoch"][0] == 30.0 and pr["lr_warmup_steps"] == 40
    assert "pre_norm_collapse_test" not in pr          # a fake run has no model to re-encode
    txt = E.render(card)
    assert "collapse recipe: z_norm center" in txt
    assert "SIGReg weight anneal 30 → final w_reg over 4 epochs" in txt
    assert "fake-run: w_reg per epoch [30, 22.2, 16.4, 12.2, 10]" in txt
    assert "(info) z_norm center: SIGReg measured on centred z" in txt
    # pre-map beside post-map: the definitional change is visible on the card
    assert ("fake-run: post-map (center) val erank 5.0 SIGReg 0.050 | test erank "
            in txt)
    assert "pre-map (raw encoder) val erank 14.0 SIGReg 0.800 | test erank — SIGReg —" in txt
    assert "schedule anneal 30 → final w_reg over 4 epochs" in txt and "10 (30→10/4ep)" in txt
    # ledger: synthetic data is never recorded, the real path writes the recipe on every line
    assert card["ledger"] is None
    led = E.record_test_scoring({**card, "manifest": {"sha256": "m"}}, home=tmp_path / "led")
    rows = [json.loads(x) for x in Path(led["path"]).read_text().splitlines()]
    assert rows and all(r["recipe"] == rec for r in rows)


def test_default_card_says_raw_z(tmp_path, monkeypatch):
    rd = _run(tmp_path, monkeypatch, {"best_epoch": 0, "best": {"val_next_ce": 2.0}})
    wm = _FakeWM(rd, {"best_epoch": 0, "best": {"val_next_ce": 2.0}})
    monkeypatch.setattr(T.WorldModel, "load", staticmethod(lambda r: wm))
    card = E.scorecard(E.stream_view(fixtures.dataset(40, seed=1)[0]), "synthetic: 40", ["fake-run"])
    assert card["recipe"]["default"] is True
    txt = E.render(card)
    assert "collapse recipe: default (attempts 1-2)" in txt
    assert "(info) z_norm none: SIGReg measured on raw z" in txt and "pre-map" not in txt


# ---- declared n-floors for the gold criteria (C2, C5) ------------------------------------------------

def _gold(n, diff=-0.05, ci=(-0.08, -0.02)):
    return {"n": n, "quality": "gold", "diff": diff, "diff_ci": ci}


def test_rule2_floor_inconclusive_whatever_the_numbers():
    assert E.GOLD_TEST_FLOOR == 10
    assert E.rule2(_gold(9)) == "INCONCLUSIVE"                       # a clear win, but n < 10
    assert E.rule2(_gold(9, 0.05, (0.02, 0.08))) == "INCONCLUSIVE"   # a clear loss, too
    assert E.rule2(_gold(10)) == "PASS" and E.rule2(_gold(10, 0.05, (0.02, 0.08))) == "FAIL"
    assert E.floor_reason(9) == "below declared floor: n gold test tasks < 10 (n = 9)"
    assert E.floor_reason(10) is None


def test_rule5_floor_on_gold_and_bad_gold():
    assert E.BAD_GOLD_FLOOR == 3
    for v in ("PASS", "FAIL"):
        assert E.rule5({"verdict": v}, "gold", n_gold=9, n_bad=5) == "INCONCLUSIVE"
        assert E.rule5({"verdict": v}, "gold", n_gold=40, n_bad=2) == "INCONCLUSIVE"
        assert E.rule5({"verdict": v}, "gold", n_gold=10, n_bad=3) == v
    assert E.floor_reason(40, 2) == "below declared floor: n bad gold test tasks < 3 (n = 2)"
    assert E.floor_reason(5, 2).startswith("below declared floor: n gold test tasks < 10")


def _few_gold_ds(n_gold, n_bad):
    """40 synthetic sessions with only ``n_gold`` gold test main tasks (``n_bad`` of them failed);
    every other label is downgraded to weak."""
    raw, _ = fixtures.dataset(40, seed=1)
    ds = E.stream_view(raw)
    bad = good = 0
    for t in E.main_ids(ds, "test", gold_only=True):
        y = P.label(ds.tasks[t])
        keep = (y == 0 and bad < n_bad) or (y == 1 and good < n_gold - n_bad)
        bad += keep and y == 0
        good += keep and y == 1
        if not keep:
            ds.tasks[t]["outcome_src"] = "weak"
    assert len(E.main_ids(ds, "test", gold_only=True)) == n_gold
    return ds


@pytest.mark.parametrize("n_gold, n_bad, c2_floor, c5_floor", [
    (6, 3, True, True),          # < 10 gold: both
    (10, 2, False, True),        # enough gold, < 3 bad: C5 only
    (10, 3, False, False),
])
def test_scorecard_applies_and_prints_the_floors(tmp_path, monkeypatch, n_gold, n_bad, c2_floor,
                                                 c5_floor):
    s = {"best_epoch": 0, "best": {"val_next_ce": 2.0}}
    rd = _run(tmp_path, monkeypatch, s)
    wm = _FakeWM(rd, s)
    monkeypatch.setattr(T.WorldModel, "load", staticmethod(lambda r: wm))
    card = E.scorecard(_few_gold_ds(n_gold, n_bad), "synthetic: 40", ["fake-run"])
    p0 = card["per_run"][0]
    assert (2 in p0["floors"]) is c2_floor and (5 in p0["floors"]) is c5_floor
    if c2_floor:
        assert card["verdicts"][2] == "INCONCLUSIVE"
        assert p0["floors"][2] == f"below declared floor: n gold test tasks < 10 (n = {n_gold})"
    if c5_floor:
        assert card["verdicts"][5] == "INCONCLUSIVE"
        assert p0["floors"][5].startswith("below declared floor: n ")
    assert card["floors"]["gold_test_min"] == 10 and card["floors"]["bad_gold_test_min"] == 3
    txt = E.render(card)
    assert "floor: INCONCLUSIVE by rule if < 10 gold test tasks (declared floor" in txt
    assert "floor: INCONCLUSIVE by rule if < 10 gold test tasks or < 3 bad gold test tasks" in txt
    assert (f"INCONCLUSIVE (fake-run): below declared floor" in txt) is (c2_floor or c5_floor)
    assert any("below declared floor" in b for b in card["blockers"]) is (c2_floor or c5_floor)


# ---- MLX: a real tiny run under the recipe ---------------------------------------------------------

@needs_mlx
def test_cli_train_with_recipe_and_rescore_guard(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path))
    from apex_router.worldmodel.cli import main
    flags = ["--z-norm", "center", "--w-reg-schedule", "anneal", "--w-reg-start", "30",
             "--w-reg-anneal-epochs", "2"]
    rc = main(["evaluate", "--synthetic", "30", "--train", "--seeds", "1", "--no-cpu",
               "--epochs", "2", "--json", *flags])
    assert rc == 0
    card = json.loads(capsys.readouterr().out)
    assert card["recipe"]["z_norm"] == "center" and card["recipe"]["w_reg_schedule"] == "anneal"
    pr = card["per_run"][0]["recipe"]
    assert pr["w_reg_per_epoch"][0] == pytest.approx(30.0)
    for k in ("pre_norm_collapse", "pre_norm_collapse_test"):
        assert {"effective_rank", "sigreg"} <= set(pr[k])
    run = card["runs"][0]
    s = json.loads((tmp_path / "worldmodel" / "runs" / run / "summary.json").read_text())
    assert s["config"]["z_norm"] == "center" and s["config"]["w_reg_start"] == 30.0
    # re-scoring the saved run without the recipe flags is refused; with them it scores
    assert main(["evaluate", "--synthetic", "30", "--run", run, "--no-save"]) == 2
    assert "--z-norm center" in capsys.readouterr().err
    assert main(["evaluate", "--synthetic", "30", "--run", run, "--no-save", "--json", *flags]) == 0
    capsys.readouterr()
