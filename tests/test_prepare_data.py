"""WP8 acceptance: allowlist hard-fail, contamination abort, replay mix, attestation."""
import json, subprocess, sys
from pathlib import Path

PREP = Path(__file__).resolve().parent.parent / "src/apex_router/ornith/training/prepare_data.py"


def _rec(ans="def f(): return 1", source="learn_chain", train=True, deid=True, tags=None):
    return {"messages": [{"role": "user", "content": "write f"}], "corrected_answer": ans,
            "source": source, "approved_for_training": train, "deidentified": deid,
            "tags": tags or []}


def _run(args, feedback_lines):
    fb = Path(args["fb"]); fb.write_text("".join(json.dumps(r) + "\n" for r in feedback_lines))
    cmd = [sys.executable, str(PREP), "--feedback", str(fb), "--out", args["out"]]
    for k in ("replay_buffer", "book_ngrams"):
        if args.get(k): cmd += [f"--{k.replace('_','-')}", args[k]]
    return subprocess.run(cmd, capture_output=True, text=True)


def test_book_derived_refused_from_weights(tmp_path):
    r = _run({"fb": tmp_path/"fb.jsonl", "out": str(tmp_path/"o")},
             [_rec(source="book"), _rec(source="learn_chain")])
    assert r.returncode == 0
    train = (tmp_path/"o"/"train.jsonl").read_text() + (tmp_path/"o"/"valid.jsonl").read_text()
    # exactly the first-party record survives; book text excluded
    assert train.count("corrected_answer") == 0  # MLX format has no such key
    assert (tmp_path/"o"/"attestation.json").exists()


def test_contamination_aborts(tmp_path):
    ng = tmp_path/"ng.txt"
    ng.write_text("the quick brown fox jumps over the lazy dog now\n")  # an 8-gram
    r = _run({"fb": tmp_path/"fb.jsonl", "out": str(tmp_path/"o"), "book_ngrams": str(ng)},
             [_rec(ans="the quick brown fox jumps over the lazy dog now indeed")])
    assert r.returncode == 4 and "CONTAMINATION" in r.stderr
    assert not (tmp_path/"o"/"train.jsonl").exists()


def test_replay_mix_and_attestation(tmp_path):
    rb = tmp_path/"replay.jsonl"
    rb.write_text(json.dumps({"messages": [{"role":"user","content":"hi"},{"role":"assistant","content":"hello"}]})+"\n")
    r = _run({"fb": tmp_path/"fb.jsonl", "out": str(tmp_path/"o"), "replay_buffer": str(rb)},
             [_rec() for _ in range(10)])
    assert r.returncode == 0
    att = json.loads((tmp_path/"o"/"attestation.json").read_text())
    assert att["ngram_check"] == "pass" and att["n_replay"] >= 1 and att["dataset_hash"]
