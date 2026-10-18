"""Mesh worker jobs: the shared contract (docs/superpowers/specs/2026-10-17-mesh-worker-design.md).

Pure data + file layout. No network, no subprocess. Used by the sender CLI (submit/result/jobs),
the HTTP server (GET /mesh/v1/jobs, /mesh/v1/results) and the worker (mesh worker).

Layout under ``identity.mesh_home()``:

  outbox/<id>.job           sender: the signed job envelope (bytes), mode 0600
  inbox/<id>.json           sender: fetched result (verified body), mode 0600
  worker/seen.jsonl         worker: one id per line; a job id runs at most once
  worker/paused             worker: present = paused
  worker/running.json       worker: {"id", "step", "effort", "model", "started"} while a job runs
  worker/results/<id>.json  worker: result body (served to the sender)
  worker/jobs/<id>/<step>   worker: worktrees
  worker/steps.jsonl        worker: one row per ladder step
  worker.json               worker: {"repos": {"name": "/abs/path"}} allowlist
  worker-settings.json      worker: Claude Code settings for the agent (--settings)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from . import identity, protocol
from .identity import Identity

JOBS_PATH = "/mesh/v1/jobs"
RESULTS_PATH = "/mesh/v1/results"
BUNDLE_PATH = "/mesh/v1/bundle"
SRC_MESH = "mesh"                           # job["src"]: the worker gets the repo from the sender
MAX_BUNDLE_BYTES = 24 * 1024 * 1024         # one git bundle (raw); base64 + envelope stays < 34 MiB

MAX_TASK_BYTES = 256 * 1024                 # room for a whole report or diff (mesh xval)
MAX_CHECK_BYTES = 1024
MAX_PATCH_BYTES = 512 * 1024
MAX_REPORT_BYTES = 64 * 1024
MAX_JOB_ENVELOPE_BYTES = 2 * 1024 * 1024    # one signed job: 256 KiB task, up to 6x JSON escaping
MAX_OFFERED = 8                             # jobs per GET /jobs response
# Raw envelope bytes per GET /jobs response. The response escapes each envelope string again
# (at most 2x), so 3 MiB stays under the client's MAX_JOBS_RESPONSE_BYTES (sender.py, 8 MiB).
# One job is always offered, even when it alone is larger.
JOBS_BUDGET = 3 * 1024 * 1024
MAX_EXPIRES_S = 3600
DEFAULT_EXPIRES_S = 3600

# Effort ladder. "opus" = Opus at max effort; every other step runs Sonnet.
# Model aliases, not IDs: the worker's own Claude Code resolves them, so a Foundry or Bedrock
# deployment with other model names still runs (a pinned ID fails there with claude_error).
LADDER = ("medium", "high", "xhigh", "max", "opus")
CEILINGS = ("high", "xhigh", "max", "opus")
DEFAULT_CEILING = "high"
SONNET = "sonnet"
OPUS = "opus"

STATUSES = ("running", "done", "failed", "rejected")

_ID_RE = re.compile(r"\A[0-9a-f]{16}\Z")
_NODE_RE = re.compile(r"\A[0-9a-f]{32}\Z")
_REPO_RE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")
_REF_RE = re.compile(r"\A(?:[0-9a-f]{40}|[A-Za-z0-9._/-]{1,200})\Z")


class JobError(ValueError):
    """A job or result that fails a contract check. The message is a short reason code."""


def new_id() -> str:
    return secrets.token_hex(8)


def valid_id(s: Any) -> bool:
    return isinstance(s, str) and bool(_ID_RE.match(s))


def valid_sha(ref: Any) -> bool:
    return isinstance(ref, str) and bool(re.fullmatch(r"[0-9a-f]{40}", ref))


def valid_ref(ref: Any) -> bool:
    """Branch/tag/sha. No leading '-' (git option), no '..', no '//' and no trailing '/' or '.lock'."""
    if not isinstance(ref, str) or not _REF_RE.match(ref):
        return False
    return not (ref.startswith(("-", "/")) or ref.endswith(("/", ".lock", "."))
                or ".." in ref or "//" in ref)


def ladder(ceiling: str) -> tuple[str, ...]:
    """Steps from medium up to and including ``ceiling``."""
    if ceiling not in CEILINGS:
        raise JobError("bad_max_effort")
    return LADDER[: LADDER.index(ceiling) + 1]


def step_model(step: str) -> tuple[str, str]:
    """(model, claude --effort level) for one ladder step."""
    if step == "opus":
        return OPUS, "max"
    if step not in LADDER:
        raise JobError("bad_step")
    return SONNET, step


# -- job envelope -------------------------------------------------------------------------------

def make_job(ident: Identity, *, to: str, repo: str, ref: str, task: str, check: str = "",
             max_effort: str = DEFAULT_CEILING, expires_s: int = DEFAULT_EXPIRES_S,
             now: float | None = None, src: str | None = None) -> tuple[str, bytes]:
    """Build and sign one job. Returns (id, envelope bytes). Raises JobError on a bad field.

    ``src="mesh"``: the worker fetches the repo as a git bundle from this node (BUNDLE_PATH), not
    from its own ``origin``. Only send it to a peer whose card has the ``bundle`` cap: an older
    worker rejects the extra key (``bad_fields``).
    """
    t = time.time() if now is None else float(now)
    body = {"id": new_id(), "from": ident.node_id, "to": to, "repo": repo, "ref": ref,
            "task": task, "check": check, "max_effort": max_effort,
            "created": t, "expires": t + int(expires_s)}
    if src is not None:
        body["src"] = src
    validate_job(body, now=t)
    if not 60 <= int(expires_s) <= MAX_EXPIRES_S:
        raise JobError("bad_expires")
    return body["id"], protocol.encode("job", body, ident, now=t)


def validate_job(body: Any, *, now: float | None = None) -> dict:
    """Field checks of a job body (no MAC, no peer, no allowlist). Returns body or raises JobError."""
    t = time.time() if now is None else float(now)
    if not isinstance(body, dict):
        raise JobError("bad_body")
    need = {"id", "from", "to", "repo", "ref", "task", "check", "max_effort", "created", "expires"}
    if set(body) - {"src"} != need:
        raise JobError("bad_fields")
    if "src" in body and (body["src"] != SRC_MESH or valid_sha(body["ref"])):
        raise JobError("bad_src")           # a bundle carries a branch or tag, not a bare commit
    if not valid_id(body["id"]):
        raise JobError("bad_id")
    for k in ("from", "to"):
        if not isinstance(body[k], str) or not _NODE_RE.match(body[k]):
            raise JobError(f"bad_{k}")
    if not isinstance(body["repo"], str) or not _REPO_RE.match(body["repo"]):
        raise JobError("bad_repo")
    if not valid_ref(body["ref"]):
        raise JobError("bad_ref")
    if not isinstance(body["task"], str) or not body["task"].strip() \
            or len(body["task"].encode("utf-8")) > MAX_TASK_BYTES:
        raise JobError("bad_task")
    if not isinstance(body["check"], str) or len(body["check"].encode("utf-8")) > MAX_CHECK_BYTES:
        raise JobError("bad_check")
    if body["max_effort"] not in CEILINGS:
        raise JobError("bad_max_effort")
    for k in ("created", "expires"):
        if isinstance(body[k], bool) or not isinstance(body[k], (int, float)):
            raise JobError(f"bad_{k}")
    if body["expires"] - body["created"] > MAX_EXPIRES_S + 1 or body["expires"] <= body["created"]:
        raise JobError("bad_expires")
    if t >= body["expires"]:
        raise JobError("expired")
    return body


def open_job(data: bytes | str, ident: Identity, *, now: float | None = None) -> dict:
    """Verify an offered job envelope (MAC, mesh, kind, fields, expiry). Returns the body.

    The 30 s skew rule of protocol.decode does not apply: an offered job may wait up to 1 h. So
    decode runs with ``now`` = the envelope's own ts (MAC + mesh + proto checked), then the job's
    own ``expires`` is checked against the real clock.
    """
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if len(raw) > MAX_JOB_ENVELOPE_BYTES:
        raise JobError("too_big")
    try:
        ts = json.loads(raw).get("ts")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            raise JobError("bad_envelope")
        env = protocol.decode(raw, ident, None, now=float(ts), max_bytes=MAX_JOB_ENVELOPE_BYTES)
    except (ValueError, AttributeError) as e:
        raise JobError("bad_envelope") from e
    if env.get("kind") != "job":
        raise JobError("bad_kind")
    return validate_job(env.get("body"), now=now)


# -- result body ------------------------------------------------------------------------------

def validate_result(body: Any) -> dict:
    """Shape + size checks of a result body. Returns body or raises JobError."""
    if not isinstance(body, dict) or not valid_id(body.get("id")):
        raise JobError("bad_result")
    if body.get("status") not in STATUSES:
        raise JobError("bad_status")
    if not isinstance(body.get("reason", ""), str) or len(body.get("reason", "")) > 1024:
        raise JobError("bad_reason")
    steps = body.get("steps", [])
    if not isinstance(steps, list) or len(steps) > len(LADDER):
        raise JobError("bad_steps")
    patch = body.get("patch_b64", "")
    if not isinstance(patch, str) or len(patch) > (MAX_PATCH_BYTES * 4) // 3 + 4:
        raise JobError("bad_patch")
    report = body.get("report", "")
    if not isinstance(report, str) or len(report.encode("utf-8")) > MAX_REPORT_BYTES:
        raise JobError("bad_report")
    return body


# -- file layout ------------------------------------------------------------------------------

def home(h: Path | None = None) -> Path:
    return h if h is not None else identity.mesh_home()


def outbox(h: Path | None = None) -> Path:
    return home(h) / "outbox"


def inbox(h: Path | None = None) -> Path:
    return home(h) / "inbox"


def worker_dir(h: Path | None = None) -> Path:
    return home(h) / "worker"


def results_dir(h: Path | None = None) -> Path:
    return worker_dir(h) / "results"


def write_private(path: Path, data: bytes) -> None:
    """Atomic write, file 0600, parent 0700."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def offered_jobs(for_node: str, ident: Identity, *, h: Path | None = None,
                 now: float | None = None) -> list[str]:
    """Sender side: signed job envelopes (str) for ``for_node``, not expired, no result yet.

    At most MAX_OFFERED and JOBS_BUDGET bytes (at least one job), oldest first. A job with a
    result in inbox/ is no longer offered.
    """
    t = time.time() if now is None else float(now)
    out: list[tuple[float, str]] = []
    box = outbox(h)
    if not box.is_dir():
        return []
    done = {p.stem for p in inbox(h).glob("*.json")} if inbox(h).is_dir() else set()
    for p in box.glob("*.job"):
        if p.stem in done or not valid_id(p.stem):
            continue
        try:
            raw = p.read_bytes()
            body = open_job(raw, ident, now=t)
        except (OSError, JobError):
            continue
        if body["to"] != for_node or body["id"] != p.stem:
            continue
        out.append((body["created"], raw.decode("utf-8")))
    out.sort()
    picked: list[str] = []
    size = 0
    for _, s in out[:MAX_OFFERED]:
        n = len(s.encode("utf-8"))
        if picked and size + n > JOBS_BUDGET:
            break
        picked.append(s)
        size += n
    return picked



# -- repo bundles (job src "mesh") ---------------------------------------------------------------

SHARE_FILE = "share_repos.json"     # sender side: {"repos": {name: abs path}}; opt-in per repo
XVAL_REPO = "xval"                  # `mesh xval` scratch repo: built in, one empty commit


def xval_repo_path(h: Path | None = None) -> Path:
    return Path(str(home(h)) + "-xval")


def ensure_xval_repo(h: Path | None = None) -> Path | None:
    """Sender side: create the xval scratch repo (main, one empty commit) if missing."""
    p = xval_repo_path(h)
    if _git_out(p, "rev-parse", "--verify", "--quiet", "main^{commit}", timeout=30):
        return p
    try:
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        return None
    ok = _git_out(p, "init", "--quiet") is not None \
        and _git_out(p, "symbolic-ref", "HEAD", "refs/heads/main") is not None \
        and _git_out(p, "-c", "user.name=apex-mesh", "-c", "user.email=mesh@localhost",
                     "commit", "--quiet", "--allow-empty", "-m", "xval scratch") is not None
    return p if ok else None


def load_share(h: Path | None = None) -> dict[str, Path]:
    """Sender side: repos this node may send as bundles: share_repos.json, plus the xval
    scratch repo when it exists. Missing or bad file = only xval."""
    out: dict[str, Path] = {}
    if (xval_repo_path(h) / ".git").is_dir():
        out[XVAL_REPO] = xval_repo_path(h)
    doc = read_json(home(h) / SHARE_FILE)
    repos = doc.get("repos") if isinstance(doc, dict) else None
    if not isinstance(repos, dict):
        return out
    for k, v in repos.items():
        if isinstance(k, str) and _REPO_RE.match(k) and isinstance(v, str) \
                and Path(v).is_absolute() and Path(v).is_dir():
            out[k] = Path(v)
    return out


def _git_out(repo: Path, *args: str, timeout: float = 120) -> bytes | None:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
                           env=env, capture_output=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


_BUNDLE_CACHE: dict[tuple[str, str], dict] = {}


def bundle_for(for_node: str, jid: str, ident: Identity, *, h: Path | None = None,
               now: float | None = None) -> dict:
    """Sender side: the bundle body for one open ``src: mesh`` job offered to ``for_node``.

    Returns ``{"id", "ref", "commit", "sha256", "size", "bundle_b64"}``. Raises JobError:
    ``no_job`` (not ours, not open, not for that node, no src), ``repo_not_shared``,
    ``bad_ref``, ``bundle_failed``, ``bundle_too_big``.
    """
    if not valid_id(jid) or not isinstance(for_node, str) or not _NODE_RE.match(for_node):
        raise JobError("no_job")
    if (inbox(h) / f"{jid}.json").exists():
        raise JobError("no_job")                    # answered: the job is closed
    try:
        body = open_job((outbox(h) / f"{jid}.job").read_bytes(), ident, now=now)
    except (OSError, JobError) as e:
        raise JobError("no_job") from e
    if body["to"] != for_node or body["id"] != jid or body.get("src") != SRC_MESH \
            or body["from"] != ident.node_id:
        raise JobError("no_job")
    repo = load_share(h).get(body["repo"])
    if repo is None:
        raise JobError("repo_not_shared")
    ref = body["ref"]
    sha = _git_out(repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
                   f"{ref}^{{commit}}", timeout=30)
    commit = sha.decode().strip() if sha else ""
    if not valid_sha(commit):
        raise JobError("bad_ref")
    key = (jid, commit)
    if key in _BUNDLE_CACHE:
        return _BUNDLE_CACHE[key]
    with tempfile.TemporaryDirectory(prefix="apex-mesh-bundle-") as td:
        path = Path(td) / "repo.bundle"
        if _git_out(repo, "bundle", "create", "--quiet", str(path), ref, timeout=300) is None \
                or not path.is_file():
            raise JobError("bundle_failed")
        if path.stat().st_size > MAX_BUNDLE_BYTES:
            raise JobError("bundle_too_big")
        data = path.read_bytes()
    out = {"id": jid, "ref": ref, "commit": commit, "sha256": hashlib.sha256(data).hexdigest(),
           "size": len(data), "bundle_b64": base64.b64encode(data).decode("ascii")}
    if len(_BUNDLE_CACHE) >= 4:
        _BUNDLE_CACHE.pop(next(iter(_BUNDLE_CACHE)))
    _BUNDLE_CACHE[key] = out
    return out


def decode_bundle(body: Any, jid: str) -> tuple[bytes, str, str]:
    """Worker side: check a ``bundle`` body. Returns (bytes, ref, commit). Raises JobError."""
    if not isinstance(body, dict) or body.get("id") != jid:
        raise JobError("bad_bundle")
    ref, commit, digest, b64 = (body.get(k) for k in ("ref", "commit", "sha256", "bundle_b64"))
    if not valid_ref(ref) or not valid_sha(commit) or not isinstance(digest, str) \
            or not isinstance(b64, str) or len(b64) > (MAX_BUNDLE_BYTES * 4) // 3 + 8:
        raise JobError("bad_bundle")
    try:
        data = base64.b64decode(b64, validate=True)
    except ValueError as e:
        raise JobError("bad_bundle") from e
    if len(data) > MAX_BUNDLE_BYTES or hashlib.sha256(data).hexdigest() != digest:
        raise JobError("bad_bundle")
    return data, str(ref), str(commit)


def results_for(for_node: str, *, h: Path | None = None, only: str | None = None) -> list[dict]:
    """Worker side: result bodies (incl. running) whose job came from ``for_node``.

    A result file carries a private "_from" key (sender node id); it is stripped from the output.
    """
    out = []
    d = results_dir(h)
    if not d.is_dir():
        return []
    files = [d / f"{only}.json"] if only is not None else sorted(d.glob("*.json"))
    for p in files:
        doc = read_json(p)
        if not isinstance(doc, dict) or doc.get("_from") != for_node:
            continue
        body = {k: v for k, v in doc.items() if k != "_from"}
        try:
            out.append(validate_result(body))
        except JobError:
            continue
    return out if only is not None else budget(out)     # one result: full (<= ~700 KiB + report)


RESULTS_BUDGET = 900 * 1024        # under the client's 1 MiB response cap, with envelope room


def budget(results: list[dict], limit: int = RESULTS_BUDGET) -> list[dict]:
    """Fit results into one response: newest first by full size; the rest become stubs.

    A stub keeps id, status, reason and steps and sets ``"truncated": true`` (patch and report
    removed). The sender asks again with ``?for=..&id=<id>`` to get that one result in full.
    """
    def size(r: dict) -> int:
        return len(json.dumps(r, separators=(",", ":")).encode("utf-8")) + 64

    out: list[dict] = []
    used = 0
    for r in results[:64]:
        full = size(r)
        if used + full <= limit:
            out.append(r)
            used += full
            continue
        stub = {k: r[k] for k in ("id", "status", "reason", "steps") if k in r}
        stub.update({"patch_b64": "", "report": "", "truncated": True})
        if used + size(stub) > limit:
            break
        out.append(stub)
        used += size(stub)
    return out


def write_result(body: dict, sender: str, *, h: Path | None = None) -> None:
    """Worker side: store one result (validated) for ``sender``."""
    validate_result(body)
    doc = dict(body)
    doc["_from"] = sender
    write_private(results_dir(h) / f"{body['id']}.json",
                  json.dumps(doc, sort_keys=True).encode("utf-8"))
