#!/usr/bin/env python3
"""CLI for the Ornith code-Q&A harness.

Usage:
  python -m codeqa.cli ask   <repo> "question"       # one question, grounded + cited
  python -m codeqa.cli batch <repo> q.txt            # one question per line (cache-reused)
  python -m codeqa.cli retrieve <repo> "question"    # show retrieved chunks only (no Ornith)
  python -m codeqa.cli repos                          # list registered repos

repos: a C++ repo and a Ruby repo are the reference configs. Register more as codeqa/repos/<name>.json.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _cmd_repos(_args) -> int:
    from .retriever import REPOS_DIR, RepoConfig
    for p in sorted(REPOS_DIR.glob("*.json")):
        try:
            cfg = RepoConfig.load(p.stem)
            digest = "digest ✓" if cfg.digest else "digest ✗ (missing)"
            idx = cfg.index.get("kind", "none")
            print(f"  {cfg.name:12} {cfg.language:6} {digest:20} index={idx}  {cfg.root}")
        except Exception as e:  # noqa: BLE001
            print(f"  {p.stem:12} ERROR: {e}")
    return 0


def _cmd_ground(args) -> int:
    """Grounding oracle: read a finding/report (file arg or stdin), check every file:line citation
    against live registered-repo code, print the verdict. With --check, exit 2 if any citation is
    'stale' (a provable factual defect) so a review can gate on it. 'unverified' is advisory and
    never trips --check."""
    from .ground_claims import ground_text
    text = Path(args.file).read_text() if args.file else sys.stdin.read()
    v = ground_text(text)
    if getattr(args, "json", False):
        print(_json.dumps({
            "applicable": v.applicable, "has_problem": v.has_problem,
            "citations": [{"file": c.file, "start": c.start, "end": c.end,
                           "repo": c.repo, "verdict": c.verdict} for c in v.citations],
        }, indent=2))
    else:
        print(v.summary())
        for c in v.citations:
            mark = {"grounded": "✓", "stale": "~ STALE",
                    "unverified": "? unverified"}.get(c.verdict, "?")
            print(f"  {mark:16} {c.repo}: {c.file}:{c.start}"
                  + (f"-{c.end}" if c.end != c.start else ""))
    if getattr(args, "check", False) and v.has_problem:
        return 2
    return 0


def _cmd_doctor(args) -> int:
    """Post-install validation: per-repo health of everything in CODEQA_REPOS.
    Exits nonzero with --check if any repo is unhealthy (root missing / no reachable code)."""
    from .retriever import REPOS_DIR
    from . import doctor
    rows = doctor.repo_health(repos_dir=REPOS_DIR)
    if not rows:
        print(f"  no repo configs in {REPOS_DIR} "
              f"(set CODEQA_REPOS to your configs dir)")
        return 1 if args.check else 0
    for r in rows:
        mark = "OK " if r["ok"] else "BAD"
        bits = []
        bits.append("root✓" if r["root_exists"] else "root✗MISSING")
        bits.append(f"code={r['code_files']}" if r["root_exists"] else "code=?")
        bits.append("digest✓" if r["digest_ok"] else "digest✗")
        detail = r["error"] or " ".join(bits)
        print(f"  [{mark}] {r['name']:14} {detail}   {r.get('root','')}")
    healthy = doctor.all_healthy(rows)
    n_bad = sum(1 for r in rows if not r["ok"])
    print(f"  {'all repos healthy' if healthy else f'{n_bad} repo(s) unhealthy'} "
          f"({len(rows)} total)")
    return (0 if healthy else 1) if args.check else 0


def _cmd_retrieve(args) -> int:
    from .retriever import RepoConfig, retrieve
    cfg = RepoConfig.load(args.repo)
    chunks = retrieve(cfg, args.question)
    if not chunks:
        print("(no chunks retrieved — try more specific identifiers)")
        return 0
    for ch in chunks:
        print(f"\n── {ch.cite()}  ({ch.why}) ──")
        print(ch.text)
    return 0


import os as _os
import json as _json
import subprocess as _sp
from pathlib import Path as _Path

_FRESHNESS_CACHE = _Path("~/.codeqa/freshness_cache.json").expanduser()


def _code_marker(root) -> str:
    """A code-version marker for the fingerprint: git HEAD PLUS a hash of the DIRTY working tree
    (staged+unstaged+untracked), so a code change re-triggers validation even before it's committed
    (Codex P1-1: HEAD alone missed uncommitted edits — the exact case you hit while actively working).
    '' when not a git repo (so a non-git root's cache is content-only; noted as a limitation)."""
    import hashlib
    try:
        head = _sp.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                       capture_output=True, text=True, timeout=5)
        if head.returncode != 0:
            return ""
        # `git status --porcelain` + a diff hash captures staged/unstaged/untracked edits cheaply
        dirty = _sp.run(["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
                        capture_output=True, text=True, timeout=10)
        diff = _sp.run(["git", "-C", str(root), "diff", "HEAD"],
                       capture_output=True, text=True, timeout=10)
        h = hashlib.sha256((dirty.stdout + "\x00" + diff.stdout).encode("utf-8", "replace")).hexdigest()[:12]
        return f"{head.stdout.strip()}+{h}"
    except (OSError, _sp.SubprocessError):
        return ""


def _runtime_spec(cfg):
    """Build the runtime-oracle spec from the repo config, expanding ~ in file paths and PRESERVING the
    commands block (not just files+status)."""
    spec = (cfg.raw.get("runtime_oracle") if getattr(cfg, "raw", None) else None) or \
        getattr(cfg, "runtime_oracle", None)
    if not spec:
        return None
    return {"files": [_os.path.expanduser(f) for f in spec.get("files", []) if f],
            "status_url": spec.get("status_url"),
            "commands": spec.get("commands")}


def _local_verifier():
    """The on-device (Ornith 35B) verifier seam — free, sufficient for VALUE claims (measured)."""
    from .freshness import _VERIFIER_SYS
    from ..ornith import ornith_client as oc
    def verify(claim, code):  # noqa: E731
        r = oc.chat_messages(
            [{"role": "system", "content": _VERIFIER_SYS},
             {"role": "user", "content": f"CLAIM:\n{claim}\n\nDEFINITION LINES:\n{code}\n\nOne word:"}],
            max_tokens=8, enable_thinking=False, temperature=0.0)
        return r.answer or ""
    return verify


def _make_verifier(local: bool):
    from .freshness import frontier_verifier
    return _local_verifier() if local else frontier_verifier


def _local_model_id() -> str:
    """The resident local model id (label only, for the per-model call tally)."""
    try:
        from ..ornith import ornith_client as oc
        return str(oc.MODEL)
    except Exception:  # noqa: BLE001 — a label must never break the run
        return "local"


def _judge_setup(*, local: bool, route: bool, env=None):
    """Resolve the judge contract for one validate run (see tier_router.judge_mode):
    returns (judge_mode, pinned_model, adjudicating, local_screen).
      - judge_mode None (no CODEQA_JUDGE_MODEL) or "pinned" → legacy single-verifier path.
      - "screen"/"both" → the routed tier screens, the pin adjudicates (never under --local: no
        paid call happens there at all).
      - local_screen: --route, OR CODEQA_LOCAL_SCREEN=1 while adjudicating (the Ornith screen for
        VALUE claims). CODEQA_LOCAL_SCREEN is inert without a pin (unset-pin behaviour unchanged)."""
    from . import tier_router
    env = _os.environ if env is None else env
    jm = tier_router.judge_mode(env)
    pin = tier_router.explicit_model_override(env)
    adjudicating = jm in ("screen", "both") and not local
    local_screen = bool(route) or (adjudicating and env.get("CODEQA_LOCAL_SCREEN") == "1")
    return jm, pin, adjudicating, local_screen


def _model_fingerprint(*, local: bool, local_screen: bool, pin, jm, env=None) -> str:
    """The MODEL half of the validate cache fingerprint: every model id validation could route to.
    A verdict is only as current as the model that produced it, so a tier bump (e.g. opus 4-8 → 5-5
    in tier_router / models.json / CODEQA_TIER_MODELS) or a new resident local model must
    re-validate instead of reusing entries judged by the old model.
      - frontier runs: the resolved haiku/sonnet/opus ids (screen tiers, or the unpinned routes)
      - local model id: under --local, or when local screening is on (--route / CODEQA_LOCAL_SCREEN)
      - the pin + judge mode + local-screen flag when CODEQA_JUDGE_MODEL is set"""
    from . import tier_router
    env = _os.environ if env is None else env
    parts = []
    if not local:
        tiers = tier_router._tier_models(env)
        parts.append("tiers=" + ",".join(f"{t}:{tiers[t]}" for t in sorted(tiers)))
    if local or local_screen:
        parts.append(f"localm={_local_model_id()}")
    if pin:
        parts.append(f"judge={pin}|jm={jm}|ls={local_screen}")
    return "|".join(parts)


def _validate_one(repo, file, *, local=False, route=False, runtime=False, write=None, use_cache=True):
    """Validate one memory/digest against one repo. Returns (n_struck, struck_claims, cached_bool).
    Auto-wire: skips re-validation when the fingerprint is unchanged. The fingerprint folds in the
    memory bytes, the code (HEAD + dirty tree), the verifier choice (local vs frontier), AND — when
    --runtime is on — the actual runtime facts, so a mode switch or a runtime-state change re-validates
    rather than returning a stale cross-mode result (Codex P1-2/P1-3)."""
    import hashlib
    from .freshness import validate_memory, gather_runtime_facts, memory_fingerprint
    from .retriever import RepoConfig
    cfg = RepoConfig.load(repo)
    text = _Path(file).read_text()
    # Runtime facts are gathered BEFORE the cache check so they participate in the fingerprint — a
    # change in the running system (status, telemetry count) must invalidate a prior clean result.
    runtime_facts = None
    if runtime:
        spec = _runtime_spec(cfg)
        if not spec:
            print(f"⚠ --runtime: '{repo}' has no 'runtime_oracle' in its config; runtime-state "
                  "claims stay UNVERIFIABLE.")
        else:
            runtime_facts = gather_runtime_facts(spec)
    jm, pin, adjudicating, local_screen = _judge_setup(local=local, route=route)
    mode = f"local={local}|route={route}|runtime={bool(runtime_facts)}|" + \
        hashlib.sha256((runtime_facts or "").encode("utf-8", "replace")).hexdigest()[:12]
    # Every model id validation could route to (tiers, local, pin) — a model bump re-validates.
    mode += "|" + _model_fingerprint(local=local, local_screen=local_screen and not local,
                                     pin=pin, jm=jm)
    fp = memory_fingerprint(file, cfg.root, code_marker=_code_marker(cfg.root) + "|" + mode)
    cache = {}
    if use_cache and _FRESHNESS_CACHE.exists():
        try:
            loaded = _json.loads(_FRESHNESS_CACHE.read_text())
            cache = loaded if isinstance(loaded, dict) else {}     # P2-8: tolerate a non-dict cache
        except (OSError, ValueError):
            cache = {}
    ckey = f"{repo}\x1f{file}"                                     # P2-8: NUL-ish sep, not ':' (path colons)
    entry = cache.get(ckey)
    if use_cache and isinstance(entry, dict) and entry.get("fp") == fp and "n_struck" in entry:
        struck = entry.get("struck", [])
        if write:                                                 # P2-4: honor --write even on a cache hit
            _apply_struck_to_file(text, struck, write)
        return entry["n_struck"], struck, True, entry.get("n_skipped", 0)
    # --route (or CODEQA_LOCAL_SCREEN=1 under a screen/both judge): supply the local verifier so VALUE
    # claims use it (frontier reserved for INFERENCE/RUNTIME) — measured −62% frontier tokens with no
    # accuracy loss vs all-frontier.
    from .freshness import routed_frontier_verifier, pinned_verifier
    local_vf = _local_verifier() if (local_screen and not local) else None
    if adjudicating:
        # SCREEN, THEN ADJUDICATE: the routed tier (pin ignored) screens every claim; the pinned model
        # re-judges what the screen did not clear ("screen") or every claim ("both"), and wins.
        result = validate_memory(text, cfg.root, verify_fn=routed_frontier_verifier,
                                 local_verify_fn=local_vf, runtime_facts=runtime_facts,
                                 adjudicate_fn=pinned_verifier(pin), judge_mode=jm,
                                 adjudicator_model=pin,
                                 local_model=_local_model_id() if local_vf else "local")
    else:
        result = validate_memory(text, cfg.root, verify_fn=_make_verifier(local),
                                 local_verify_fn=local_vf, runtime_facts=runtime_facts,
                                 local_model=_local_model_id() if local_vf else "local")
    if write:
        _Path(write).write_text(result.text)
    n_adj_failed = getattr(result, "n_adjudicate_failed", 0)
    if n_adj_failed:
        print(f"⚠ {n_adj_failed} adjudication call(s) to {pin} failed (transport / empty / unparsable "
              "reply) — kept the screen verdict; result NOT cached so the next run retries.")
    if use_cache and not n_adj_failed:      # a failed adjudication must be retried, never cached
        cache[ckey] = {"fp": fp, "n_struck": result.n_struck, "struck": result.struck_claims,
                       "n_skipped": result.n_skipped}
        try:
            _FRESHNESS_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _FRESHNESS_CACHE.write_text(_json.dumps(cache, indent=2))
        except OSError:
            pass
    _emit_metrics(repo, file, result, cached=False, routed=bool(local_vf), local_only=local,
                  runtime=bool(runtime_facts), judge_mode=jm if not local else None,
                  judge_model=pin if not local else None)
    return result.n_struck, result.struck_claims, False, result.n_skipped


_METRICS_PATH = _Path("~/.codeqa/validate_metrics.jsonl").expanduser()


def _emit_metrics(repo, file, result, *, cached, routed, local_only, runtime,
                  judge_mode=None, judge_model=None):
    """Append one benchmark line per validation run, so runs are differentiable/comparable over time
    (which repo, how many struck/local/frontier/skipped, token cost, whether routed/cached).

    Judge-mode fields (added 2026-10; readers must .get() them — older rows lack them):
      judge_mode (None|pinned|screen|both), judge_model (the pin), n_screened, n_adjudicated,
      n_screen_struck, n_adjudicated_struck, n_adjudicate_failed (adjudicator call failed → screen
      verdict kept, not counted as adjudicated/paid), n_agree/n_compared/agreement ("both" only),
      n_frontier_calls (PAID calls made — drives est_frontier_tokens), model_calls (every verifier
      call keyed by ACTUAL model id, local included). tier_calls keeps its old meaning (paid calls by
      tier name; a pinned model appears under its id)."""
    from datetime import datetime, timezone
    from .freshness import metrics_record
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Codex P1-1: in --local mode the local model is passed as verify_fn (validate_memory can't see
    # it's local), so it reports everything as n_frontier. But NO paid frontier call happened → report
    # it as local so est_frontier_tokens is honest.
    n_local, n_frontier = result.n_local, result.n_frontier
    tier_calls = dict(result.tier_calls)
    model_calls = dict(getattr(result, "model_calls", {}) or {})
    n_frontier_calls = getattr(result, "n_frontier_calls", n_frontier)
    if local_only:
        n_local, n_frontier = result.n_checked, 0
        tier_calls = {}                                # no paid frontier call happened → no tier split
        n_frontier_calls = 0
        model_calls = {_local_model_id(): result.n_checked} if result.n_checked else {}
    metrics_record(_METRICS_PATH, {
        "repo": repo, "file": str(file),               # Codex P2-6: full path, not basename (collision)
        "n_checked": result.n_checked, "n_struck": result.n_struck,
        "n_local": n_local, "n_frontier": n_frontier, "n_skipped": result.n_skipped,
        "tier_calls": tier_calls,                      # frontier model-picker split (haiku/sonnet/opus)
        "struck": [c[:120] for c in result.struck_claims],
        "cached": cached, "routed": routed, "local_only": local_only, "runtime": runtime,
        "judge_mode": judge_mode or getattr(result, "judge_mode", None),
        "judge_model": judge_model,
        "n_screened": getattr(result, "n_screened", 0),
        "n_adjudicated": getattr(result, "n_adjudicated", 0),
        "n_screen_struck": getattr(result, "n_screen_struck", 0),
        "n_adjudicated_struck": getattr(result, "n_adjudicated_struck", 0),
        "n_adjudicate_failed": getattr(result, "n_adjudicate_failed", 0),
        "n_agree": getattr(result, "n_agree", 0),
        "n_compared": getattr(result, "n_compared", 0),
        "agreement": getattr(result, "agreement", None),
        "n_frontier_calls": n_frontier_calls,
        "model_calls": model_calls,
    }, ts=ts)


def _apply_struck_to_file(text, struck, write):
    """On a cache hit with --write, reconstruct the flagged memory from the cached struck-claim list
    (re-strike the exact bullet lines) so --write is never a silent no-op (Codex P2-4)."""
    from .freshness import _STRIKE
    struck_set = {c.strip() for c in struck}
    out = []
    for line in text.splitlines():
        if line.strip() in struck_set:
            marker = line[:len(line) - len(line.lstrip())] + line.lstrip()[0]
            out.append(marker + _STRIKE)
        else:
            out.append(line)
    _Path(write).write_text("\n".join(out))


def _plan_one(repo, file, *, local=False, route=False, runtime=False) -> int:
    """--plan: print the per-claim routing plan (screen model, adjudication policy, whether evidence
    resolves) WITHOUT calling any model. Runtime facts are gathered only with --runtime (read-only
    local commands, same as a real run). Returns the number of claims that would spend a call."""
    from collections import Counter
    from .freshness import plan_validation, gather_runtime_facts
    from .retriever import RepoConfig
    cfg = RepoConfig.load(repo)
    text = _Path(file).read_text()
    runtime_facts = None
    if runtime:
        spec = _runtime_spec(cfg)
        runtime_facts = gather_runtime_facts(spec) if spec else None
    jm, pin, adjudicating, local_screen = _judge_setup(local=local, route=route)
    rows = plan_validation(text, cfg.root, judge_mode=jm if adjudicating else None,
                           local=local_screen and not local, local_only=local,
                           local_model=_local_model_id() if (local or local_screen) else "local",
                           runtime_facts=runtime_facts)
    print(f"validate PLAN (dry run — no model called) — {file} vs live {repo}")
    print(f"  judge_mode={jm or 'unpinned'}  pin={pin or '-'}  local_screen={local_screen and not local}"
          f"  local_only={local}")
    for r in rows:
        ev = "ev" if r["evidence"] else "--"
        print(f"  L{r['line']:<4} {r['type']:13} {ev}  screen={r['screen']:<28} "
              f"adjudicate={r['adjudicate']:<32} {r['claim'][:70]}")
    live = [r for r in rows if r["evidence"] and not r["screen"].startswith("skip")]
    screens = Counter(r["screen"] for r in live)
    always = sum(1 for r in live if r["adjudicate"].startswith("always"))
    maybe = sum(1 for r in live if r["adjudicate"].startswith("if "))
    print(f"\n  {len(rows)} candidate claim(s); {len(live)} would spend a screen call: "
          + ", ".join(f"{m}×{c}" for m, c in screens.most_common()))
    if always or maybe:
        print(f"  adjudication: {always} certain + up to {maybe} conditional call(s) to {pin}")
    return len(live)


def _cmd_validate(args) -> int:
    """Freshness gate: validate a memory/digest's claims against the repo's live code (and runtime
    oracle), flag the stale ones. Measured value: a stale doc misleads the model (corrupted memory
    0.33 vs 0.63 no-memory); striking the contradicted claims recovers it. Auto-wired with a
    fingerprint cache (only re-validates on change). --check exits nonzero if any stale claim is
    found (for pre-commit/cron gating); --all sweeps every registered repo's digest."""
    from .retriever import RepoConfig, REPOS_DIR
    if getattr(args, "plan", False):
        if getattr(args, "all", False):
            for cfgpath in sorted(REPOS_DIR.glob("*.json")):
                try:
                    cfg = RepoConfig.load(cfgpath.stem)
                except Exception as e:  # noqa: BLE001
                    print(f"  {cfgpath.stem}: skipped ({type(e).__name__})"); continue
                if cfg.digest:
                    _plan_one(cfgpath.stem, str(cfg.digest), local=args.local, route=args.route,
                              runtime=args.runtime)
            return 0
        _plan_one(args.repo, args.file, local=args.local, route=args.route, runtime=args.runtime)
        return 0
    # --all: sweep each registered repo against its own digest.
    if getattr(args, "all", False):
        total_stale = 0
        for cfgpath in sorted(REPOS_DIR.glob("*.json")):
            repo = cfgpath.stem
            try:
                cfg = RepoConfig.load(repo)
            except Exception as e:  # noqa: BLE001
                print(f"  {repo}: skipped ({type(e).__name__})"); continue
            if not cfg.digest:
                print(f"  {repo}: no digest configured — skipped"); continue
            n, struck, cached, n_skip = _validate_one(repo, str(cfg.digest), local=args.local,
                                                      route=args.route, runtime=args.runtime,
                                                      use_cache=not args.no_cache)
            total_stale += n
            tag = " (cached)" if cached else ""
            skip = f", {n_skip} skipped (non-derivable)" if n_skip else ""
            print(f"  {repo}: {n} stale claim(s){skip}{tag}")
            for c in struck:
                print(f"      ✗ {c[:100]}")
        print(f"\ntotal stale claims across all repos: {total_stale}")
        return 1 if (args.check and total_stale) else 0
    n, struck, cached, n_skip = _validate_one(args.repo, args.file, local=args.local, route=args.route,
                                              runtime=args.runtime, write=args.write,
                                              use_cache=not args.no_cache)
    verifier = "local model" if args.local else ("routed (value→local, else frontier)" if args.route
                                                 else "frontier model")
    jm, pin, adjudicating, local_screen = _judge_setup(local=args.local, route=args.route)
    if adjudicating:
        verifier = (f"{'local+' if local_screen else ''}routed screen → {pin} adjudicates "
                    f"({'every claim' if jm == 'both' else 'uncleared claims'}; mode={jm})")
    elif jm == "pinned" and not args.local:
        verifier = f"pinned {pin} (CODEQA_JUDGE_MODE=pinned — routing bypassed)"
    tag = " (cached — unchanged since last run)" if cached else ""
    print(f"freshness gate — {args.file} vs live {args.repo}{tag} · verifier: {verifier}")
    skip = f", {n_skip} skipped (non-derivable — no code oracle)" if n_skip else ""
    print(f"  struck {n} claim(s) as contradicted{skip}.")
    for c in struck:
        print(f"  ✗ STALE: {c[:110]}")
    if args.write and not cached:
        print(f"  (validated memory written to {args.write})")
    elif n and not args.write:
        print("  (pass --write PATH to save the flagged memory)")
    return 1 if (args.check and n) else 0


def _cmd_ask(args) -> int:
    if args.verify:
        return _cmd_ask_verified(args)
    from .driver import ask
    a = ask(args.repo, args.question, max_tokens=args.max_tokens,
            enable_thinking=args.think)
    print(a.text)
    print("\n" + "─" * 60)
    print("citations:", ", ".join(a.citations()) or "(none)")
    if a.cached_tokens is not None:
        print(f"cache: {a.cached_tokens}/{a.prompt_tokens} prompt tokens served from cache")
    return 0


def _cmd_ask_verified(args) -> int:
    """Delivery path: ask → verify every citation against the live tree → log impact. Prints each
    cite with a ✓current / ~moved / ✗STALE marker so a stale cite is never presented as clean."""
    from .deliver import deliver
    d = deliver(args.repo, args.question, max_tokens=args.max_tokens,
                enable_thinking=args.think)
    print(d.text)
    print("\n" + "─" * 60)
    if d.citations:
        print("citations the answer emitted (verified):")
        for c in d.citations:
            print(f"  {c.marker():16} {c.cite}")
    else:
        print("citations: (the answer emitted no file:line citation)")
    cv = d.citation_validity()
    if cv is not None:
        n_grounded = sum(1 for c in d.citations if c.verdict == "grounded")
        print(f"citation validity: {cv:.0%} of emitted citations are grounded "
              f"({n_grounded}/{len(d.citations)} cite real code the model was given)")
    if any(c.verdict == "hallucinated" for c in d.citations):
        print("⚠ HALLUCINATED citation(s) — the answer cited a file:line it was never given. Distrust.")
    if any(c.verdict == "stale" for c in d.citations):
        print("⚠ STALE citation(s) — cited source has moved/gone; verify before trusting.")
    behind = f"{d.digest_commits_behind} commits behind HEAD" if d.digest_commits_behind else "current"
    print(f"provenance: repo@{d.git_head or '?'} · digest {behind} · {d.latency_ms}ms"
          + (f" · cache {d.cached_tokens}/{d.prompt_tokens}" if d.cached_tokens is not None else ""))
    return 2 if d.has_problem() else 0  # nonzero on stale/hallucinated so a hook can gate on it


def _cmd_batch(args) -> int:
    from .driver import ask_many
    questions = [ln.strip() for ln in Path(args.file).read_text().splitlines()
                 if ln.strip() and not ln.startswith("#")]
    if not questions:
        print("(no questions in file)")
        return 1
    answers = ask_many(args.repo, questions, max_tokens=args.max_tokens)
    for a in answers:
        print(f"\n### Q: {a.question}\n")
        print(a.text)
        print("citations:", ", ".join(a.citations()) or "(none)")
    if answers and answers[-1].cached_tokens:
        print(f"\n[cache reuse active: last question served "
              f"{answers[-1].cached_tokens}/{answers[-1].prompt_tokens} prompt tokens from cache]")
    return 0


def _cmd_ab(args) -> int:
    """Run the impact A/B: fresh vs stale-N vs absent digest, holding retrieval fixed. Answers via
    LOCAL Ornith (≈0 paid tokens); scores the deterministic groundedness axis; prints the
    pre-registered build/don't-build decision."""
    from .ab import (ab_run, build_variants, decide, real_ask_fn, real_retrieve_fn,
                     retrieval_is_reproducible, write_ab_jsonl)
    from .retriever import RepoConfig
    questions = [ln.strip() for ln in Path(args.file).read_text().splitlines()
                 if ln.strip() and not ln.startswith("#")]
    if not questions:
        print("(no questions in file)")
        return 1
    cfg = RepoConfig.load(args.repo)
    retrieve_fn = real_retrieve_fn(args.repo)
    # PREFLIGHT (Codex A/B-F2): retrieval must be reproducible or the frozen-context design is invalid.
    if not retrieval_is_reproducible(cfg.root, questions[0], retrieve_fn):
        print("⚠ ABORT: retrieval is not reproducible for the first question — the frozen-context "
              "control is void. Fix retrieval determinism before running the A/B.")
        return 3
    variants = build_variants(args.repo, stale_commits=args.stale,
                              warn=lambda m: print(f"⚠ {m}"))
    if args.stale and not any(v.name.startswith("stale") for v in variants):
        print("⚠ NOTE: no stale variant was built, so the 'staleness hurts' half of the decision "
              "rule cannot be evaluated — expect CANNOT DECIDE. (See the SKIPPED reason above.)")
    judge_fn = None
    if args.judge:
        from .judge import judge_preflight, opus_judge_fn
        # PREFLIGHT the credential BEFORE the expensive N×V local answering — a bad/missing token
        # otherwise wastes every answer, then fails every grade (the '0/5 judged' symptom).
        ok, msg = judge_preflight(cfg.root)
        if not ok:
            print(f"⚠ ABORT: the frontier judge is not reachable — {msg}\n"
                  "  The frontier judge is opt-in: export CODEQA_JUDGE_BASE=<https-url> for an\n"
                  "  Anthropic-messages endpoint you control (+ CODEQA_JUDGE_AUTH if it needs one).\n"
                  "  Or use the LOCAL verifier (no frontier call), or run without --judge for\n"
                  "  diagnostics-only. codeqa does NOT grade through an agentic CLI.")
            return 3
        # blinded Opus correctness judge — grades against the LIVE tree at cfg.root (cross-validation)
        judge_fn = opus_judge_fn(cfg.root)
    axis = "Opus correctness judge (primary)" if judge_fn else "NO judge (diagnostics only)"
    print(f"A/B: {len(questions)} questions × {len(variants)} variants "
          f"({', '.join(v.name for v in variants)}) — local Ornith answerer · {axis}")
    result = ab_run(cfg.root, questions, variants,
                    retrieve_fn=retrieve_fn,
                    ask_fn=real_ask_fn(args.repo, max_tokens=args.max_tokens),
                    judge_fn=judge_fn)
    if result.get("judge_errors"):
        print(f"\n⚠ {result['judge_errors']} judge call(s) FAILED (network/auth/protocol) — those "
              "answers are unscored; correctness means are over successful grades only. If ALL "
              "failed, check the judge credential (CODEQA_JUDGE_AUTH / CODEQA_JUDGE_APIM_KEY).")
    # Per-question detail FIRST — at small n the aggregate mean hides which question moved (one
    # question swings a 3-question mean by 0.33). Read the spread before trusting the verdict.
    if result.get("per_question"):
        print("\nper question × variant:")
        by_q: dict[str, list] = {}
        for rec in result["per_question"]:
            by_q.setdefault(rec["question"], []).append(rec)
        for q, recs in by_q.items():
            print(f"  Q: {q}")
            for rec in recs:
                c = (f"{rec['correctness']:.2f}" if rec["correctness"] is not None
                     else ("ERR" if rec["judge_error"] else "n/a"))
                g = f"{rec['groundedness']:.2f}" if rec["groundedness"] is not None else "uncited"
                cites = ",".join(rec["cited_files"]) or "(none)"
                print(f"    {rec['variant']:30} correctness={c}  grounded={g}  cited={cites}")
    print("\nper variant:")
    n_paired = next((p.get("n_paired") for p in result["per_variant"]
                     if p.get("n_paired") is not None), None)
    for p in result["per_variant"]:
        c = f"{p['mean_correctness']:.3f}" if p["mean_correctness"] is not None else "n/a (no judge)"
        judged = f"{p.get('n_judged', 0)}/{p['n_questions']}"
        # paired = the mean decide() actually uses: over questions ALL variants scored (honest compare)
        pc = (f"{p['paired_correctness']:.3f}" if p.get("paired_correctness") is not None else "n/a")
        g = f"{p['mean_groundedness']:.3f}" if p["mean_groundedness"] is not None else "n/a"
        cov = f"{p['citation_coverage']:.0%}" if p["citation_coverage"] is not None else "n/a"
        print(f"  {p['variant']:14} paired={pc} (n={p.get('n_paired', 0)})  unpaired={c} "
              f"(judged {judged})  [secondary: grounded={g} cov={cov} "
              f"uncited={p['n_uncited']}/{p['n_questions']}]")
    judge_ran = any(p.get("n_judged") for p in result["per_variant"])
    if judge_ran and n_paired is not None and n_paired < result["per_variant"][0]["n_questions"]:
        lost = result["per_variant"][0]["n_questions"] - n_paired
        print(f"  NOTE: {lost} question(s) not scored by all variants (judge failures) → paired set "
              f"is n={n_paired}. The verdict uses the paired means (the honest cross-variant compare).")
    if args.jsonl:
        write_ab_jsonl(args.jsonl, result)
        print(f"\n(per-question + per-variant records written to {args.jsonl})")
    d = decide(result["per_variant"], margin=args.margin)
    verdict = {True: "BUILD", False: "DO NOT BUILD", None: "CANNOT DECIDE"}[d["build"]]
    print(f"\nDECISION: {verdict} the dynamic index")
    print(f"  {d['rationale']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="codeqa", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    _MT_HELP = "answer budget; default None -> repo config's max_tokens, else 1200"
    pa = sub.add_parser("ask", help="ask one question")
    pa.add_argument("repo"); pa.add_argument("question")
    pa.add_argument("--max-tokens", type=int, default=None, help=_MT_HELP)
    pa.add_argument("--think", action="store_true", help="enable model thinking (slow; synthesis only)")
    pa.add_argument("--verify", action="store_true",
                    help="verify every citation against the working tree + log impact (delivery mode)")
    pa.set_defaults(func=_cmd_ask)

    pb = sub.add_parser("batch", help="ask questions from a file (one per line)")
    pb.add_argument("repo"); pb.add_argument("file")
    pb.add_argument("--max-tokens", type=int, default=None, help=_MT_HELP)
    pb.set_defaults(func=_cmd_batch)

    pr = sub.add_parser("retrieve", help="show retrieved chunks only (no Ornith call)")
    pr.add_argument("repo"); pr.add_argument("question")
    pr.set_defaults(func=_cmd_retrieve)

    pab = sub.add_parser("ab", help="impact A/B: does digest staleness degrade answers? "
                                    "(local Ornith; deterministic groundedness axis)")
    pab.add_argument("repo"); pab.add_argument("file", help="one question per line")
    pab.add_argument("--stale", action="append", default=[], metavar="REF",
                     help="a git ref for a stale digest variant (repeatable, e.g. HEAD~20)")
    pab.add_argument("--max-tokens", type=int, default=1200)
    pab.add_argument("--margin", type=float, default=0.10, help="decision margin (default 0.10)")
    pab.add_argument("--jsonl", metavar="PATH",
                     help="write per-question + per-variant records to PATH for offline analysis")
    pab.add_argument("--judge", action="store_true",
                     help="score prose correctness with a blinded Opus judge (the PRIMARY decision "
                          "axis; needs frontier creds). Without it, the run is diagnostics-only and "
                          "the decision is CANNOT DECIDE.")
    pab.set_defaults(func=_cmd_ab)

    pl = sub.add_parser("repos", help="list registered repos")
    pl.set_defaults(func=_cmd_repos)

    pd = sub.add_parser("doctor", help="post-install validation: per-repo health "
                                       "(config parses, root exists, code reachable, digest)")
    pd.add_argument("--check", action="store_true",
                    help="exit nonzero if any repo is unhealthy (for install/CI gating)")
    pd.set_defaults(func=_cmd_doctor)

    pv = sub.add_parser("validate", help="freshness gate: check a memory/digest's claims against a "
                                         "repo's live code (+ runtime oracle) and flag the stale ones")
    pv.add_argument("repo", nargs="?", help="registered repo (omit with --all)")
    pv.add_argument("file", nargs="?", help="the memory/digest markdown to validate (omit with --all)")
    pv.add_argument("--write", metavar="PATH", help="write the validated (flagged) memory to PATH")
    pv.add_argument("--local", action="store_true",
                    help="use the LOCAL model as verifier (default: frontier — it clears the "
                         "default-value→state inference the local model hedges on)")
    pv.add_argument("--route", action="store_true",
                    help="ROUTE by claim type: VALUE claims → free local verifier, INFERENCE/RUNTIME "
                         "→ frontier (measured −62%% frontier tokens, no accuracy loss vs all-frontier)")
    pv.add_argument("--runtime", action="store_true",
                    help="also check present-tense RUNTIME-state claims against the repo's runtime "
                         "oracle (files + /status + read-only commands), declared as 'runtime_oracle'")
    pv.add_argument("--all", action="store_true",
                    help="sweep EVERY registered repo against its own digest (auto-wire)")
    pv.epilog = ("CODEQA_JUDGE_MODEL: when set, the default CODEQA_JUDGE_MODE is now 'screen' — each "
                 "claim goes to its routed tier first and the pinned model only adjudicates claims "
                 "the screen did not clear. A screen SUPPORTED is FINAL (e.g. a haiku-routed VALUE "
                 "claim is never re-judged by the pin); set CODEQA_JUDGE_MODE=both to calibrate the "
                 "screen against the pin, or =pinned for the legacy every-claim-to-the-pin behaviour. "
                 "If the adjudicator call fails, the screen verdict is kept and the run is not cached.")
    pv.add_argument("--check", action="store_true",
                    help="exit nonzero if any stale claim is found (for pre-commit / cron gating)")
    pv.add_argument("--no-cache", action="store_true",
                    help="ignore the fingerprint cache and re-validate even if unchanged")
    pv.add_argument("--plan", action="store_true",
                    help="DRY RUN: print the per-claim routing plan (screen model, adjudication "
                         "policy, evidence) without calling any model; writes no cache/metrics")
    pv.set_defaults(func=_cmd_validate)

    pg = sub.add_parser("ground", help="grounding oracle: check the file:line citations in a "
                                       "finding/report against live registered-repo code")
    pg.add_argument("file", nargs="?",
                    help="finding/report text file to ground (default: read stdin)")
    pg.add_argument("--json", action="store_true", help="emit the verdict as JSON")
    pg.add_argument("--check", action="store_true",
                    help="exit 2 if any citation is stale (a provable defect), for gating a review")
    pg.set_defaults(func=_cmd_ground)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
