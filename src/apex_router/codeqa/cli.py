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
    mode = f"local={local}|route={route}|runtime={bool(runtime_facts)}|" + \
        hashlib.sha256((runtime_facts or "").encode("utf-8", "replace")).hexdigest()[:12]
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
    # --route: supply the local verifier so VALUE claims use it (frontier reserved for INFERENCE/
    # RUNTIME) — measured −62% frontier tokens with no accuracy loss vs all-frontier.
    local_vf = _local_verifier() if (route and not local) else None
    result = validate_memory(text, cfg.root, verify_fn=_make_verifier(local),
                             local_verify_fn=local_vf, runtime_facts=runtime_facts)
    if write:
        _Path(write).write_text(result.text)
    if use_cache:
        cache[ckey] = {"fp": fp, "n_struck": result.n_struck, "struck": result.struck_claims,
                       "n_skipped": result.n_skipped}
        try:
            _FRESHNESS_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _FRESHNESS_CACHE.write_text(_json.dumps(cache, indent=2))
        except OSError:
            pass
    _emit_metrics(repo, file, result, cached=False, routed=bool(local_vf), local_only=local,
                  runtime=bool(runtime_facts))
    return result.n_struck, result.struck_claims, False, result.n_skipped


_METRICS_PATH = _Path("~/.codeqa/validate_metrics.jsonl").expanduser()


def _emit_metrics(repo, file, result, *, cached, routed, local_only, runtime):
    """Append one benchmark line per validation run, so runs are differentiable/comparable over time
    (which repo, how many struck/local/frontier/skipped, token cost, whether routed/cached)."""
    from datetime import datetime, timezone
    from .freshness import metrics_record
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Codex P1-1: in --local mode the local model is passed as verify_fn (validate_memory can't see
    # it's local), so it reports everything as n_frontier. But NO paid frontier call happened → report
    # it as local so est_frontier_tokens is honest.
    n_local, n_frontier = result.n_local, result.n_frontier
    tier_calls = dict(result.tier_calls)
    if local_only:
        n_local, n_frontier = result.n_checked, 0
        tier_calls = {}                                # no paid frontier call happened → no tier split
    metrics_record(_METRICS_PATH, {
        "repo": repo, "file": str(file),               # Codex P2-6: full path, not basename (collision)
        "n_checked": result.n_checked, "n_struck": result.n_struck,
        "n_local": n_local, "n_frontier": n_frontier, "n_skipped": result.n_skipped,
        "tier_calls": tier_calls,                      # frontier model-picker split (haiku/sonnet/opus)
        "struck": [c[:120] for c in result.struck_claims],
        "cached": cached, "routed": routed, "local_only": local_only, "runtime": runtime,
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


def _cmd_validate(args) -> int:
    """Freshness gate: validate a memory/digest's claims against the repo's live code (and runtime
    oracle), flag the stale ones. Measured value: a stale doc misleads the model (corrupted memory
    0.33 vs 0.63 no-memory); striking the contradicted claims recovers it. Auto-wired with a
    fingerprint cache (only re-validates on change). --check exits nonzero if any stale claim is
    found (for pre-commit/cron gating); --all sweeps every registered repo's digest."""
    from .retriever import RepoConfig, REPOS_DIR
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

    pa = sub.add_parser("ask", help="ask one question")
    pa.add_argument("repo"); pa.add_argument("question")
    pa.add_argument("--max-tokens", type=int, default=1200)
    pa.add_argument("--think", action="store_true", help="enable Ornith thinking (slow; synthesis only)")
    pa.add_argument("--verify", action="store_true",
                    help="verify every citation against the working tree + log impact (delivery mode)")
    pa.set_defaults(func=_cmd_ask)

    pb = sub.add_parser("batch", help="ask questions from a file (one per line)")
    pb.add_argument("repo"); pb.add_argument("file")
    pb.add_argument("--max-tokens", type=int, default=1200)
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
    pv.add_argument("--check", action="store_true",
                    help="exit nonzero if any stale claim is found (for pre-commit / cron gating)")
    pv.add_argument("--no-cache", action="store_true",
                    help="ignore the fingerprint cache and re-validate even if unchanged")
    pv.set_defaults(func=_cmd_validate)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
