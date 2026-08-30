"""WP9 — shadow-tag QLoRA evaluation + serving pointer (path a).

A fused candidate is imported as `local-candidate:<cycle_id>`. Before it can serve, it
must clear TWO bars against the CURRENT PRODUCTION CONFIG (RAG included — not the
pre-train adapter):
  1. a frozen general probe (reject if score drops > 2% -> catastrophic forgetting), and
  2. the promotion gate on a (layer, task_class) bench cell (RAG-incumbent vs QLoRA-cand).
On pass, the `local` family pointer is repointed to the candidate tag; the previous tag
is kept for one-command rollback. One serving stack only (ollama/GGUF).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PROBE_PATH = Path(__file__).resolve().parent / "ornith" / "training" / "probe_general.jsonl"
PROBE_DROP_LIMIT = float(os.environ.get("QLORA_PROBE_DROP_LIMIT", "0.02"))


def pointer_path() -> Path:
    env = os.environ.get("APEX_LOCAL_POINTER")
    return Path(env) if env else Path.home() / ".apex-router" / "local_model.json"


def load_probe(path: Path | None = None) -> list[dict]:
    path = Path(path) if path else PROBE_PATH
    out = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def run_probe(probe: list[dict], generate_fn) -> float:
    """Fraction of probe prompts whose generated text contains the expected substring."""
    if not probe:
        return 0.0
    ok = 0
    for item in probe:
        try:
            out = generate_fn(item.get("prompt", "")) or ""
        except Exception:  # noqa: BLE001 — a generation error counts as a miss, not a crash
            out = ""
        if str(item.get("expect_contains", "")).lower() in out.lower():
            ok += 1
    return ok / len(probe)


def _read_pointer() -> dict:
    p = pointer_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"current": os.environ.get("ORNITH_LOCAL_TAG", "local:incumbent"), "previous": None}


def _write_pointer(d: dict) -> None:
    p = pointer_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2), encoding="utf-8")


def repoint(new_tag: str) -> dict:
    ptr = _read_pointer()
    ptr = {"current": new_tag, "previous": ptr.get("current")}
    _write_pointer(ptr)
    return ptr


def rollback() -> dict:
    ptr = _read_pointer()
    if not ptr.get("previous"):
        return ptr
    ptr = {"current": ptr["previous"], "previous": ptr.get("current")}
    _write_pointer(ptr)
    return ptr


def evaluate(cycle_id: str, *, generate_base_fn, generate_cand_fn, gate_fn,
             probe_path: Path | None = None) -> dict:
    """Gate the candidate tag against the current production config.

    gate_fn() -> bool: True iff the (layer,task_class) cell promotes the QLoRA candidate
    over the RAG-incumbent via amr.gate (caller wires deltas_from_rows -> run_gate).
    Returns a decision dict; only promotes (repoints) when BOTH bars pass.
    """
    probe = load_probe(probe_path)
    if not probe:
        return {"promoted": False, "reason": "general probe unavailable or empty",
                "probe_pre": None, "probe_post": None}
    pre = run_probe(probe, generate_base_fn)
    post = run_probe(probe, generate_cand_fn)
    if post < pre - PROBE_DROP_LIMIT:
        return {"promoted": False, "reason": f"probe regressed {pre:.2f}->{post:.2f} (> {PROBE_DROP_LIMIT})",
                "probe_pre": pre, "probe_post": post}
    if not gate_fn():
        return {"promoted": False, "reason": "gate did not promote candidate over RAG-incumbent",
                "probe_pre": pre, "probe_post": post}
    ptr = repoint(f"local-candidate:{cycle_id}")
    return {"promoted": True, "reason": "probe held + gate promoted", "probe_pre": pre,
            "probe_post": post, "pointer": ptr}


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="qlora-serve")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rollback")
    st = sub.add_parser("status")
    a = p.parse_args(argv)
    if a.cmd == "rollback":
        print(json.dumps(rollback(), indent=2)); return 0
    print(json.dumps(_read_pointer(), indent=2)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
