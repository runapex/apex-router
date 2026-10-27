"""Draft, review, apply, backup and rollback for apex flow studio.

The draft is ``<router home>/studio/draft.json``: ``{"schema":1, "changes": {key: change}}``. A
change is ``{"id", "value", "proxy"?}``; ``key`` is the id (``id@proxy`` for proxy-env settings).

Effect classes (``flow``):
  live     Apply writes the file: a timestamped backup in ``studio/backups/<stamp>/`` first, then
           an atomic write (temp file + fsync + os.replace) of JSON that parses back and passes
           the family checks. Rollback restores the newest backup set that is not rolled back.
  restart  `apply()` here only returns a plan (the plist diff with secrets masked, and the commands).
           The plist write + restart is `restart.py`: only when the proxy reports 0 requests in
           progress after an idle window, with an automatic restore if it does not come back healthy.
  locked   rejected (also when a hand-edited draft.json carries one). Secret settings too.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import shlex
import tempfile
import time
from pathlib import Path

from .flow import (FAMILY_RE, MODEL_ID_RE, PATH_VALUE_RE, PROVIDER_RE, PROXIES, THINKING_LEVELS, Paths,
                   all_settings, is_secret, plist_env, read_json, tilde)

MAX_CHANGES = 64


class DraftError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


# ---- validation -----------------------------------------------------------------------------

def validate_value(s: dict, value) -> str:
    """The value as the string that would be stored, or DraftError."""
    if value is None:
        raise DraftError(f"{s['id']}: value missing")
    if isinstance(value, bool):
        value = "1" if value else "0"
    if isinstance(value, (int, float)):
        value = repr(value) if isinstance(value, float) else str(value)
    if not isinstance(value, str) or len(value) > 400 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise DraftError(f"{s['id']}: bad value")
    v = value.strip()
    k = s["kind"]
    if k == "bool":
        if v not in ("0", "1"):
            raise DraftError(f"{s['id']}: must be 0 or 1")
    elif k in ("int", "float"):
        try:
            n = int(v) if k == "int" else float(v)
        except ValueError:
            raise DraftError(f"{s['id']}: must be a number") from None
        if k == "float" and (n != n or n in (float("inf"), float("-inf"))):
            raise DraftError(f"{s['id']}: must be finite")
        lo, hi = s.get("min"), s.get("max")
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            raise DraftError(f"{s['id']}: must be in [{lo}, {hi}]")
    elif k == "enum":
        if v not in (s.get("choices") or []):
            raise DraftError(f"{s['id']}: must be one of {', '.join(s.get('choices') or [])}")
    elif k == "model":
        if not MODEL_ID_RE.fullmatch(v):
            raise DraftError(f"{s['id']}: not a valid model id")
    elif k == "provider":
        if not PROVIDER_RE.fullmatch(v):
            raise DraftError(f"{s['id']}: not a valid provider name")
    elif k == "path":
        if not PATH_VALUE_RE.fullmatch(v) or ".." in v.split("/"):
            raise DraftError(f"{s['id']}: not a plain path")
    else:
        raise DraftError(f"{s['id']}: not editable")
    return v


def check_editable(sid: str, settings: dict) -> dict:
    s = settings.get(sid)
    if s is None:
        raise DraftError(f"unknown setting: {sid}", 404)
    if s["class"] == "locked":
        raise DraftError(f"{sid} is locked", 403)
    if s["secret"] or is_secret(s["name"]):
        raise DraftError(f"{sid} is secret: not editable here", 403)
    return s


def _key(s: dict, proxy) -> str:
    return f"{s['id']}@{proxy}" if s["scope"] == "proxy_env" else s["id"]


# ---- draft file -----------------------------------------------------------------------------

def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_draft(paths: Paths) -> dict:
    d = read_json(paths.draft)
    if not isinstance(d, dict) or not isinstance(d.get("changes"), dict):
        return {"schema": 1, "changes": {}, "updated_at": None}
    ch = {k: v for k, v in d["changes"].items()
          if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("id"), str)}
    return {"schema": 1, "changes": ch, "updated_at": d.get("updated_at")}


def save_draft(paths: Paths, draft: dict) -> None:
    draft = {"schema": 1, "changes": draft["changes"], "updated_at": time.time()}
    _atomic_write(paths.draft, json.dumps(draft, indent=2, sort_keys=True) + "\n", 0o600)


def update_draft(paths: Paths, body) -> dict:
    """PUT body: {"set": [{"id","value","proxy"?}], "unset": [key], "clear": bool}. All or nothing."""
    if not isinstance(body, dict):
        raise DraftError("body must be a JSON object")
    settings = all_settings(paths)
    draft = load_draft(paths)
    ch = {} if body.get("clear") is True else dict(draft["changes"])
    for key in body.get("unset") or []:
        if isinstance(key, str):
            ch.pop(key, None)
    sets = body.get("set") or []
    if not isinstance(sets, list):
        raise DraftError("set must be a list")
    for item in sets:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise DraftError("each set item needs an id")
        s = check_editable(item["id"], settings)
        proxy = item.get("proxy") or "main"
        if s["scope"] == "proxy_env" and proxy not in PROXIES:
            raise DraftError("unknown proxy")
        v = validate_value(s, item.get("value"))
        ch[_key(s, proxy)] = {"id": s["id"], "value": v, **({"proxy": proxy} if s["scope"] == "proxy_env" else {})}
    if len(ch) > MAX_CHANGES:
        raise DraftError(f"too many changes (max {MAX_CHANGES})")
    draft["changes"] = ch
    save_draft(paths, draft)
    return load_draft(paths)


# ---- review ---------------------------------------------------------------------------------

def _dump(obj) -> str:
    return json.dumps(obj, indent=2) + "\n"


SECRET_VALUE_RE = __import__("re").compile(r"^(?:sk-|xox[abp]-|ghp_|eyJ)[A-Za-z0-9._-]{8,}|^[A-Za-z0-9+/_=-]{40,}$")


def _mask_json(obj, parent_key: str = ""):
    """A copy for display: the value of a secret-named key, or a string that looks like a token, is
    replaced by "<set>". Diffs and API output use this; the written file keeps the real values."""
    if isinstance(obj, dict):
        return {k: ("<set>" if (is_secret(str(k)) and v not in (None, "")) else _mask_json(v, str(k)))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_json(v, parent_key) for v in obj]
    if isinstance(obj, str) and SECRET_VALUE_RE.search(obj):
        return "<set>"
    return obj


def _udiff(a: str, b: str, name: str) -> str:
    return "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True),
                                        fromfile=f"a/{name}", tofile=f"b/{name}", n=2))


def _file_target(paths: Paths, fname: str) -> Path:
    return paths.models if fname == "models.json" else paths.pi_routes


def _apply_file_changes(raw, fname: str, items: list):
    """New JSON object for one file. ``raw`` is the parsed current file (must be an object)."""
    if not isinstance(raw, dict):
        raise DraftError(f"{fname} is missing or not a JSON object", 409)
    new = json.loads(json.dumps(raw))
    for s, v in items:
        fam = s["family"]
        if not FAMILY_RE.fullmatch(fam):
            raise DraftError(f"bad family name {fam!r}")
        if fname == "models.json":
            fams = new.get("pi_families")
            if not isinstance(fams, dict) or not isinstance(fams.get(fam), dict):
                raise DraftError(f"{fname}: family {fam} is not in the file", 409)
            if s["field"] == "id" and not isinstance(fams[fam].get("id"), str):
                raise DraftError(f"{fname}: family {fam} has no pinned id", 409)
            fams[fam][s["field"]] = v
        else:
            if not isinstance(new.get(fam), dict):
                raise DraftError(f"{fname}: family {fam} is not in the file", 409)
            new[fam][s["field"]] = v
    return new


def _validate_file(fname: str, obj) -> None:
    """The JSON round-trips and the family entries keep the shape apex-route.ts reads."""
    back = json.loads(json.dumps(obj))
    if back != obj or not isinstance(obj, dict):
        raise DraftError(f"{fname}: JSON check failed", 500)
    fams = obj.get("pi_families", {}) if fname == "models.json" else obj
    if not isinstance(fams, dict):
        raise DraftError(f"{fname}: pi_families must be an object", 500)
    for name, spec in fams.items():
        if name.startswith("//") or not isinstance(spec, dict):
            continue
        if "provider" in spec and not (isinstance(spec["provider"], str) and PROVIDER_RE.fullmatch(spec["provider"])):
            raise DraftError(f"{fname}: {name}.provider is not valid", 500)
        if "id" in spec and not (isinstance(spec["id"], str) and MODEL_ID_RE.fullmatch(spec["id"])):
            raise DraftError(f"{fname}: {name}.id is not valid", 500)
        if "effort" in spec and spec["effort"] not in THINKING_LEVELS:
            raise DraftError(f"{fname}: {name}.effort is not valid", 500)


def _masked_env_lines(env: dict) -> list:
    out = []
    for k in sorted(env):
        v = ("<set>" if env[k] != "" else "<empty>") if is_secret(k) else env[k]
        out.append(f"{k}={v}\n")
    return out


def _plist_plan(paths: Paths, proxy: str, items: list) -> dict:
    plist = paths.plist(proxy)
    env = plist_env(plist)
    new = dict(env)
    for st, v in items:
        new[st["name"]] = v
    diff = "".join(difflib.unified_diff(_masked_env_lines(env), _masked_env_lines(new),
                                        fromfile=f"a/{plist.name} EnvironmentVariables",
                                        tofile=f"b/{plist.name} EnvironmentVariables", n=2))
    label = PROXIES[proxy]["label"]
    q = '"$HOME/' + tilde(plist)[2:] + '"' if tilde(plist).startswith("~/") else shlex.quote(str(plist))
    cmds = []
    for st, v in items:
        op = f"Set :EnvironmentVariables:{st['name']} {v}" if st["name"] in env \
            else f"Add :EnvironmentVariables:{st['name']} string {v}"
        cmds.append(f"/usr/libexec/PlistBuddy -c {shlex.quote(op)} {q}")
    cmds += [f'launchctl bootout "gui/$(id -u)/{label}"',
             f'launchctl bootstrap "gui/$(id -u)" {q}']
    return {"target": f"plist:{proxy}", "file": tilde(plist), "present": plist.is_file(), "diff": diff,
            "commands": cmds, "class": "restart",
            "why": f"The proxy :{PROXIES[proxy]['port']} reads its env only at start. A restart during a request "
                   "breaks it, so the studio restarts only when no request is in progress and none started for "
                   "the idle time (bootout + bootstrap: kickstart -k alone keeps the old env)."}


def _pi_env_plan(items: list) -> dict:
    lines = [f"export {s['name']}={shlex.quote(v)}" for s, v in items]
    return {"target": "pi-env", "file": "the shell profile that starts pi (for example ~/.zshrc)",
            "diff": "".join(f"+{ln}\n" for ln in lines), "commands": lines + ["# then start pi again"],
            "class": "restart",
            "why": "pi reads these env vars when it starts. A running pi session keeps the old value; "
                   "start a new pi session after the change."}


def review(paths: Paths, draft: dict | None = None) -> dict:
    draft = draft or load_draft(paths)
    settings = all_settings(paths)
    groups: dict = {}
    errors = []
    for key, c in sorted(draft["changes"].items()):
        try:
            s = check_editable(c["id"], settings)
            v = validate_value(s, c.get("value"))
        except DraftError as e:
            errors.append({"key": key, "error": str(e), "status": e.status})
            continue
        if s["scope"] == "file":
            gk = f"file:{s['file']}"
        elif s["scope"] == "proxy_env":
            gk = f"plist:{c.get('proxy') or 'main'}"
        else:
            gk = "pi-env"
        groups.setdefault(gk, []).append((s, v, key))
    targets = []
    for gk, items in sorted(groups.items()):
        pairs = [(s, v) for s, v, _ in items]
        rows = [{"key": k, "id": s["id"], "name": s["name"], "value": v, "class": s["class"]} for s, v, k in items]
        if gk.startswith("file:"):
            fname = gk[5:]
            fp = _file_target(paths, fname)
            raw = read_json(fp)
            try:
                new = _apply_file_changes(raw, fname, pairs)
                _validate_file(fname, new)
                diff = _udiff(_dump(_mask_json(raw)), _dump(_mask_json(new)), fname)   # xval F3: no secret value
                err = None
            except DraftError as e:
                diff, err = "", str(e)
            targets.append({"target": gk, "file": tilde(fp), "class": "live", "changes": rows, "diff": diff,
                            "error": err, "why": "Apply writes this file (backup first). pi reads it at "
                                                 "extension load: run /reload in pi to use it."})
        elif gk.startswith("plist:"):
            t = _plist_plan(paths, gk[6:], pairs)
            t["changes"] = rows
            targets.append(t)
        else:
            t = _pi_env_plan(pairs)
            t["changes"] = rows
            targets.append(t)
    n_live = sum(len(t["changes"]) for t in targets if t["class"] == "live" and not t.get("error"))
    return {"count": len(draft["changes"]), "live": n_live,
            "restart": sum(len(t["changes"]) for t in targets if t["class"] == "restart"),
            "targets": targets, "errors": errors, "last_backup": last_backup(paths)}


# ---- apply / backup / rollback --------------------------------------------------------------

def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _backup_sets(paths: Paths) -> list:
    if not paths.backups.is_dir():
        return []
    out = []
    for d in sorted(paths.backups.iterdir()):
        m = read_json(d / "manifest.json") if d.is_dir() else None
        if isinstance(m, dict) and isinstance(m.get("files"), list):
            out.append((d, m))
    return out


def last_backup(paths: Paths):
    for d, m in reversed(_backup_sets(paths)):
        if not m.get("rolled_back"):
            return {"id": d.name, "created_at": m.get("created_at"),
                    "files": [tilde(f["path"]) for f in m["files"]]}
    return None


def apply(paths: Paths) -> dict:
    """Write the live-class changes; return plans for the restart-class ones."""
    draft = load_draft(paths)
    settings = all_settings(paths)
    for key, c in draft["changes"].items():
        check_editable(c.get("id", ""), settings)       # a locked/secret id in a hand-edited draft: 403
    rv = review(paths, draft)
    if rv["errors"]:
        raise DraftError("; ".join(e["error"] for e in rv["errors"]), 400)
    live = [t for t in rv["targets"] if t["class"] == "live"]
    plans = [t for t in rv["targets"] if t["class"] == "restart"]
    bad = [t for t in live if t.get("error")]
    if bad:
        raise DraftError("; ".join(t["error"] for t in bad), 409)
    written = []
    if live:
        stamp = time.strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}-{int(time.time() * 1000) % 1000:03d}"
        bdir = paths.backups / stamp
        bdir.mkdir(parents=True, exist_ok=False)
        os.chmod(bdir, 0o700)
        manifest = {"schema": 1, "created_at": time.time(), "files": [], "rolled_back": False, "complete": False}
        groups: dict = {}
        for key, c in draft["changes"].items():
            s = settings[c["id"]]
            if s["class"] == "live":
                groups.setdefault(s["file"], []).append((s, validate_value(s, c["value"]), key))
        # 1. plan every file and write every backup + the manifest BEFORE the first target write, so a
        #    crash at any later point leaves a backup set that rollback finds (xval F2)
        plan = []
        for fname, items in sorted(groups.items()):
            fp = _file_target(paths, fname)
            before = fp.read_bytes()
            new = _apply_file_changes(json.loads(before), fname, [(s, v) for s, v, _ in items])
            _validate_file(fname, new)
            text = _dump(new)
            bfile = bdir / fname
            bfile.write_bytes(before)
            os.chmod(bfile, 0o600)
            manifest["files"].append({"path": str(fp), "backup": fname, "sha_before": _sha(before),
                                      "sha_after": _sha(text.encode())})
            plan.append((fp, before, text, items))
        _atomic_write(bdir / "manifest.json", json.dumps(manifest, indent=2) + "\n", 0o600)
        # 2. write; a file counts as touched before its write, so any failure restores it too
        touched = []
        try:
            for fp, before, text, items in plan:
                touched.append((fp, before))
                _atomic_write(fp, text, fp.stat().st_mode & 0o777)
                if fp.read_bytes() != text.encode():
                    raise OSError(f"{fp.name}: read-back differs")
                json.loads(fp.read_text())
                written.append({"file": tilde(fp), "keys": [k for _, _, k in items]})
        except Exception as e:
            failed = []
            for fp, before in reversed(touched):
                try:
                    if not fp.exists() or fp.read_bytes() != before:
                        _atomic_write(fp, before.decode("utf-8"), fp.stat().st_mode & 0o777 if fp.exists() else 0o644)
                except Exception:
                    failed.append(tilde(fp))
            if failed:   # never claim "nothing changed" when a restore failed; rollback has the set
                raise DraftError(f"apply failed ({type(e).__name__}) and {', '.join(failed)} could not be "
                                 f"restored: use Rollback (backup {bdir.name})", 500) from None
            manifest["rolled_back"] = True
            manifest["aborted"] = type(e).__name__
            try:
                _atomic_write(bdir / "manifest.json", json.dumps(manifest, indent=2) + "\n", 0o600)
            except Exception:
                pass
            raise DraftError(f"apply failed, nothing changed: {type(e).__name__}", 500) from None
        manifest["complete"] = True
        _atomic_write(bdir / "manifest.json", json.dumps(manifest, indent=2) + "\n", 0o600)
        for w in written:
            for k in w["keys"]:
                draft["changes"].pop(k, None)
        save_draft(paths, draft)
    return {"applied": written, "plans": plans, "remaining": len(draft["changes"]),
            "note": "Restart-class changes stay in the draft until \"Apply + restart when idle\" runs.",
            "last_backup": last_backup(paths)}


def rollback(paths: Paths) -> dict:
    sets = [(d, m) for d, m in _backup_sets(paths) if not m.get("rolled_back")]
    if not sets:
        raise DraftError("no backup to restore", 404)
    d, m = sets[-1]
    restored, warnings = [], []
    # 1. check every backup before the first write (no half rollback on a bad backup)
    todo = []
    for f in m["files"]:
        fp = Path(f["path"])
        src = d / f["backup"]
        if not src.is_file() or Path(f["backup"]).name != f["backup"]:
            raise DraftError(f"backup file missing: {f['backup']}", 500)
        data = src.read_bytes()
        if _sha(data) != f.get("sha_before"):
            raise DraftError(f"backup {f['backup']} does not match its digest", 500)
        todo.append((f, fp, data))
    # 2. restore; keep any later hand edit in a NEW file (a retry never overwrites an earlier copy)
    for f, fp, data in todo:
        try:
            cur = fp.read_bytes()
        except OSError:
            cur = None
        if cur is not None and _sha(cur) == f.get("sha_before"):
            restored.append(tilde(fp))   # already restored (an earlier, interrupted rollback)
            continue
        if cur is not None and _sha(cur) != f.get("sha_after"):
            keep = d / f"{f['backup']}.pre-rollback.{time.strftime('%Y%m%dT%H%M%S')}.{_sha(cur)[:8]}"
            if not keep.exists():
                keep.write_bytes(cur)
                os.chmod(keep, 0o600)
            warnings.append(f"{tilde(fp)} changed after the apply; the current content is kept in {tilde(keep)}")
        mode = fp.stat().st_mode & 0o777 if fp.exists() else 0o644
        _atomic_write(fp, data.decode("utf-8"), mode)
        restored.append(tilde(fp))
    m["rolled_back"] = True
    m["rolled_back_at"] = time.time()
    _atomic_write(d / "manifest.json", json.dumps(m, indent=2) + "\n", 0o600)
    return {"restored": restored, "backup": d.name, "warnings": warnings, "last_backup": last_backup(paths)}


def restart_changes(paths: Paths, proxy: str) -> list:
    """The draft's restart-class proxy-env changes for one proxy, re-validated:
    [{"key", "id", "name", "value"}]. Locked or secret ids raise (403)."""
    draft = load_draft(paths)
    settings = all_settings(paths)
    out = []
    for key, c in sorted(draft["changes"].items()):
        s = check_editable(c.get("id", ""), settings)
        if s["scope"] != "proxy_env" or s["class"] != "restart" or (c.get("proxy") or "main") != proxy:
            continue
        out.append({"key": key, "id": s["id"], "name": s["name"], "value": validate_value(s, c.get("value"))})
    return out


def clear_restart_changes(paths: Paths, proxy: str, keys: list) -> None:
    """Drop applied restart changes from the draft (after a healthy restart)."""
    draft = load_draft(paths)
    for k in keys:
        draft["changes"].pop(k, None)
    save_draft(paths, draft)
