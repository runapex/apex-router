"""Reproducible evidence manifests for production policy bundles.

A policy seal proves that one JSON object was not edited after compilation. It does not, by itself,
bind that object to the source tree, full corpus population, tokenizer table, model deployment, or
behavioral transcripts that justified it. This module builds that input identity and verifies every
behavioral transcript before its digest can enter a production bundle.

Offline/compiler plane only. The runtime verifies the resulting plain manifest through
``apex_router.proxy_engine.policy.EvidenceBundle`` without importing this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from apex_router.proxy_engine.tuner.behavioral_gate import load_and_verify_gate_report
from apex_router.proxy_engine.tuner.replay import Request
from apex_router.proxy_engine.tuner.tokens import tokenizer_identity

MANIFEST_SCHEMA_VERSION = 1
_SOURCE_SUFFIXES = {".py", ".toml", ".lock", ".md"}
_SOURCE_EXCLUDES = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "reports",  # evidence outputs are bound separately as artifacts
}


class EvidenceError(ValueError):
    pass


def _canonical_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_file(path: str | os.PathLike[str]) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def source_tree_hash(repo_root: str | os.PathLike[str]) -> str:
    """Hash source-controlled inputs by relative path + bytes, independent of mtimes."""
    root = Path(repo_root).resolve()
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in _SOURCE_SUFFIXES:
            continue
        rel = path.relative_to(root)
        if any(part in _SOURCE_EXCLUDES for part in rel.parts):
            continue
        rel_b = rel.as_posix().encode("utf-8")
        h.update(len(rel_b).to_bytes(4, "big"))
        h.update(rel_b)
        data = path.read_bytes()
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


def _git_state(repo_root: str | os.PathLike[str]) -> tuple[str, bool | None]:
    root = str(Path(repo_root).resolve())
    try:
        revision = subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", root, "status", "--porcelain"], stderr=subprocess.DEVNULL, text=True
        )
        return revision, not bool(status.strip())
    except (OSError, subprocess.CalledProcessError):
        return "unavailable", None


def corpus_content_hash(corpus: Iterable[Request]) -> str:
    """Hash every replay row, not merely the aggregate composition.

    Sorting by stable value identity makes the digest independent of list/object identity while
    retaining all labels that can condition evidence decisions.
    """
    rows = []
    for req in corpus:
        rows.append(
            (
                req.session_id,
                float(req.ts),
                req.model,
                int(req.tokens),
                req.regime,
                req.project or "",
                list(req.message_boundaries) if req.message_boundaries is not None else None,
                hashlib.sha256(req.content).hexdigest(),
                hashlib.sha256(req.frontier_block or b"").hexdigest(),
                req.context_bytes,
                req.prefix_tokens_hint,
                req.diverged_hint,
                len(req.content),
            )
        )
    rows.sort()
    h = hashlib.sha256()
    for row in rows:
        blob = json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        h.update(len(blob).to_bytes(8, "big"))
        h.update(blob)
    return h.hexdigest()


@dataclass(frozen=True)
class EvidenceArtifact:
    label: str
    sha256: str
    bytes: int
    kind: str

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class EvidenceManifest:
    compiled_at: float
    source_revision: str
    source_tree_clean: bool | None
    source_tree_sha256: str
    corpus_content_sha256: str
    policy_corpus_hash: str
    corpus_n_requests: int
    corpus_n_sessions: int
    projects: tuple[str, ...]
    models: tuple[str, ...]
    tokenizer: dict
    compiler_hash: str
    validators: dict[str, str]
    artifacts: tuple[EvidenceArtifact, ...]
    schema_version: int = MANIFEST_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "compiled_at": self.compiled_at,
            "source_revision": self.source_revision,
            "source_tree_clean": self.source_tree_clean,
            "source_tree_sha256": self.source_tree_sha256,
            "corpus_content_sha256": self.corpus_content_sha256,
            "policy_corpus_hash": self.policy_corpus_hash,
            "corpus_n_requests": self.corpus_n_requests,
            "corpus_n_sessions": self.corpus_n_sessions,
            "projects": list(self.projects),
            "models": list(self.models),
            "tokenizer": dict(self.tokenizer),
            "compiler_hash": self.compiler_hash,
            "validators": dict(sorted(self.validators.items())),
            "artifacts": [a.to_dict() for a in self.artifacts],
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_bytes(self.to_dict())).hexdigest()


def _artifact(path: Path, *, root: Path, kind: str) -> EvidenceArtifact:
    try:
        label = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        label = path.name
    return EvidenceArtifact(label, sha256_file(path), path.stat().st_size, kind)


def build_evidence_manifest(
    *,
    repo_root: str | os.PathLike[str],
    corpus: list[Request],
    policy_corpus_hash: str,
    compiler_hash: str,
    compiled_at: float,
    corpus_source_files: Iterable[str | os.PathLike[str]] = (),
    gate_report_paths: Iterable[str | os.PathLike[str]] = (),
    validators: dict[str, str] | None = None,
    require_clean_tree: bool = True,
) -> EvidenceManifest:
    """Build a verified manifest or raise before compilation/signing.

    Gate reports are parsed and every outcome is re-derived from typed transcript records. Merely
    hashing an invalid report is forbidden. A Git checkout must be clean by default; source archives
    without Git metadata remain reproducible through ``source_tree_sha256`` but are marked unknown.
    """
    root = Path(repo_root).resolve()
    revision, clean = _git_state(root)
    if require_clean_tree and clean is False:
        raise EvidenceError("refusing evidence bundle from a dirty source tree")

    artifacts: list[EvidenceArtifact] = []
    for raw in corpus_source_files:
        path = Path(raw)
        if not path.is_file():
            raise EvidenceError(f"corpus source file does not exist: {path}")
        artifacts.append(_artifact(path, root=root, kind="corpus_source"))
    for raw in gate_report_paths:
        path = Path(raw)
        if not path.is_file():
            raise EvidenceError(f"gate report does not exist: {path}")
        try:
            _report, verification = load_and_verify_gate_report(str(path))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise EvidenceError(f"gate report failed verification: {path}: {exc}") from exc
        artifact = _artifact(path, root=root, kind="behavioral_gate")
        if artifact.sha256 != verification.report_sha256:
            # Both hash canonical JSON versus raw file bytes differently; bind the raw artifact and
            # retain the canonical-report digest in its label-independent kind below.
            artifacts.append(artifact)
            artifacts.append(
                EvidenceArtifact(
                    label=f"{artifact.label}#canonical-json",
                    sha256=verification.report_sha256,
                    bytes=artifact.bytes,
                    kind="behavioral_gate_canonical",
                )
            )
        else:
            artifacts.append(artifact)

    sessions = {r.session_id for r in corpus}
    projects = tuple(sorted({r.project for r in corpus if r.project}))
    models = tuple(sorted({r.model for r in corpus if r.model}))
    return EvidenceManifest(
        compiled_at=float(compiled_at),
        source_revision=revision,
        source_tree_clean=clean,
        source_tree_sha256=source_tree_hash(root),
        corpus_content_sha256=corpus_content_hash(corpus),
        policy_corpus_hash=policy_corpus_hash,
        corpus_n_requests=len(corpus),
        corpus_n_sessions=len(sessions),
        projects=projects,
        models=models,
        tokenizer=tokenizer_identity(),
        compiler_hash=compiler_hash,
        validators=validators or {},
        artifacts=tuple(sorted(artifacts, key=lambda a: (a.kind, a.label))),
    )
