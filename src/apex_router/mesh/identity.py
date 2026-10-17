"""Node identity for the LAN mesh: node id, shared secret, config load/save. No network.

Layout under ``mesh_home()`` (dir mode 0700, files mode 0600, atomic writes):

- ``mesh.key``  -- the raw 32-byte shared secret. It never goes on the wire.
- ``node.json`` -- ``{"node_id": "<32 hex>", "name": "<str>"}``.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

SECRET_LEN = 32
NAME_MAX = 64
KEY_FILE = "mesh.key"
NODE_FILE = "node.json"
TOKEN_PREFIX = "apexmesh1:"

_NODE_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")
_B64URL_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")


class MeshNotInitialized(RuntimeError):
    """No usable identity under the mesh home. Run ``apex-router mesh init``."""


def mesh_home() -> Path:
    """``APEX_MESH_HOME`` or ``~/.apex-router/mesh`` (resolved at call time, not import time)."""
    env = os.environ.get("APEX_MESH_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".apex-router" / "mesh"


@dataclass(frozen=True)
class Identity:
    node_id: str
    name: str
    secret: bytes = field(repr=False)   # never in repr / logs

    @property
    def mesh_id(self) -> str:
        """First 16 hex of sha256(b"apex-mesh-id" + secret). Identifies the mesh, hides the secret."""
        return hashlib.sha256(b"apex-mesh-id" + self.secret).hexdigest()[:16]


def clean_name(name: str | None) -> str:
    """Printable, stripped, at most NAME_MAX chars; falls back to the host name, then 'node'."""
    raw = name if name is not None else platform.node()
    out = "".join(ch for ch in str(raw) if ch.isprintable()).strip()[:NAME_MAX].strip()
    return out or "node"


def _ensure_dir(home: Path) -> None:
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home, 0o700)


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to a 0600 temp file in the same dir, fsync, then os.replace."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def _check_secret(secret: bytes) -> bytes:
    if not isinstance(secret, (bytes, bytearray)) or len(secret) != SECRET_LEN:
        raise ValueError(f"mesh secret must be exactly {SECRET_LEN} bytes")
    return bytes(secret)


def init(home: Path | None = None, *, secret: bytes | None = None,
         name: str | None = None, force: bool = False) -> Identity:
    """Create a new identity (new node id; new secret unless ``secret`` is given).

    Without ``force``: a valid existing identity is returned unchanged; FileExistsError if it
    holds a different ``secret`` than the one given, or if partial/corrupt files exist (never
    overwrite a key silently). ``force`` replaces both files.
    """
    home = Path(home) if home is not None else mesh_home()
    if not force and ((home / KEY_FILE).exists() or (home / NODE_FILE).exists()):
        try:
            cur = load(home)
        except MeshNotInitialized as e:
            raise FileExistsError(f"{e}; use force=True to replace it") from e
        if secret is not None and not hmac.compare_digest(_check_secret(secret), cur.secret):
            raise FileExistsError(
                f"a different mesh identity exists in {home}; use force=True to replace it")
        return cur
    key = _check_secret(secret) if secret is not None else secrets.token_bytes(SECRET_LEN)
    ident = Identity(node_id=uuid.uuid4().hex, name=clean_name(name), secret=key)
    _ensure_dir(home)
    _atomic_write(home / KEY_FILE, ident.secret)
    node = json.dumps({"node_id": ident.node_id, "name": ident.name},
                      sort_keys=True, indent=2) + "\n"
    _atomic_write(home / NODE_FILE, node.encode("utf-8"))
    return ident


def load(home: Path | None = None) -> Identity:
    """Read the identity. Raises MeshNotInitialized if files are missing or not valid."""
    home = Path(home) if home is not None else mesh_home()
    key_path, node_path = home / KEY_FILE, home / NODE_FILE
    try:
        secret = key_path.read_bytes()
        node = json.loads(node_path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise MeshNotInitialized(f"no mesh identity in {home}: run `apex-router mesh init`") from e
    except (OSError, ValueError) as e:
        raise MeshNotInitialized(f"mesh identity in {home} is not readable: {e}") from e
    if len(secret) != SECRET_LEN:
        raise MeshNotInitialized(f"{key_path} is not a {SECRET_LEN}-byte key")
    if not isinstance(node, dict):
        raise MeshNotInitialized(f"{node_path} is not a JSON object")
    node_id, name = node.get("node_id"), node.get("name")
    if not isinstance(node_id, str) or not _NODE_ID_RE.match(node_id):
        raise MeshNotInitialized(f"{node_path}: node_id is not 32 lowercase hex")
    if not isinstance(name, str):
        raise MeshNotInitialized(f"{node_path}: name is not a string")
    # Tighten modes if something loosened them (the key is a credential).
    for p in (key_path, node_path):
        try:
            if p.stat().st_mode & 0o077:
                os.chmod(p, 0o600)
        except OSError:
            pass
    return Identity(node_id=node_id, name=clean_name(name), secret=secret)


def export_secret(ident: Identity) -> str:
    """Out-of-band transfer token: ``apexmesh1:<base64url, no padding>``."""
    return TOKEN_PREFIX + base64.urlsafe_b64encode(ident.secret).rstrip(b"=").decode("ascii")


def parse_secret(token: str) -> bytes:
    """Inverse of export_secret. Raises ValueError on any bad token."""
    if not isinstance(token, str):
        raise ValueError("mesh token must be a string")  # noqa: TRY004 -- contract: ValueError
    tok = token.strip()
    if not tok.startswith(TOKEN_PREFIX):
        raise ValueError(f"mesh token must start with {TOKEN_PREFIX!r}")
    body = tok[len(TOKEN_PREFIX):].rstrip("=")
    if not body or not _B64URL_RE.match(body):
        raise ValueError("mesh token is not base64url")
    try:
        raw = base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError("mesh token is not base64url") from e
    if len(raw) != SECRET_LEN:
        raise ValueError(f"mesh token must hold {SECRET_LEN} bytes, got {len(raw)}")
    return raw
