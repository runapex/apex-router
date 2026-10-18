"""Mesh worker, sender side (docs/superpowers/specs/2026-10-17-mesh-worker-design.md).

- ``submit`` signs one job and writes ``outbox/<id>.job`` (served by ``mesh up`` on
  ``GET /mesh/v1/jobs?for=<worker>``).
- ``fetch_jobs`` (used by a worker) and ``fetch_results`` (used by the sender) are HTTP clients.
  They never raise. Each response must be a signed envelope of the right kind; ``fetch_results``
  also checks ``body.node_id`` and every result with ``jobs.validate_result``.
- ``read_job``, ``job_state``, ``list_jobs``, ``resolve_to``, ``write_outputs`` and ``apply_patch``
  are the local helpers of ``mesh submit|result|jobs``.

Stdlib only. Sockets go through ``collect._pinned_opener`` (LAN interface of the running ``up``).
"""
from __future__ import annotations

import base64
import binascii
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import collect, jobs, protocol
from .jobs import JobError

if TYPE_CHECKING:
    from .identity import Identity
    from .peers import Peer

AUTH_HEADER = "X-Apex-Mesh"
MAX_RESPONSE_BYTES = 2 << 20          # 2 MiB: one full result (512 KiB patch -> ~700 KiB base64,
                                     # 64 KiB report, up to 6x JSON escaping) always fits
# GET /jobs: up to jobs.JOBS_BUDGET raw envelope bytes, escaped once more in the response (<= 2x).
MAX_JOBS_RESPONSE_BYTES = 8 << 20
# GET /bundle: one base64 git bundle (jobs.MAX_BUNDLE_BYTES raw -> 4/3) plus the envelope.
MAX_BUNDLE_RESPONSE_BYTES = 34 << 20
TERMINAL = ("done", "failed", "rejected")
STATES = ("offered", "running", "done", "failed", "rejected", "expired")

_RESPONSE_GUARD = protocol.ReplayGuard()


# ---- submit -----------------------------------------------------------------------------------

def submit(ident: Identity, *, to: str, repo: str, ref: str, task: str, check: str = "",
           max_effort: str = jobs.DEFAULT_CEILING, expires_s: int = jobs.DEFAULT_EXPIRES_S,
           h: Path | None = None, now: float | None = None, src: str | None = None) -> str:
    """Sign one job and write ``outbox/<id>.job`` (0600). Returns the id. Raises JobError."""
    jid, env = jobs.make_job(ident, to=to, repo=repo, ref=ref, task=task, check=check,
                             max_effort=max_effort, expires_s=expires_s, now=now, src=src)
    if len(env) > jobs.MAX_JOB_ENVELOPE_BYTES:
        raise JobError("job_too_big")               # JSON escaping can grow a 256 KiB task past it
    jobs.write_private(jobs.outbox(h) / f"{jid}.job", env)
    return jid


def resolve_to(spec: str, rows: list[dict]) -> str:
    """Node id for ``spec`` (a node name or a node id) from peers.json rows. Raises JobError."""
    if not isinstance(spec, str) or not spec:
        raise JobError("unknown_worker")
    by_id = [r for r in rows if isinstance(r, dict) and r.get("node_id") == spec]
    if by_id:
        return spec
    by_name = {r.get("node_id") for r in rows
               if isinstance(r, dict) and r.get("name") == spec
               and isinstance(r.get("node_id"), str)}
    if len(by_name) > 1:
        raise JobError("ambiguous_worker")
    if not by_name:
        raise JobError("unknown_worker")
    return by_name.pop()


# ---- local job/result state -------------------------------------------------------------------

def read_job(jid: str, ident: Identity, *, h: Path | None = None) -> dict | None:
    """Body of ``outbox/<id>.job`` (MAC, kind and fields checked; expiry NOT checked), or None.

    A result can come after the job expired (the run may take up to 30 min), so expiry is not
    a reason to forget the job here.
    """
    if not jobs.valid_id(jid):
        return None
    try:
        raw = (jobs.outbox(h) / f"{jid}.job").read_bytes()
        if len(raw) > jobs.MAX_JOB_ENVELOPE_BYTES:
            return None
        ts = json.loads(raw).get("ts")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            return None
        env = protocol.decode(raw, ident, None, now=float(ts),
                              max_bytes=jobs.MAX_JOB_ENVELOPE_BYTES)
        if env.get("kind") != "job":
            return None
        body = env.get("body")
        jobs.validate_job(body, now=body.get("created") if isinstance(body, dict) else None)
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    if body["id"] != jid or body["from"] != ident.node_id:
        return None
    return body


def stored_result(jid: str, *, h: Path | None = None) -> dict | None:
    """The verified result body in ``inbox/<id>.json``, or None."""
    if not jobs.valid_id(jid):
        return None
    doc = jobs.read_json(jobs.inbox(h) / f"{jid}.json")
    try:
        body = jobs.validate_result(doc)
    except JobError:
        return None
    if body.get("truncated"):
        return None                              # an old cached stub: fetch the full result again
    return body if body["id"] == jid else None


def store_result(body: dict, *, h: Path | None = None) -> None:
    jobs.validate_result(body)
    if body.get("truncated"):
        raise JobError("truncated")              # a stub has no patch/report: never cache it
    jobs.write_private(jobs.inbox(h) / f"{body['id']}.json",
                       json.dumps(body, sort_keys=True).encode("utf-8"))


def job_state(job: dict, result: dict | None, *, now: float | None = None) -> str:
    """offered | running | done | failed | rejected | expired."""
    if result is not None and result.get("status") in STATES:
        return result["status"]
    t = time.time() if now is None else float(now)
    return "expired" if t >= float(job["expires"]) else "offered"


def list_jobs(ident: Identity, *, h: Path | None = None, now: float | None = None) -> list[dict]:
    """Outbox jobs (oldest first): id, to, repo, ref, max_effort, created, expires, state."""
    box = jobs.outbox(h)
    if not box.is_dir():
        return []
    out = []
    for p in box.glob("*.job"):
        job = read_job(p.stem, ident, h=h)
        if job is None:
            continue
        res = stored_result(job["id"], h=h)
        out.append({"id": job["id"], "to": job["to"], "repo": job["repo"], "ref": job["ref"],
                    "max_effort": job["max_effort"], "created": job["created"],
                    "expires": job["expires"], "state": job_state(job, res, now=now)})
    out.sort(key=lambda r: (r["created"], r["id"]))
    return out


# ---- HTTP clients -----------------------------------------------------------------------------

def _url(peer: Peer, path: str) -> str:
    return f"http://{collect._host_for_url(peer.host)}:{int(peer.port)}{path}"


def _get_signed(peer: Peer, ident: Identity, base: str, for_node: str, kind: str,
                timeout: float, opener: Any, job_id: str | None = None,
                max_bytes: int = MAX_RESPONSE_BYTES) -> tuple[dict | None, str | None]:
    """GET ``base?for=<for_node>[&id=<job_id>]`` from ``peer``. (envelope body, None) or (None, error)."""
    path = f"{base}?for=" + urllib.parse.quote(for_node, safe="")
    if job_id is not None:
        path += "&id=" + urllib.parse.quote(job_id, safe="")
    req = urllib.request.Request(_url(peer, path), method="GET", headers={
        AUTH_HEADER: protocol.auth_header(ident, path, peer.node_id)})
    end = time.monotonic() + float(timeout)
    try:
        with (opener or collect._pinned_opener(collect._local_ip())).open(
                req, timeout=timeout) as resp:
            chunks, size = [], 0
            while size <= max_bytes:
                if time.monotonic() > end:
                    return None, "timeout"
                chunk = resp.read(min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b"".join(chunks)
    except urllib.error.HTTPError as e:
        return None, f"http {e.code}"
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(e, TimeoutError) or isinstance(reason, TimeoutError):
            return None, "timeout"
        return None, f"unreachable: {type(reason).__name__}"
    try:
        env = protocol.decode(raw, ident, _RESPONSE_GUARD, max_bytes=max_bytes)
    except protocol.ProtocolError as e:
        return None, f"bad response: {e}"
    if env["kind"] != kind:
        return None, f"bad response: kind {env['kind']!r}"
    return env["body"], None


def fetch_jobs(peer: Peer, ident: Identity, timeout: float = 5.0, *, opener: Any = None) -> dict:
    """Worker side: jobs that ``peer`` offers to this node. Never raises.

    Returns ``{"node_id", "name", "ok", "jobs": [<job envelope str>, ...], "error"?}``. The inner
    jobs are NOT opened here: the worker checks each one with ``jobs.open_job`` and its own rules.
    """
    out: dict = {"node_id": getattr(peer, "node_id", None), "name": getattr(peer, "name", None),
                 "ok": False, "jobs": []}
    try:
        body, err = _get_signed(peer, ident, jobs.JOBS_PATH, ident.node_id, "jobs", timeout,
                                opener, max_bytes=MAX_JOBS_RESPONSE_BYTES)
        if err is not None:
            out["error"] = err
            return out
        lst = body.get("jobs")
        if not isinstance(lst, list) or len(lst) > jobs.MAX_OFFERED \
                or not all(isinstance(s, str) and len(s.encode("utf-8"))
                           <= jobs.MAX_JOB_ENVELOPE_BYTES for s in lst):
            out["error"] = "bad response: jobs"
            return out
        out.update(ok=True, jobs=list(lst))
        return out
    except Exception as e:  # noqa: BLE001 -- contract: never raises
        out["error"] = f"error: {type(e).__name__}"
        return out


def fetch_results(peer: Peer, ident: Identity, timeout: float = 5.0, *,
                  opener: Any = None, job_id: str | None = None) -> dict:
    """Sender side: results ``peer`` holds for jobs this node sent. Never raises.

    Returns ``{"node_id", "name", "ok", "results": [<result body>, ...], "dropped": int,
    "error"?}``. The response must be a signed ``results`` envelope with ``body.node_id`` =
    ``peer.node_id``. A result that fails ``jobs.validate_result`` is dropped and counted.
    """
    out: dict = {"node_id": getattr(peer, "node_id", None), "name": getattr(peer, "name", None),
                 "ok": False, "results": [], "dropped": 0}
    try:
        if job_id is not None and not jobs.valid_id(job_id):
            out["error"] = "bad job id"
            return out
        body, err = _get_signed(peer, ident, jobs.RESULTS_PATH, ident.node_id, "results",
                                timeout, opener, job_id)
        if err is not None:
            out["error"] = err
            return out
        if body.get("node_id") != peer.node_id:
            out["error"] = "bad response: node_id mismatch"
            return out
        lst = body.get("results")
        if not isinstance(lst, list):
            out["error"] = "bad response: results"
            return out
        good = []
        for r in lst:
            try:
                good.append(jobs.validate_result(r))
            except JobError:
                out["dropped"] += 1
        out.update(ok=True, results=good)
        return out
    except Exception as e:  # noqa: BLE001 -- contract: never raises
        out["error"] = f"error: {type(e).__name__}"
        return out



def fetch_bundle(peer: Peer, ident: Identity, job_id: str, timeout: float = 120.0, *,
                 opener: Any = None) -> dict:
    """Worker side: the repo bundle of job ``job_id`` from its sender ``peer``. Never raises.

    Returns ``{"ok": True, "data": bytes, "ref", "commit"}`` or ``{"ok": False, "error"}``.
    """
    try:
        if not jobs.valid_id(job_id):
            return {"ok": False, "error": "bad job id"}
        body, err = _get_signed(peer, ident, jobs.BUNDLE_PATH, ident.node_id, "bundle", timeout,
                                opener, job_id, max_bytes=MAX_BUNDLE_RESPONSE_BYTES)
        if err is not None:
            return {"ok": False, "error": err}
        data, ref, commit = jobs.decode_bundle(body, job_id)
        return {"ok": True, "data": data, "ref": ref, "commit": commit}
    except JobError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 -- contract: never raises
        return {"ok": False, "error": f"error: {type(e).__name__}"}


# ---- result outputs and apply -----------------------------------------------------------------

def decode_patch(body: dict) -> bytes:
    """The patch bytes of a result (may be empty). Raises JobError on bad base64 or size."""
    s = body.get("patch_b64", "") or ""
    try:
        data = base64.b64decode(s.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError) as e:
        raise JobError("bad_patch") from e
    if len(data) > jobs.MAX_PATCH_BYTES:
        raise JobError("bad_patch")
    return data


def report_md(job: dict | None, body: dict, peer_name: str | None = None) -> str:
    lines = [f"# mesh job {body['id']}", "",
             f"- status: {body['status']}"]
    if body.get("reason"):
        lines.append(f"- reason: {body['reason']}")
    if job is not None:
        lines += [f"- worker: {peer_name or job['to']} ({job['to']})",
                  f"- repo: {job['repo']}", f"- ref: {job['ref']}",
                  f"- max_effort: {job['max_effort']}"]
    steps = body.get("steps") or []
    if steps:
        lines += ["", "| effort | model | pass | s | cost_usd |", "|---|---|---|---|---|"]
        for st in steps:
            st = st if isinstance(st, dict) else {}
            lines.append("| " + " | ".join(str(st.get(k, "")) for k in
                                            ("effort", "model", "pass", "s", "cost_usd")) + " |")
    lines += ["", "## Report", "", body.get("report", "") or "(no report)", ""]
    return "\n".join(lines)


def write_outputs(job: dict | None, body: dict, cwd: Path,
                  peer_name: str | None = None) -> tuple[Path | None, Path]:
    """Write ``<id>.patch`` (only when the patch is not empty) and ``<id>.md`` in ``cwd``."""
    patch = decode_patch(body)
    ppath = None
    if patch:
        ppath = cwd / f"{body['id']}.patch"
        ppath.write_bytes(patch)
    mpath = cwd / f"{body['id']}.md"
    mpath.write_text(report_md(job, body, peer_name), encoding="utf-8")
    return ppath, mpath


def _git(cwd: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          timeout=timeout, check=False)


def apply_patch(patch: Path, cwd: Path) -> tuple[int, str]:
    """``git am --3way <patch>`` in the repo of ``cwd``. Returns (exit code, message).

    2 = refused (not a git repo, tracked changes, an am/rebase in progress). 1 = git am failed
    (then ``git am --abort`` runs). 0 = applied.
    """
    try:
        top = _git(cwd, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            return 2, "not a git repository"
        gitdir = _git(cwd, "rev-parse", "--absolute-git-dir")
        gd = Path(gitdir.stdout.strip()) if gitdir.returncode == 0 else None
        if gd is not None and ((gd / "rebase-apply").exists() or (gd / "rebase-merge").exists()):
            return 2, "a git am or rebase is in progress"
        st = _git(cwd, "status", "--porcelain", "--untracked-files=no")
        if st.returncode != 0:
            return 2, "git status failed"
        if st.stdout.strip():
            return 2, "working tree is dirty (commit or stash first)"
        am = _git(cwd, "am", "--3way", str(patch.resolve()))
        if am.returncode != 0:
            _git(cwd, "am", "--abort")
            msg = (am.stderr or am.stdout).strip().splitlines()
            return 1, "git am failed: " + (msg[-1] if msg else f"exit {am.returncode}")
        return 0, "applied"
    except (OSError, subprocess.SubprocessError) as e:
        return 1, f"git: {type(e).__name__}"
