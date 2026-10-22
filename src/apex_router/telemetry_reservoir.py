"""Telemetry reservoir: keep a stratified sample of proxy telemetry before rotation deletes it.

The proxy rotates ``telemetry.jsonl`` at 64 MB and keeps the newest ``KEEP`` (20) generations; the
oldest are deleted. Before this module, a deleted generation was gone: rare error causes (a
``ConnectError`` burst, an ``http_401`` week) left no trace after about six months.

Now ``TelemetryWriter.rotate_if_large`` calls ``fold_generation`` on each generation it is about to
delete. The rows go into a ``core.reservoir.StratifiedReservoir`` stored next to the telemetry file
(``telemetry_reservoir.json``, mode 0600):

- stratum = ``error_cause | model_resolved | client`` (``ok`` when no error), so every error cause
  keeps its own sample and its exact count;
- a row keeps numbers, bools and enum labels only. ``session_id``, ``request_id``, ``agent_id``,
  ``epoch_id``, ``error_detail``, ``usage``, ``shadow`` and ``transforms`` are never stored.

A generation is folded once: its id (file name + size) is recorded in the store with the counts.
Order: fold -> save the store (atomic) -> the caller deletes the file. A crash before the save
folds again next time (nothing counted yet); a crash after it skips the file (already counted).

CLI: ``apex-router telemetry-reservoir fold [--backfill]`` folds every rotated generation now (they
are then skipped at prune time); ``telemetry-reservoir report [--json]`` prints per-stratum counts.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from typing import get_args as _args

from .core import reservoir as R
from .proxy_engine.proxy.upstream import RETRY_POLICIES as _RETRY_ARMS
from .proxy_engine.telemetry.events import BustCause, GuardAction, MatcherEvent

STORE_NAME = "telemetry_reservoir.json"
CAP = 100                       # rows kept per stratum
MAX_STRATA = 256
SEED = 7

_ENUMS = ("client", "model_requested", "model_resolved", "stratum", "endpoint_id", "error_cause",
          "stop_reason", "matcher_event", "retry_arm", "bust_cause", "guard_action")
_BOOLS = ("is_error", "upstream_rejected", "bust", "guard_fired")
_NUMS = ("ts", "turn", "tokens_in", "tokens_out", "cache_read_tokens", "cache_write_tokens",
         "ttft_ms", "t_upstream_ttfb_ms", "apex_added_ms", "upstream_error_wait_ms",
         "connect_retries", "connect_backoff_ms", "bytes_up", "bytes_down", "frontier_rerenders",
         "ccr_retrievals")
# Field vocabularies (F2): closed sets, not patterns. A value outside its field's set is stored as
# "other", so no free text (a name, a diagnosis, a path) can reach the store. The enum types come
# from the writer's own Literal types; the rest are the values the proxy code sets.
_SETS = {
    "client": {"claude-code", "codex", "pi", "unknown"},
    "endpoint_id": {"anthropic", "openai"},
    "stratum": {"xs", "s", "m", "l", "xl", "xxl", "unknown"},
    "stop_reason": {"end_turn", "tool_use", "max_tokens", "stop_sequence", "pause_turn", "refusal",
                    "content_filter", "completed", "failed", "incomplete", "cancelled",
                    "model_context_window_exceeded"},
    "matcher_event": set(_args(MatcherEvent)),
    "bust_cause": set(_args(BustCause)),
    "guard_action": set(_args(GuardAction)),
    "retry_arm": set(_RETRY_ARMS),
}
# error_cause is set by the proxy code only: `http_<status>` or an exception class name
# (type(e).__name__), optionally "midstream_"-prefixed. It never carries request text.
_CAUSE = re.compile(r"\A(?:http_[1-5]\d\d|(?:midstream_)?[A-Z][A-Za-z]{2,39})\Z")
# Model names (reconciled after 3 review passes: a pattern is never a closed list). A model id is
# kept only if it is in KNOWN_MODELS: the ids seen in this proxy's telemetry up to 2026-10-22, plus
# every id the model registry names (tiers, pi families, venues), with and without the foundry
# "it-entra-" prefix. Any other id is stored as "other": counted, never written as text. A new
# model shows up in `telemetry-reservoir report` as an "other" model stratum; add it here then.
_SEEN_MODELS = frozenset({
    "claude-opus-5-5", "claude-sonnet-5", "claude-sonnet-5-5", "claude-haiku-4-5",
    "claude-opus-4-8", "claude-opus-5", "claude-sonnet-4-6", "claude-fable-5-1",
    "gpt-5.3-codex", "gpt-5.4", "gpt-5.5", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra",
    "gpt-6", "gpt-6-astra", "gpt-6-codex", "gpt-6-luna", "gpt-6-nova", "gpt-6-pro", "gpt-6-sol",
    "gpt-6-terra", "gpt-6.1", "gpt-6.1-astra", "gpt-6.1-codex", "gpt-6.1-luna", "gpt-6.1-pro",
    "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6.5-sol",
})
_PREFIX = "it-entra-"
_MODEL_ID = re.compile(r"\A[a-z0-9][a-z0-9.-]{0,63}\Z")


def _registry_models() -> set:
    """Every model id string in the model registry (fail-open: empty on any error)."""
    out: set = set()
    try:
        from . import model_registry

        def walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    if k in ("id", "default_model", "downshift_model", "code_model",
                             "deep_ctx_model") or isinstance(v, (dict, list)):
                        walk(v) if isinstance(v, (dict, list)) else out.add(v)
                    elif k in ("haiku", "sonnet", "opus", "fable") and isinstance(v, str):
                        out.add(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)

        walk(model_registry.load())
        walk(model_registry.families())
    except Exception:  # noqa: BLE001
        return set()
    # only plain ids: no path or registry form ("hf.co/org/x:tag") can be a stratum key part
    return {m for m in out if isinstance(m, str) and _MODEL_ID.match(m)}


def _known_models() -> frozenset:
    base = set(_SEEN_MODELS) | {m.removeprefix(_PREFIX) for m in _registry_models()}
    return frozenset(base | {_PREFIX + m for m in base})


KNOWN_MODELS = _known_models()


OTHER_VALUE = "other"
FINE_STRATA = MAX_STRATA - 64     # cause|model|client keys; the last 64 slots: cause-only keys


def store_path(telemetry: Path) -> Path:
    return Path(telemetry).with_name(STORE_NAME)


def _enum(field: str, v: Any) -> str | None:
    """The value if it is in the field's vocabulary, "other" if not, None if missing."""
    if v is None:
        return None
    if not isinstance(v, str):
        return OTHER_VALUE
    if field in _SETS:
        ok = v in _SETS[field]
    elif field == "error_cause":
        ok = bool(_CAUSE.match(v))
    else:                                              # model_requested, model_resolved
        ok = v in KNOWN_MODELS
    return v if ok else OTHER_VALUE


def row_sample(d: Any) -> tuple[str, dict] | None:
    """(stratum key, flat row) for one telemetry request row; None for heartbeats and junk."""
    if not isinstance(d, dict) or "tokens_in" not in d:
        return None
    row: dict = {}
    for f in _ENUMS:
        row[f] = _enum(f, d.get(f))
    for f in _BOOLS:
        v = d.get(f)
        row[f] = v if isinstance(v, bool) else None
    for f in _NUMS:
        v = d.get(f)
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        row[f] = v if ok else None
    cause = row["error_cause"] or "ok"
    key = R.key(cause, row["model_resolved"] or "none", row["client"] or "none")
    return key, row


def _cause_of(key: str) -> str:
    return key.split("|", 1)[0]


def _place(res: R.StratifiedReservoir, key: str) -> str:
    """The stratum a row goes to. A new cause|model|client key beyond FINE_STRATA falls back to
    the cause-only key ``cause|other|other``, so each error cause keeps its own sample even when
    models x clients are many (F3)."""
    if key in res.strata or len(res.strata) < FINE_STRATA:
        return key
    return R.key(_cause_of(key), OTHER_VALUE, OTHER_VALUE)


def fold_lines(lines, res: R.StratifiedReservoir, by_cause: dict | None = None) -> dict:
    """Add every request row of ``lines`` to ``res`` and count it in ``by_cause`` (exact, never
    folded into a shared bucket). -> {rows, skipped, bad}."""
    n = skipped = bad = 0
    for line in lines:
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            bad += 1
            continue
        try:
            got = row_sample(d)
        except (ValueError, TypeError):                # a row with no valid stratum key
            bad += 1
            continue
        if got is None:
            skipped += 1
            continue
        key, row = got
        try:
            res.add(_place(res, key), row)
            n += 1
        except (ValueError, TypeError):
            bad += 1
            continue
        if by_cause is not None:
            c = _cause_of(key)
            by_cause[c] = by_cause.get(c, 0) + 1
    return {"rows": n, "skipped": skipped, "bad": bad}


# --- the store ----------------------------------------------------------------------------------

def _empty() -> dict:
    return {"v": 1, "folded": [], "by_cause": {}, "reservoir": None}


def load(path: Path) -> tuple[dict, R.StratifiedReservoir]:
    """(store doc, reservoir). A missing file is an empty store. A bad file raises ValueError:
    the caller must not delete telemetry it cannot count."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        doc = _empty()
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e}") from e
    if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("folded"), list) \
            or not isinstance(doc.setdefault("by_cause", {}), dict):
        raise ValueError(f"bad reservoir store {path}")
    res = (R.StratifiedReservoir.from_dict(doc["reservoir"]) if doc.get("reservoir")
           else R.StratifiedReservoir(cap=CAP, max_strata=MAX_STRATA, seed=SEED))
    return doc, res


def save(path: Path, doc: dict, res: R.StratifiedReservoir) -> None:
    """Atomic write, mode 0600."""
    path = Path(path)
    doc = {**doc, "reservoir": res.to_dict()}       # the ledger is never cut: count once (F4)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY)       # the rename is durable before a delete (F6)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


LOCK_WAIT_S = 10.0


class _LockBusy(OSError):
    pass


@contextlib.contextmanager
def _locked(path: Path, wait_s: float | None = None):
    """Exclusive store lock with a bounded wait; OSError (the caller keeps the file) on timeout."""
    import fcntl
    wait_s = LOCK_WAIT_S if wait_s is None else wait_s
    lock = Path(path).with_name(Path(path).name + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise _LockBusy("reservoir store is locked") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def gen_id(gen: Path) -> str:
    """Content identity (F5): SHA-256 of the bytes. A renamed or restored copy has the same id (not
    counted again); different content under a reused name has a new id (counted)."""
    import hashlib
    h = hashlib.sha256()
    with open(gen, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


DEFERRED = "deferred"     # fold_generation result: the store lock is busy; try again later


def fold_generation(gen: Path, store: Path | None = None) -> bool | str:
    """Fold one rotated generation into the store, once. True = counted (now or before), so the
    caller may delete the file. False = not counted (unreadable file or store): keep the file.
    DEFERRED = the store is locked by another fold: keep the file, never force-delete it."""
    gen = Path(gen)
    store = store_path(gen.with_name(gen.name.rsplit(".", 1)[0])) if store is None else Path(store)
    try:
        gid = gen_id(gen)
    except OSError:
        return False
    try:
        with _locked(store):
            doc, res = load(store)
            if any(f.get("gen") == gid for f in doc["folded"] if isinstance(f, dict)):
                return True
            with open(gen, encoding="utf-8", errors="replace") as fh:
                counts = fold_lines(fh, res, doc["by_cause"])
            doc["folded"].append({"gen": gid, "name": gen.name, "at": round(time.time(), 3),
                                  **counts})
            save(store, doc, res)
        return True
    except _LockBusy:
        return DEFERRED
    except (OSError, ValueError):
        return False


# --- report -------------------------------------------------------------------------------------

def report(store: Path) -> dict:
    doc, res = load(store)
    pop = res.population()
    total = sum(pop.values())
    by_cause = dict(doc["by_cause"])                  # exact, independent of stratum overflow
    strata = [{"stratum": h, "n": n, "kept": len(res.strata[h]["rows"])}
              for h, n in sorted(pop.items(), key=lambda x: -x[1])]
    total = sum(by_cause.values()) or total
    return {"generations": len(doc["folded"]), "rows": total, "strata": len(pop),
            "by_cause": dict(sorted(by_cause.items(), key=lambda x: -x[1])),
            "error_share": (1 - by_cause.get("ok", 0) / total) if total else None,
            "top": strata}


def main(argv: list[str] | None = None) -> int:
    from .telemetry_path import telemetry_files, telemetry_path
    ap = argparse.ArgumentParser(prog="apex-router telemetry-reservoir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fold", help="fold rotated telemetry generations into the reservoir")
    f.add_argument("--backfill", action="store_true",
                   help="fold every rotated generation now (default: only the oldest beyond KEEP)")
    r = sub.add_parser("report", help="per-stratum counts from the reservoir")
    r.add_argument("--json", action="store_true")
    r.add_argument("--top", type=int, default=15)
    a = ap.parse_args(argv)
    tel = telemetry_path()
    store = store_path(tel)
    if a.cmd == "fold":
        gens = [g for g in telemetry_files(tel) if g != tel]
        if not a.backfill:
            from .proxy_engine.telemetry.events import TelemetryWriter
            gens = gens[: max(0, len(gens) - TelemetryWriter.DEFAULT_KEEP)]
        ok = sum(fold_generation(g, store) is True for g in gens)
        print(f"telemetry-reservoir: {ok}/{len(gens)} generation(s) counted -> {store}")
        return 0 if ok == len(gens) else 1
    try:
        rep = report(store)
    except ValueError as e:
        print(f"telemetry-reservoir: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rep, indent=1))
        return 0
    es = "n/a" if rep["error_share"] is None else f"{100 * rep['error_share']:.2f} %"
    print(f"telemetry reservoir: {rep['generations']} generation(s), {rep['rows']} rows, "
          f"{rep['strata']} strata, error share {es}")
    print("  by cause: " + ", ".join(f"{c} {n}" for c, n in rep["by_cause"].items()))
    for s in rep["top"][: a.top]:
        print(f"  {s['n']:>8}  kept {s['kept']:>3}  {s['stratum']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
