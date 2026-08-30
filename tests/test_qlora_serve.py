"""WP9 acceptance: dry-run w/o mlx-lm exits 1; gate ON repoints + rollback; probe drop rejects."""
import json, subprocess, sys
from pathlib import Path
from apex_router.qlora_serve import evaluate, repoint, rollback, run_probe, pointer_path

TRAIN = Path(__file__).resolve().parent.parent / "src/apex_router/ornith/training/train.sh"


def _mkdata(d):
    d.mkdir(parents=True, exist_ok=True)
    (d/"train.jsonl").write_text('{"messages":[]}\n'); (d/"valid.jsonl").write_text('{"messages":[]}\n')
    (d/"attestation.json").write_text('{"ngram_check":"pass"}')
    return d


def test_dryrun_without_mlx_exits_1(tmp_path):
    data = _mkdata(tmp_path/"data")
    r = subprocess.run(["bash", str(TRAIN), "--data", str(data), "--cycle-id", "c1", "--dry-run"],
                       capture_output=True, text=True)
    # this box has no mlx-lm -> clear error, exit 1, nothing created
    assert r.returncode == 1 and "mlx-lm" in (r.stdout + r.stderr)
    assert not (tmp_path/"data"/"adapters").exists()


def test_missing_data_exits_3(tmp_path):
    r = subprocess.run(["bash", str(TRAIN), "--data", str(tmp_path/"none"), "--cycle-id", "c1", "--dry-run"],
                       capture_output=True, text=True)
    assert r.returncode == 3


def test_gate_on_repoints_and_rollback(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_LOCAL_POINTER", str(tmp_path/"ptr.json"))
    monkeypatch.setenv("ORNITH_LOCAL_TAG", "local:incumbent")
    good = lambda prompt: {"Reverse the string 'hello'.": "olleh", "What is 17 + 25?": "42",
                           "Name the data structure with LIFO order.": "a stack",
                           "Complete: def add(a,b): return": "a + b"}.get(prompt, "")
    dec = evaluate("cyc7", generate_base_fn=good, generate_cand_fn=good, gate_fn=lambda: True)
    assert dec["promoted"] is True
    assert json.loads((tmp_path/"ptr.json").read_text())["current"] == "local-candidate:cyc7"
    back = rollback()
    assert back["current"] == "local:incumbent"          # one-command rollback restores prior tag


def test_probe_regression_rejects(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_LOCAL_POINTER", str(tmp_path/"ptr.json"))
    base = lambda p: "olleh 42 stack a + b"     # base passes everything
    cand = lambda p: ""                         # candidate fails everything -> big drop
    dec = evaluate("cyc8", generate_base_fn=base, generate_cand_fn=cand, gate_fn=lambda: True)
    assert dec["promoted"] is False and "probe regressed" in dec["reason"]
    assert not (tmp_path/"ptr.json").exists()   # incumbent untouched


def test_missing_probe_fails_closed(tmp_path, monkeypatch):
    pointer = tmp_path / "ptr.json"
    monkeypatch.setenv("APEX_LOCAL_POINTER", str(pointer))
    dec = evaluate(
        "cyc9",
        generate_base_fn=lambda _p: "good",
        generate_cand_fn=lambda _p: "good",
        gate_fn=lambda: True,
        probe_path=tmp_path / "missing.jsonl",
    )
    assert dec == {
        "promoted": False,
        "reason": "general probe unavailable or empty",
        "probe_pre": None,
        "probe_post": None,
    }
    assert not pointer.exists()
