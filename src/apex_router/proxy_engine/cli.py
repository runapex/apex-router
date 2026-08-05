"""apex CLI — `apex serve`, `apex doctor`. Console entrypoint (pyproject [project.scripts]).

Kept intentionally small for M0: serve the proxy, and a doctor that verifies the upstream is
reachable and the config is sane (the seed of the M8 routing self-check). Config/knob
subcommands land with the registry (M4/M7).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from apex_router.proxy_engine.config import APEX_VERSION, CONFIG


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from apex_router.proxy_engine.config import Config

    # Only override fields the user actually passed (else Config(host=None) clobbers the default).
    overrides = {}
    if args.host:
        overrides["host"] = args.host
    if args.port:
        overrides["port"] = args.port
    cfg = Config(**overrides) if overrides else CONFIG
    cfg.ensure_home()
    print(
        f"apex {APEX_VERSION} serving on http://{cfg.host}:{cfg.port} "
        f"→ anthropic={cfg.anthropic_upstream}",
        file=sys.stderr,
    )
    uvicorn.run(
        "apex_router.proxy_engine.proxy.app:create_app",
        factory=True,
        host=cfg.host,
        port=cfg.port,
        log_level=args.log_level,
        access_log=False,
    )
    return 0


def _doctor(args: argparse.Namespace) -> int:
    """apex doctor — cache-cost report from telemetry (WP2). `--check` runs the old config/upstream
    reachability probe instead; `--json` emits the report dict; `--session ID` filters one."""
    import json as _json
    from pathlib import Path

    cfg = CONFIG
    if getattr(args, "check", False):
        return _doctor_reachability(cfg)

    tel = Path(cfg.telemetry_path)
    if not tel.exists():
        print(f"apex doctor: no telemetry at {tel} — run the proxy first (or `apex doctor --check` "
              "for config/reachability).", file=sys.stderr)
        return 1

    from apex_router.proxy_engine.readout.doctor import build_report, format_report, load_rows
    rows = load_rows(str(tel))
    if getattr(args, "session", None):
        rows = [d for d in rows if d.get("session_id") == args.session]

    if getattr(args, "divergence", False):
        return _doctor_divergence(rows)

    report = build_report(rows)
    if getattr(args, "json", False):
        report = {**report, "calibration": _calibration_lines(rows, as_dict=True)}
        print(_json.dumps(report, indent=2, default=str))
    else:
        print(format_report(report))
        for line in _calibration_lines(rows):
            print(line)
    return 0


def _calibration_lines(rows: list, *, as_dict: bool = False):
    """R1 calibration line per wire (Spec 2) — the instrument auditing itself against the provider's
    bill. Emits a number when the fit holds, else an honest refusal naming why. Per-endpoint (the
    wire y-semantics differ). Returns text lines, or {wire: fit_dict} when as_dict."""
    from apex_router.proxy_engine.analytics.r1 import fit_r1, format_calibration_line
    wires = sorted({d.get("endpoint_id") for d in rows if d.get("endpoint_id")})
    if as_dict:
        return {w: fit_r1(rows, endpoint=w).to_dict() for w in wires}
    return [format_calibration_line(fit_r1(rows, endpoint=w)) for w in wires]


def _doctor_divergence(rows: list) -> int:
    """apex doctor --divergence — per-event structural diagnosis of cache breaks (Spec 1). The WP3
    divergence DETECTOR + frontier-byte capture (#14) are not wired yet, so there are no captured
    events to classify from live telemetry today. Rather than fabricate events, state that honestly:
    the classifier (apex_router.proxy_engine.readout.signature) is built and tested and activates the moment a detector
    feeds it DivergenceContexts. This is the seam, reported truthfully — no guessed output."""
    from apex_router.proxy_engine.readout.signature import format_divergence_report
    # No detector = no events. `format_divergence_report([])` prints the header + "no events" line,
    # and names which wire is classified vs pending the suffix matcher (#13, Codex grouping).
    print(format_divergence_report([], classified_wires=("anthropic",), pending_wires=("openai",)))
    print("\nnote: the divergence DETECTOR (#14 frontier-byte capture) is not wired yet — the "
          "signature classifier is ready and will populate this report once events are captured.",
          file=sys.stderr)
    return 0


def _doctor_reachability(cfg) -> int:
    import asyncio

    import httpx

    print(f"apex {APEX_VERSION}")
    print(f"  home:              {cfg.home}")
    print(f"  port:              {cfg.port}  (the reference proxy A/B peer on 8787)")
    print(f"  anthropic upstream:{cfg.anthropic_upstream}")

    async def _probe() -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(cfg.anthropic_upstream, timeout=8)
                return True, f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001 - doctor reports any failure verbatim
            return False, f"{type(e).__name__}: {e}"

    ok, detail = asyncio.run(_probe())
    print(f"  upstream reachable:{'yes' if ok else 'NO'}  ({detail})")
    return 0 if ok else 1


def _compile(args: argparse.Namespace) -> int:
    """Compile a signed policy bundle from a replay corpus and write it to `policy_path`.

    This is an OFFLINE (compiler-plane) command — it imports `apex_router.proxy_engine.tuner`, which the hot path may
    not. The proxy then loads the written bundle via the plane-clean `load_verified` at startup. The
    bundle is what shadow mode enforces (in prediction only) and what the wire eventually emits, so
    compilation and serving are deliberately separate binaries touching the same signed artifact.
    """
    import json
    import os
    import tempfile
    from pathlib import Path

    from apex_router.proxy_engine.config import Config
    from apex_router.proxy_engine.policy import EvidenceBundle
    from apex_router.proxy_engine.tuner.cachesim import Pricing
    from apex_router.proxy_engine.tuner.compiler import _compiler_hash, compile_policy
    from apex_router.proxy_engine.tuner.composition import composition_hash, diagnose
    from apex_router.proxy_engine.tuner.evidence import EvidenceError, build_evidence_manifest, sha256_file
    from apex_router.proxy_engine.tuner.sensitivity import DEFAULT_BAND

    cfg = CONFIG
    cfg.ensure_home()
    corpus, stats = _build_corpus(args)
    if not corpus:
        print("apex compile: empty corpus — nothing to compile", file=sys.stderr)
        return 1
    # compiled_at is a caller-supplied input, never now() (reproducibility); default to the corpus's
    # last-request timestamp so re-compiling the same frozen corpus is byte-identical.
    compiled_at = float(args.compiled_at) if args.compiled_at is not None else _corpus_stamp(corpus)
    # `apex compile` writes a bundle a live proxy loads → it is the EVIDENCE-GRADE signing path, so
    # it passes the corpus provenance and compiles evidence-grade. A `--limit-sessions` probe is NOT
    # canonical: rather than sign a bundle from a truncated (sorted-filename-biased) corpus — the
    # instrument that produced "admitted: NONE" — refuse it here with a clear message. Freeze the
    # canonical corpus or drop --limit-sessions to sign.
    from apex_router.proxy_engine.tuner.compiler import CorpusProvenance

    provenance = CorpusProvenance.from_stats(stats)
    if not provenance.canonical:
        print(
            f"apex compile: REFUSING to sign a bundle from a non-canonical corpus "
            f"({provenance.source}). A --limit-sessions cap biases toward the first-N sessions "
            f"by sorted filename (the 'admitted: NONE' instrument). Drop --limit-sessions (full "
            f"population) or freeze the canonical corpus to sign.",
            file=sys.stderr,
        )
        return 2
    pricing = Pricing()
    band = DEFAULT_BAND
    policy_corpus_hash = composition_hash(diagnose(corpus))
    compiler_hash = _compiler_hash(pricing, band)
    repo_root = Path(__file__).resolve().parent.parent
    validators_path = repo_root / "apex" / "pipeline" / "validators.py"
    try:
        manifest = build_evidence_manifest(
            repo_root=repo_root,
            corpus=corpus,
            policy_corpus_hash=policy_corpus_hash,
            compiler_hash=compiler_hash,
            compiled_at=compiled_at,
            corpus_source_files=getattr(stats, "source_files", ()),
            gate_report_paths=args.gate_report,
            validators={"gutter_floor_v1": sha256_file(validators_path)[:16]},
            require_clean_tree=not args.allow_dirty_source,
        )
    except EvidenceError as exc:
        print(f"apex compile: REFUSING evidence bundle: {exc}", file=sys.stderr)
        return 2
    result = compile_policy(
        corpus,
        version=args.version,
        compiled_at=compiled_at,
        pricing=pricing,
        band=band,
        evidence_grade=True,
        corpus_provenance=provenance,
        evidence_manifest_hash=manifest.digest,
    )
    policy = result.policy
    out_path = Config(policy_path_env=args.out).policy_path if args.out else cfg.policy_path
    bundle = EvidenceBundle(policy=policy, manifest=manifest.to_dict(), evidence=result.evidence)
    # Self-verify BEFORE publication, then replace atomically so the proxy can never observe a
    # half-written production artifact.
    EvidenceBundle.load_verified(bundle.to_dict())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{out_path.name}.", dir=out_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(bundle.to_dict(), f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    enabled = sorted(
        f"{c}/{st}" for c, strata in policy.rules.items() for st, r in strata.items() if r.enabled
    )
    print(
        f"apex compile: {stats.n_sessions} sessions / {stats.n_requests} reqs → {out_path}",
        file=sys.stderr,
    )
    print(f"  enabled cells:   {enabled or 'NONE'}", file=sys.stderr)
    print(f"  corpus_hash:     {policy.corpus_hash}", file=sys.stderr)
    print(f"  evidence_hash:   {policy.evidence_manifest_hash}", file=sys.stderr)
    print(
        f"  expected Δ/sess: {policy.expected.delta_dollars_per_session:.1f} tok-eq",
        file=sys.stderr,
    )
    print(
        f"  has_active:      {policy.has_active_policy()} (shadow-only until True)", file=sys.stderr
    )
    return 0


def _build_corpus(args: argparse.Namespace):
    """Build the replay corpus from a Claude Code project's transcripts (offline).

    The corpus builder lives in the source-tree `fixtures/` package, which the wheel does NOT ship
    (`pyproject.toml` packages `apex` only). So `apex compile` is a SOURCE-CHECKOUT tool today —
    run it from a clone, not an installed wheel. Fail with a clear, actionable message rather than a
    bare ImportError if `fixtures` isn't importable (packaging it into `apex.corpus` is a separate
    refactor tracked for when compile becomes an end-user command)."""
    try:
        from fixtures.build_replay_corpus import build_corpus, build_streaming_corpus
    except ModuleNotFoundError as e:
        # The error message is the ONLY place a stranger meets the admission doctrine — state it,
        # don't let it read as a packaging bug. External installs run measure-only BY DESIGN: no
        # policy is compiled or enforced, so `apex compile` is not part of the end-user path.
        raise SystemExit(
            "apex compile is not available in an installed wheel — and by design you don't need "
            "it: external deployments run MEASURE-ONLY (no admitted transforms, no policy "
            "enforced), so there is nothing to compile. Compilation is the evidence-toolchain step "
            "for the maintainer (it needs the source-tree `fixtures` replay-corpus builder + the "
            f"tuner extras); run it from a source checkout of the apex repo to build policy. [{e}]"
        ) from e

    builder = build_corpus if getattr(args, "batch_corpus", False) else build_streaming_corpus
    return builder(
        args.project,
        limit_sessions=args.limit_sessions,
        min_turns=args.min_turns,
        exclude_contaminated=True,
    )


def _corpus_stamp(corpus) -> float:
    """Latest request timestamp in the corpus — a deterministic `compiled_at` default."""
    return float(max((r.ts for r in corpus), default=0.0))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apex", description="apex context-optimizing proxy")
    parser.add_argument("--version", action="version", version=f"apex {APEX_VERSION}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="run the proxy")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--log-level", default="warning")
    p_serve.set_defaults(func=_serve)

    p_doctor = sub.add_parser("doctor", help="cache-cost report from telemetry (--check = config)")
    p_doctor.add_argument("--json", action="store_true", help="emit the report as JSON")
    p_doctor.add_argument("--session", default=None, help="filter to one session id")
    p_doctor.add_argument("--check", action="store_true",
                          help="run config + upstream reachability instead of the cost report")
    p_doctor.add_argument("--divergence", action="store_true",
                          help="per-event structural diagnosis of cache breaks (signature report)")
    p_doctor.set_defaults(func=_doctor)

    p_compile = sub.add_parser("compile", help="compile a signed policy bundle from a corpus")
    p_compile.add_argument(
        "project",
        help="Claude Code project dir (under ~/.claude/projects; a "
        "leading-dash name needs a `--` guard: `apex compile -- -Users-...`)",
    )
    p_compile.add_argument("--out", default=None, help="output path (default ~/.apex/policy.json)")
    p_compile.add_argument("--version", type=int, default=1)
    p_compile.add_argument("--limit-sessions", type=int, default=None, dest="limit_sessions")
    p_compile.add_argument("--min-turns", type=int, default=3, dest="min_turns")
    p_compile.add_argument(
        "--compiled-at",
        type=float,
        default=None,
        dest="compiled_at",
        help="reproducibility stamp (default: corpus's last-request ts)",
    )
    p_compile.add_argument(
        "--gate-report",
        action="append",
        default=[],
        help="verified behavioral-gate transcript to bind (repeatable)",
    )
    p_compile.add_argument(
        "--allow-dirty-source",
        action="store_true",
        help="development escape hatch; production evidence should be built from a clean Git tree",
    )
    p_compile.add_argument(
        "--batch-corpus",
        action="store_true",
        help="debug path that materializes every growing prefix (not for large sessions)",
    )
    p_compile.set_defaults(func=_compile)

    p_readout = sub.add_parser("readout", help="shadow readout (R1 + read:write + json/xl)")
    p_readout.add_argument(
        "--telemetry", default=None, help="telemetry jsonl (default ~/.apex/telemetry.jsonl)"
    )
    p_readout.add_argument(
        "--live",
        action="store_true",
        help="mark provenance live-shadow (real readout); omit for a dry-run",
    )
    p_readout.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit the readout as JSON instead of text",
    )
    p_readout.add_argument(
        "--drift-threshold",
        type=float,
        default=0.0,
        dest="drift_threshold",
        help="residual-trend threshold for the R1 drift alarm (0 = off)",
    )
    p_readout.set_defaults(func=_readout)

    p_ask = sub.add_parser(
        "ask", help="grounded, cited code Q&A over the local the local code-QA harness (delivery mode: "
                    "verifies every citation against the working tree + logs impact)")
    p_ask.add_argument("question", help="natural-language question about the code")
    p_ask.add_argument("--repo", default=None,
                       help="repo name (default: resolved from the current directory)")
    p_ask.add_argument("--max-tokens", type=int, default=1200, dest="max_tokens")
    p_ask.add_argument("--no-verify", action="store_true",
                       help="skip citation verification + impact logging (raw harness answer)")
    p_ask.set_defaults(func=_ask)

    args = parser.parse_args(argv)
    return args.func(args)


# Where the codeqa harness lives (a separate repo + venv; apex shells to it rather than importing it,
# so the two dependency graphs stay decoupled). Set CODEQA_DIR / CODEQA_PY to enable `apex ask`;
# unset, the subcommand reports it needs configuring rather than guessing a path.
_CODEQA_DIR = os.environ.get("CODEQA_DIR")
_CODEQA_PY = os.environ.get("CODEQA_PY", sys.executable)


def _ask(args: argparse.Namespace) -> int:
    """Delivery surface for semantic code search. Shells to the codeqa harness for the current repo,
    which verifies every citation against the working tree and logs an impact record. Repo resolves
    from cwd unless --repo is given. The harness's exit code passes through (2 = a stale citation)."""
    import subprocess

    codeqa = Path(_CODEQA_DIR) / "codeqa"
    if not codeqa.exists():
        print(f"codeqa harness not found at {_CODEQA_DIR} (set CODEQA_DIR). "
              "Semantic search is an out-of-tree dev tool; see the Phase-0 spec.")
        return 3

    repo = args.repo
    if repo is None:
        # Resolve the repo from cwd via the harness's own resolver (single source of truth).
        probe = subprocess.run(
            [_CODEQA_PY, "-c",
             "import sys; sys.path.insert(0, %r); "
             "from codeqa.deliver import resolve_repo_from_cwd; "
             "r = resolve_repo_from_cwd(); print(r or '')" % _CODEQA_DIR],
            capture_output=True, text=True, cwd=os.getcwd())
        repo = (probe.stdout or "").strip()
        if not repo:
            print("could not resolve a registered repo from the current directory; "
                  "pass --repo <name> (registered repos: run `apex ask --repo '' ...` to list).")
            return 3

    cmd = [_CODEQA_PY, "-m", "codeqa.cli", "ask", repo, args.question,
           "--max-tokens", str(args.max_tokens)]
    if not args.no_verify:
        cmd.append("--verify")
    return subprocess.run(cmd, cwd=_CODEQA_DIR).returncode


def _readout(args: argparse.Namespace) -> int:
    """First-week shadow readout — R1 wire-usage regression + read:write dist + json/xl watch. Reads
    the telemetry jsonl (analytics plane). Provenance defaults to a fixture-style dry-run unless
    --live is passed (the operator asserts real shadow traffic after the wire switch)."""
    import json

    from apex_router.proxy_engine.tuner.readout import build_readout, format_readout

    cfg = CONFIG
    path = args.telemetry or str(cfg.telemetry_path)
    provenance = "live-shadow" if args.live else f"fixture:{path}"
    readout = build_readout(path, provenance=provenance, drift_trend_threshold=args.drift_threshold)
    if args.as_json:
        print(json.dumps(readout.to_dict(), indent=2))
    else:
        print(format_readout(readout))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
