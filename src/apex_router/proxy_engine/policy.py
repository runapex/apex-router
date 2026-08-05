"""`PolicyVersion` — the signed, versioned, immutable artifact the compiler emits and the dumb
runtime looks up. §2.2 / §5 (`policy_provenance`).

This module is the ONE thing that crosses the offline/runtime plane boundary: the policy
compiler (`apex_router.proxy_engine.tuner.compiler`, offline) constructs a `PolicyVersion`; the enforcement plane
(hot path) loads it and does nothing but table lookup + pure transforms. It therefore imports
**stdlib only** — no `apex_router.proxy_engine.tuner`, no pipeline, no cachesim — so the hot path can import it
without violating plane separation (`tests/test_plane_separation.py`). All the economics live
in the compiler that *produces* this object; this file is just the shape of the result plus the
provenance seal that lets the runtime reject anything the compiler didn't sign.

Immutability is enforced two ways: the dataclasses are `frozen` (no field reassignment), and the
`seal` is an HMAC over the canonical serialization — a hand-edit to any field changes the bytes
and fails `verify()`, so the registry loads only compiler-emitted policy (the `policy_provenance`
invariant). The runtime has no write path; only the compiler calls `sealed()`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

# The content-class contract. The compiler emits a rule per class; the runtime routes each block
# to exactly one of these and looks up its rule. `opaque` is the spec's fallback (§2.2) for content
# the classifier can't safely place — no transform, ships raw. `file_read` and `diff` are
# first-class (M5a.1 review F3): real "prose" traffic was ~31% line-numbered file reads and some
# diffs, each with DIFFERENT transforms, fidelity rules (line-number gutters are functional, not
# cruft), and economics — lumping them under `prose` conflated three populations and hid `code`
# (file reads classified as prose without a path). Distinct classes now, so admission prices each.
CONTENT_CLASSES: tuple[str, ...] = (
    "terminal",
    "json",
    "code",
    "file_read",
    "diff",
    "prose",
    "opaque",
)

# Fidelity taxonomy v2 (Δ6) — the honest classes a rule's `fidelity_class` must be one of. Mirrors
# `apex_router.proxy_engine.pipeline.transforms.base.Fidelity` (kept here too so the plane-neutral loader can gate
# on it without importing the pipeline). A legacy string (lossless/recoverable/lossy_ccr) on a
# signed rule is a pre-migration artifact and is rejected at load.
FIDELITY_CLASSES: tuple[str, ...] = (
    "wire_canonicalization",
    "self_contained",
    "external_retrieval",
    "ccr_retrieval",
)

# The amortization horizon every Δ$ in an `ExpectedReport` is priced on. `positional` = entry-position
# R (an upper bound, ignores prefix truncation); `effective` = measured R_eff (the survival scan). Two
# denominations are NOT commensurable; `load_verified` fails closed on anything else so a malformed
# signed policy can't activate (the value is default-omitted from the seal only when `positional`).
R_DENOMINATIONS: tuple[str, ...] = ("positional", "effective")

# CLASSIFIER_VERSION: `classify` keys the rule table, so changing it is a BINNING MIGRATION that
# breaks cross-version comparability of composition, per-class `expected`, and the transfer gap G
# (the stratum-versioning trap). It is versioned as its own rarely-changed artifact and folds into
# the compiler_hash — a policy compiled under v2 is not comparable to one under v1. Bump ONLY with a
# deliberate taxonomy change (done now, before any G baseline exists — cost only grows later).
CLASSIFIER_VERSION = 2

# `classify` lives HERE (plane-neutral) rather than in apex_router.proxy_engine.tuner, because BOTH planes need it and
# must agree: the compiler classifies a block to PRICE it, the runtime classifies the same block to
# ROUTE it to the compiled rule — a divergence would look up the wrong cell. Pure structural logic
# (bytes + optional path), no economics/tokenizer, so the hot path may import it without violating
# plane separation. `apex_router.proxy_engine.tuner.tokens` re-exports it for the offline callers.
_CODE_EXT = (".py", ".js", ".ts", ".go", ".rs", ".java", ".c", ".cpp", ".rb", ".sh", ".rkt")
_ESC = "\x1b"
# A file-read block (cat -n / Read tool / grep -n): most lines carry a leading line-number gutter.
_GUTTER_RE = re.compile(r"^\s*\d+[\t:|]")


def classify(text: str, file_path: str = "") -> str:
    """Coarse content class from the blob + optional file path — the shared routing/pricing key.
    Versioned by `CLASSIFIER_VERSION`: a change to these rules is a binning migration."""
    if file_path.lower().endswith(_CODE_EXT):
        return "code"
    t = text.lstrip()
    if t[:1] in "[{":
        try:
            json.loads(t)
            return "json"
        except (json.JSONDecodeError, ValueError):
            pass
    # diff: a unified-diff header or hunk marker (structured, its own transform + fidelity rules).
    if t.startswith("diff --git") or re.search(r"^@@ -\d+", text, re.M):
        return "diff"
    if (
        _ESC in text
        or text.count("\r") > 2
        or re.search(r"^\S+@\S+[:~]", text, re.M)   # user@host shell prompt
        or re.search(r"^\S+\$ ", text, re.M)
    ):
        return "terminal"
    # file_read: a majority of lines carry a line-number gutter (cat -n / Read / grep -n). Distinct
    # from prose because the gutter is FUNCTIONAL (view_range / grep -n / the model reasons in
    # "line N") and number-verbatim under the entity floor — a different transform than prose.
    lines = text.split("\n")
    nonempty = [ln for ln in lines if ln.strip()]
    if nonempty and sum(1 for ln in nonempty if _GUTTER_RE.match(ln)) >= 0.5 * len(nonempty):
        return "file_read"
    return "prose"


# Δ2 (revised, a measurement lesson): CANONICAL BYTE STRATA — the one plane-neutral size-binning
# contract, like `classify()`. BOTH planes bin on the SAME observable — CONTEXT BYTES (the ledger's
# committed_wire_length at runtime; the replayed emission length offline) — via the SAME
# `size_stratum_bytes()`. So a cell's admission evidence covers exactly the population routing to it
# at runtime: misrouting is not "bounded", it's UNDEFINED (no second binning to disagree with).
# This DELETES the earlier byte←token ratio, which was structurally broken: bytes/token varies
# 3.2–4.06 by class, so ANY single ratio misroutes some class in some direction (the hole Codex
# found — the ratio-invariant test only checked the min-bpt class while routing applies to all).
# The design rule under measure-don't-estimate: when two planes can share one observable, share it
# — never bridge them with a distribution-dependent conversion.
#
# Token strata survive only as a DERIVED reporting dimension (computed offline from measured tokens)
# for continuity with the historical "xl ≥128k tokens" numbers — two views, one key. The byte cuts
# track the historical token regimes at typical density.
BYTE_STRATA_BOUNDS = ((8_000, "xs"), (32_000, "s"), (128_000, "m"), (512_000, "l"))
TOKEN_STRATA_BOUNDS = ((2_000, "xs"), (8_000, "s"), (32_000, "m"), (128_000, "l"))  # derived report


def size_stratum_bytes(context_bytes: int) -> str:
    """Canonical size stratum by CONTEXT BYTES — the ONE binning contract both planes use (Δ2
    revised). Runtime routes and the compiler prices with this identical function on the identical
    observable, so compiler-cell-key == runtime-cell-key by construction (the identity the invariant
    test now checks, replacing the density-holed ratio inequality)."""
    for bound, name in BYTE_STRATA_BOUNDS:
        if context_bytes < bound:
            return name
    return "xl"


# HMAC key for the seal — the trust chain's root. In a DISTRIBUTED (obfuscated) artifact a shared
# default key would ship in the wheel, so every external user could forge a "signed" policy and
# `verify()` would be theater. There is therefore NO shared default: the key is either supplied by
# the deployment (`APEX_POLICY_KEY`) or generated PER-INSTALL and persisted 0600. Fail-closed — if
# neither can be established, signing/verifying REFUSES rather than minting a default (the
# authority-defaults doctrine applied to the key: "a default key is the signing authority for every
# caller who doesn't state one").
_KEY_FILENAME = "key"
_KEY_BYTES = 32  # the exact per-install key length (secrets.token_bytes(32)); a shorter file is a
# crash-truncated/partial write and is REFUSED, never adopted as a weak key (cross-validation).


def _read_key_file(key_path: Path) -> bytes | None:
    """Read a persisted key, requiring EXACTLY `_KEY_BYTES` and OWNER-ONLY perms (0600). None if
    absent; RAISE (fail-closed) on a present-but-wrong-length file (empty/truncated) OR a file that
    is GROUP/WORLD-readable (a secret others can read is compromised — authority fails closed)."""
    if not key_path.exists():
        return None
    import stat

    mode = stat.S_IMODE(key_path.stat().st_mode)
    if mode & 0o077:  # any group/other permission bit set
        raise OSError(
            f"seal key file {key_path} has mode {oct(mode)} — group/world-accessible; a secret "
            "others can read is compromised. Refusing (fail-closed). Fix: chmod 600."
        )
    data = key_path.read_bytes()
    if len(data) == _KEY_BYTES:
        return data
    raise OSError(
        f"seal key file {key_path} is {len(data)} bytes, expected {_KEY_BYTES} — a partial/corrupt "
        "key; refusing to sign/verify (fail-closed) rather than use a weak key"
    )


def resolve_seal_key(home: str | os.PathLike | None = None) -> bytes:
    """The seal key for this install. Precedence: explicit `APEX_POLICY_KEY` env (deployment
    control) → a per-install key persisted at `<home>/key` (generated 0600 on first use) → RAISE
    (fail-closed).

    `home` defaults to `$APEX_HOME` or `~/.apex` (matching Config.home) so the runtime and the
    compiler resolve the SAME key without importing Config here (plane-clean). Never returns a
    shared default: two fresh installs get two different keys, so a policy signed on one machine
    does not validate on another — the point (policy_provenance is per-deployment, not global)."""
    env = os.environ.get("APEX_POLICY_KEY")
    if env:
        return env.encode("utf-8")
    base = Path(home) if home is not None else Path(
        os.environ.get("APEX_HOME", str(Path.home() / ".apex"))
    )
    key_path = base / _KEY_FILENAME
    existing = _read_key_file(key_path)  # exact-length or RAISE; None if absent
    if existing is not None:
        return existing
    # Generate + persist a fresh per-install key, 0600. Any failure here fails CLOSED (the caller
    # cannot sign/verify without a key) — we do NOT fall back to a shared constant.
    base.mkdir(parents=True, exist_ok=True)
    new_key = secrets.token_bytes(_KEY_BYTES)
    # O_EXCL, NOT O_TRUNC: create-exclusive so a concurrent first-run writer is never clobbered. If
    # another process won the race between our exists()-check above and here, O_EXCL raises
    # FileExistsError — we then ADOPT the winner's key (read it back) so both processes end with the
    # SAME key and seals cross-verify. 0600 from creation (mode arg; chmod re-asserts vs umask).
    try:
        fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Lost the create race — adopt the winner's key (exact-length required; RAISE on partial).
        adopted = _read_key_file(key_path)
        if adopted is not None:
            return adopted
        raise OSError(
            f"seal key file {key_path} exists but is empty — refusing to sign/verify"
        ) from None
    try:
        os.write(fd, new_key)
    finally:
        os.close(fd)
    os.chmod(key_path, 0o600)
    return new_key


class InvalidPolicy(Exception):
    """Raised by `PolicyVersion.load_verified` when a policy's seal does not verify — the registry
    refuses to load unsigned, tampered, or foreign-key policy (policy_provenance, §5)."""


_DIGEST_UNREADABLE = "unreadable-origin"  # a distinctive NON-HEX sentinel — no real sha256[:16] can
# equal it, so an ENABLED cell whose sealed digest is a real hash MISMATCHES it → load_verified
# refuses (fail-closed). Returned when the origin can't be read (zip-import / custom loader /
# absent file on an obfuscated or sourceless wheel), instead of escaping a raw OSError into the hot
# load path (cross-validation). Distinct from "" (absent/disabled transform), which load_verified
# refuses separately as "unsigned".


def transform_digest(transform_name: str) -> str:
    """Content digest of an INSTALLED transform module's bytes — the identity the compiler seals a
    rule against (Δ3). A code-level change to a transform (a knob default, the marker format,
    `_elide` logic) changes this digest, so a policy sealed against old code fails `load_verified`
    on the new code — instead of silently emitting different bytes under an unchanged signed policy
    (which would break cross-session reproducibility and contaminate G). Stdlib-only (sha256 of the
    module file located via importlib, no execution) so `policy.py` stays plane-clean.

    Return values: `""` for an unknown/None/origin-less transform (a disabled/raw cell carries no
    digest; load refuses an ENABLED cell with empty digest as unsigned). `_DIGEST_UNREADABLE` when
    the origin exists but can't be read (zip-import/custom-loader/obfuscated wheel) — a non-hex
    sentinel that can't match a real sealed digest, so an enabled cell fails CLOSED rather than
    raising a raw OSError. DISTRIBUTION: on an obfuscated/sourceless wheel `spec.origin` may be a
    `.pyc`; the digest then hashes THOSE bytes — which is correct IFF compile and serve run the same
    artifact form (the distribution doctrine: obfuscate/build first, then compile on that form)."""
    if not transform_name:
        return ""
    import importlib.util

    spec = importlib.util.find_spec(
        f"apex_router.proxy_engine.pipeline.transforms.{transform_name}")
    if spec is None or not spec.origin:
        return ""
    try:
        with open(spec.origin, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return _DIGEST_UNREADABLE


@dataclass(frozen=True)
class ClassRule:
    """The per-(class × stratum) enforcement rule — a pure step function the runtime evaluates with
    no estimation (§3). `transform` is a transform name (or None = NONE, ship raw). `enabled` is the
    admission verdict on THIS deployment's composition, at THIS context-size stratum. `min_bytes` is
    COMPILED (not defaulted) — the smallest block size that is Δ$-positive across the band at the
    ceiling, so the worst-block ceiling minimum is taken only over blocks the rule would transform
    and an out-of-range outlier excludes itself (M5a.1 review F1). `ratio_floor` is the minimum
    achieved reduction to emit (else fall back to raw). `retrieval_ceiling` is the max retrieval
    rate at which this cell's compression still nets positive — per stratum, the retrieval cost
    driver is context length L = the stratum (v2.1 §10.4: a per-stratum threshold table, static).

    Δ3 SEALED IDENTITY — the rule pins the exact code + config it was compiled against, all folded
    into the seal (they're in `to_dict`): `knobs` is the compile-time knob snapshot the runtime
    threads into the transform (NOT `{}` — otherwise a knob change is invisible to provenance);
    `transform_version` is `transform_digest(transform)` at compile time (load rejects a mismatch
    vs installed code); `validator_id`/`validator_version` name the pure-function fidelity validator
    the runtime must run for a lossy cell (Δ13); `fidelity_class` is the taxonomy-v2 class (Δ6)."""

    transform: str | None
    enabled: bool
    min_bytes: int
    ratio_floor: float
    retrieval_ceiling: float = 0.0
    knobs: dict = field(default_factory=dict)
    transform_version: str = ""
    validator_id: str | None = None
    validator_version: str = ""
    fidelity_class: str = ""

    def to_dict(self) -> dict:
        return {
            "transform": self.transform,
            "enabled": self.enabled,
            "min_bytes": self.min_bytes,
            "ratio_floor": self.ratio_floor,
            "retrieval_ceiling": self.retrieval_ceiling,
            "knobs": dict(self.knobs),
            "transform_version": self.transform_version,
            "validator_id": self.validator_id,
            "validator_version": self.validator_version,
            "fidelity_class": self.fidelity_class,
        }


@dataclass(frozen=True)
class T2Policy:
    """History-consolidation thresholds (§4 T2), compiled from the survival curve at P25 so the
    runtime never estimates session length. T2 fires only at explicit epoch boundaries whose
    cause is in `consolidate_on`, and only once `turn_count >= min_turn_count`."""

    consolidate_on: tuple[str, ...]
    min_turn_count: int

    def to_dict(self) -> dict:
        return {"consolidate_on": list(self.consolidate_on), "min_turn_count": self.min_turn_count}


@dataclass(frozen=True)
class ExpectedReport:
    """The compiler's signed prediction — the target the transfer gap G grades against (§7).
    `delta_dollars_per_session` is the whole-policy expectation; `by_stratum` decomposes it so a
    zero-leverage stratum can't hide inside a healthy blend.

    `r_denomination` names the amortization horizon every Δ$ here is priced on: `"positional"` = each
    block's entry-position R (n−1−index), an UPPER BOUND that ignores prefix truncation (compaction /
    TTL / divergence); `"effective"` = measured R_eff (the survival scan). A positional-R policy is
    thus MECHANICALLY distinguishable from a future R_eff one — the two are NOT commensurable, and
    comparing them (e.g. a transfer gap across denominations) is the exact "number without its
    population" bug the arc keeps finding, pre-empted in the sealed schema."""

    delta_dollars_per_session: float
    by_stratum: dict[str, float]
    r_denomination: str = "positional"

    def to_dict(self) -> dict:
        return {
            "delta_dollars_per_session": self.delta_dollars_per_session,
            "by_stratum": dict(self.by_stratum),
            "r_denomination": self.r_denomination,
        }

    def _seal_dict(self) -> dict:
        """The sealed form: identical to a pre-`r_denomination` policy when the denomination is the
        default `positional`, so an old signed policy still verifies; a non-default (`effective`)
        denomination IS sealed, making the two cryptographically non-substitutable."""
        d = {
            "delta_dollars_per_session": self.delta_dollars_per_session,
            "by_stratum": dict(self.by_stratum),
        }
        if self.r_denomination != "positional":
            d["r_denomination"] = self.r_denomination
        return d


@dataclass(frozen=True)
class PolicyVersion:
    """The compiled policy table. Signed (`seal`), versioned (`version`), immutable (frozen +
    sealed). Reproducible byte-for-byte across deployments from the same corpus: `compiled_at` is
    a caller-supplied input (never `now()`), and the two hashes anchor reproducibility."""

    version: int
    compiled_at: float
    compiler_hash: str
    corpus_hash: str
    band: tuple[float, float]
    # rules[content_class][stratum] → ClassRule. Conditioning on stratum (context-size bin) is the
    # F1 structural fix: the retrieval-cost driver is context length L = the stratum, so a rule that
    # is dollar-positive on small contexts but negative on huge ones is split into two honest cells
    # instead of one class ceiling poisoned by the worst context (v2.1 §10.4, per-stratum static).
    rules: dict[str, dict[str, ClassRule]]
    t2: T2Policy
    expected: ExpectedReport
    # SHA-256 of the full evidence-input manifest (source tree, complete corpus content,
    # tokenizer table, model ids, validators, and verified behavioral transcripts). Empty on
    # legacy/probe policies; production bundles require it through `EvidenceBundle.load_verified`.
    evidence_manifest_hash: str = ""
    # Δ8 lifecycle metadata (all sealed): `policy_epoch` is monotonic — the registry refuses to
    # activate an older (or equal) epoch without a signed rollback, so a runtime never silently
    # downgrades. `valid_from`/`expires_at` are a validity window (caller-supplied wall-clock, like
    # `compiled_at` — never `now()`); an expired or not-yet-valid bundle is refused at activation.
    policy_epoch: int = 0
    valid_from: float = 0.0
    expires_at: float = float("inf")
    seal: str = ""

    # --- canonical form (the bytes the seal signs; also what corpus/compiler hashes fold into) ---
    def _body(self) -> dict:
        """Everything except the seal, as plain JSON-able data with deterministic ordering."""
        return {
            "version": self.version,
            "compiled_at": self.compiled_at,
            "compiler_hash": self.compiler_hash,
            "corpus_hash": self.corpus_hash,
            "band": list(self.band),
            "rules": {
                cls: {st: self.rules[cls][st].to_dict() for st in sorted(self.rules[cls])}
                for cls in sorted(self.rules)
            },
            "t2": self.t2.to_dict(),
            # r_denomination folds into the seal ONLY when non-default: a `positional` policy seals
            # exactly as before this field existed (back-compat — a policy signed by old code still
            # verifies), while an `effective` (R_eff) policy seals the field so the two denominations
            # are cryptographically non-substitutable. Same convention as `expires_at` inf→null.
            "expected": self.expected._seal_dict(),
            "evidence_manifest_hash": self.evidence_manifest_hash,
            "policy_epoch": self.policy_epoch,
            "valid_from": self.valid_from,
            # inf is not valid JSON; serialize an unbounded expiry as null and restore it on load.
            "expires_at": None if self.expires_at == float("inf") else self.expires_at,
        }

    def canonical_bytes(self) -> bytes:
        """Stable serialization of the body — sorted keys, no whitespace — so the same policy
        hashes and seals identically on any machine."""
        return json.dumps(self._body(), sort_keys=True, separators=(",", ":")).encode("utf-8")

    def compute_seal(self, key: bytes | None = None) -> str:
        """HMAC-SHA256 of the canonical body. Deterministic; independent of the current seal. With
        `key is None`, resolves the per-install key (`resolve_seal_key`); never a shared key. An
        explicit empty key is REJECTED (a caller asserting `b""` is an error, not "use the default"
        — the `key or` truthiness trap that would silently substitute a different key; xval)."""
        if key is not None and not key:
            raise ValueError("compute_seal: explicit empty key is not a valid signing key")
        return hmac.new(
            key if key is not None else resolve_seal_key(),
            self.canonical_bytes(), hashlib.sha256,
        ).hexdigest()

    def sealed(self, key: bytes | None = None) -> PolicyVersion:
        """Return a copy carrying a fresh seal. ONLY the compiler calls this (the runtime has no
        write path to policy — policy_provenance)."""
        from dataclasses import replace

        return replace(self, seal=self.compute_seal(key))

    def verify(self, key: bytes | None = None) -> bool:
        """True iff `seal` matches the canonical body under `key`. The registry gates load on this:
        a hand-edit to any field, or a policy signed under a foreign key, fails here and is
        rejected. Constant-time compare to avoid a timing oracle on the seal."""
        if not self.seal:
            return False
        return hmac.compare_digest(self.seal, self.compute_seal(key))

    def to_dict(self) -> dict:
        d = self._body()
        # The wire form is human-readable and ALWAYS shows the denomination (visibility is the point),
        # even though the seal omits it when default (`_seal_dict`). from_dict reads it back either way.
        d["expected"] = self.expected.to_dict()
        d["seal"] = self.seal
        return d

    @staticmethod
    def from_dict(d: dict) -> PolicyVersion:
        """Reconstruct from `to_dict()` output (e.g. loaded from disk). Does NOT re-seal — the
        loaded seal is preserved so `verify()` can check it against the reconstructed body."""
        return PolicyVersion(
            version=d["version"],
            compiled_at=d["compiled_at"],
            compiler_hash=d["compiler_hash"],
            corpus_hash=d["corpus_hash"],
            band=tuple(d["band"]),  # type: ignore[arg-type]
            rules={
                cls: {st: ClassRule(**r) for st, r in strata.items()}
                for cls, strata in d["rules"].items()
            },
            t2=T2Policy(
                consolidate_on=tuple(d["t2"]["consolidate_on"]),
                min_turn_count=d["t2"]["min_turn_count"],
            ),
            expected=ExpectedReport(
                delta_dollars_per_session=d["expected"]["delta_dollars_per_session"],
                by_stratum=dict(d["expected"]["by_stratum"]),
                r_denomination=d["expected"].get("r_denomination", "positional"),
            ),
            evidence_manifest_hash=d.get("evidence_manifest_hash", ""),
            policy_epoch=d.get("policy_epoch", 0),
            valid_from=d.get("valid_from", 0.0),
            expires_at=float("inf") if d.get("expires_at") is None else d["expires_at"],
            seal=d.get("seal", ""),
        )

    @staticmethod
    def load_verified(d: dict, key: bytes | None = None) -> PolicyVersion:
        """The registry's ONLY load path (policy_provenance, §5): reconstruct then verify the
        seal, refusing anything the compiler didn't sign. Raises `InvalidPolicy` on a broken seal
        so a tampered or foreign-key policy can never reach `rule_for`. The frozen dataclass holds
        mutable dicts (a hand-edit post-load can't be prevented at the type level), so this gate —
        verify-at-load, no other entry — is what makes the seal load-bearing (cross-validation)."""
        policy = PolicyVersion.from_dict(d)
        if not policy.verify(key):
            raise InvalidPolicy(
                "policy seal does not verify — refusing to load unsigned/tampered "
                "policy (policy_provenance)"
            )
        # Δ8: a signed bundle must cite a corpus fingerprint (the frozen-snapshot rule). An empty
        # corpus_hash means the policy was compiled against an unfrozen/unknown corpus — refuse it,
        # so evidence provenance can't be silently dropped.
        if not policy.corpus_hash:
            raise InvalidPolicy(
                "policy has no corpus fingerprint — refusing to load a bundle not tied to a frozen "
                "corpus snapshot (Δ8; frozen-snapshot standing rule)"
            )
        # Structural totality: rule_for's opaque fallback assumes the table covers every content
        # class with a non-empty stratum map. A signed-but-malformed policy (e.g. a compiler bug
        # that dropped `opaque`) would otherwise KeyError on the hot path — reject it here, at load,
        # not at request time (cross-validation).
        for cls in CONTENT_CLASSES:
            if cls not in policy.rules or not policy.rules[cls]:
                raise InvalidPolicy(f"policy table is not total — missing rules for class '{cls}'")
        # Fail-closed on the R denomination: the seal protects against TAMPERING an existing value, but
        # a policy SIGNED with a typo'd/unknown denomination (`efffective`) has a self-consistent seal
        # yet is malformed — no consumer understands it. Reject at the gate rather than activate it
        # (deny-by-default; a missing field already defaulted to `positional` in from_dict — legacy-safe).
        if policy.expected.r_denomination not in R_DENOMINATIONS:
            raise InvalidPolicy(
                f"unknown expected.r_denomination {policy.expected.r_denomination!r} — must be one of "
                f"{R_DENOMINATIONS} (a signed policy with an out-of-schema denomination fails closed)"
            )
        # Δ3+Δ6 sealed-field gates, FAIL-CLOSED (cross-validation): an ENABLED rule naming a transform is
        # valid only if the compiler sealed its provenance — the compiler ALWAYS emits a non-empty
        # transform_version (the installed digest) and fidelity_class. So EMPTY on an enabled
        # transform rule is not "skip the check", it is a forged/malformed bundle → REFUSE. (The old
        # guards `if rule.transform_version …` fail-OPEN on empty, letting a forged rule bypass both
        # the digest and taxonomy checks by leaving the fields blank; provenance is fail-closed.)
        for cls, strata in policy.rules.items():
            for st, rule in strata.items():
                if not (rule.enabled and rule.transform):
                    continue
                if not rule.transform_version:
                    raise InvalidPolicy(
                        f"enabled rule {cls}/{st} '{rule.transform}' has no sealed digest — an "
                        "unsigned/forged rule, refused (Δ3 fail-closed; cross-validation)"
                    )
                installed = transform_digest(rule.transform)
                if rule.transform_version != installed:
                    raise InvalidPolicy(
                        f"transform digest mismatch for {cls}/{st} '{rule.transform}': sealed "
                        f"{rule.transform_version!r} but installed code is {installed!r} — the "
                        "runtime transform changed since compile (Δ3 knob/digest sealing)"
                    )
                if not rule.fidelity_class:
                    raise InvalidPolicy(
                        f"enabled rule {cls}/{st} '{rule.transform}' has no sealed fidelity_class "
                        "— an unsigned/forged rule, refused (Δ6 fail-closed; cross-validation)"
                    )
                if rule.fidelity_class not in FIDELITY_CLASSES:
                    raise InvalidPolicy(
                        f"unknown fidelity_class {rule.fidelity_class!r} for {cls}/{st} — not a "
                        f"taxonomy-v2 class {FIDELITY_CLASSES} (Δ6 migration; legacy policy)"
                    )
        return policy

    def rule_for(self, content_class: str, stratum: str) -> ClassRule:
        """Runtime lookup: the rule for a routed block's (class, stratum) — where `stratum` is the
        size bin of the CURRENT context (`decide()` knows the prefix size at frontier time). Unknown
        class or stratum falls back to `opaque`/raw — the table is total over CONTENT_CLASSES ×
        strata, so the hot path never KeyErrors. Assumes load via `load_verified`."""
        by_stratum = self.rules.get(content_class) or self.rules["opaque"]
        rule = by_stratum.get(stratum)
        if rule is not None:
            return rule
        # class present but this stratum absent → opaque cell for that stratum, else the raw default
        opaque = self.rules["opaque"]
        return opaque.get(stratum) or next(iter(opaque.values()))

    def has_active_policy(self) -> bool:
        """True iff some cell is enabled. A zero-value policy (nothing admitted — the current
        real-corpus state) must NOT run in the live emit path: `decide()` there would be pure risk
        surface bought for exactly $0. The proxy runs SHADOW-ONLY until this is True (M5a.1 review).
        """
        return any(r.enabled for strata in self.rules.values() for r in strata.values())


@dataclass(frozen=True)
class EvidenceBundle:
    """The production artifact: policy + the manifest its seal binds + human evidence.

    A bare `PolicyVersion` proves only that the policy JSON was sealed. This bundle additionally
    proves that the sealed policy cites the exact evidence-input manifest and that the manifest's
    compiler/corpus identities agree with the policy. The runtime uses this loader; probe tests may
    still load a bare policy directly through `PolicyVersion.load_verified`.
    """

    policy: PolicyVersion
    manifest: dict
    evidence: dict
    schema_version: int = 1

    @staticmethod
    def _manifest_digest(manifest: dict) -> str:
        blob = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def to_dict(self) -> dict:
        return {
            "bundle_schema_version": self.schema_version,
            "policy": self.policy.to_dict(),
            "manifest": self.manifest,
            "evidence": self.evidence,
        }

    @staticmethod
    def load_verified(d: dict, key: bytes | None = None) -> EvidenceBundle:
        if not isinstance(d, dict) or "policy" not in d or "manifest" not in d:
            raise InvalidPolicy(
                "production policy path requires an evidence bundle, not a bare PolicyVersion"
            )
        if d.get("bundle_schema_version") != 1:
            raise InvalidPolicy(
                f"unsupported evidence bundle schema {d.get('bundle_schema_version')!r}"
            )
        manifest = d.get("manifest")
        evidence = d.get("evidence")
        if not isinstance(manifest, dict) or not isinstance(evidence, dict):
            raise InvalidPolicy("evidence bundle manifest/evidence must be objects")
        if manifest.get("schema_version") != 1:
            raise InvalidPolicy(
                f"unsupported evidence manifest schema {manifest.get('schema_version')!r}"
            )
        policy = PolicyVersion.load_verified(d["policy"], key)
        digest = EvidenceBundle._manifest_digest(manifest)
        if not policy.evidence_manifest_hash:
            raise InvalidPolicy("policy has no evidence_manifest_hash — refusing production load")
        if not hmac.compare_digest(policy.evidence_manifest_hash, digest):
            raise InvalidPolicy(
                "evidence manifest digest does not match the digest sealed into the policy"
            )
        if manifest.get("policy_corpus_hash") != policy.corpus_hash:
            raise InvalidPolicy("evidence manifest corpus identity does not match policy")
        if manifest.get("compiler_hash") != policy.compiler_hash:
            raise InvalidPolicy("evidence manifest compiler identity does not match policy")
        if float(manifest.get("compiled_at", float("nan"))) != float(policy.compiled_at):
            raise InvalidPolicy("evidence manifest compiled_at does not match policy")
        return EvidenceBundle(policy, manifest, evidence, schema_version=1)


class PolicyRegistry:
    """The runtime's policy holder with ATOMIC, lifecycle-checked activation (Δ8). `activate()`
    validates the incoming bundle FULLY before swapping it in, so a reader calling `current()` never
    observes a half-applied or rejected policy. Three checks, all fail-closed:
      - validity window: `valid_from ≤ now < expires_at`, else refuse;
      - epoch monotonicity: the new epoch must be STRICTLY GREATER than the live one, unless
        `rollback=True` (an explicit, sanctioned downgrade) — so a runtime never silently reverts;
      - provenance: the bundle must already `verify()` (activation re-checks, defence in depth).
    Single-writer by construction (one registry per process); the swap is a single attribute set."""

    def __init__(self) -> None:
        self._current: PolicyVersion | None = None

    def current(self) -> PolicyVersion | None:
        return self._current

    def activate(self, policy: PolicyVersion, *, now: float, rollback: bool = False) -> None:
        """Validate `policy`, and only if every check passes make it current. Raises `InvalidPolicy`
        on any failure WITHOUT mutating the live policy (atomicity)."""
        if not policy.verify():
            raise InvalidPolicy("activation: policy seal does not verify")
        if not policy.corpus_hash:
            raise InvalidPolicy("activation: policy has no corpus fingerprint (Δ8)")
        if not (policy.valid_from <= now < policy.expires_at):
            raise InvalidPolicy(
                f"activation: policy not valid at now={now} (window "
                f"[{policy.valid_from}, {policy.expires_at}))"
            )
        cur = self._current
        if cur is not None and not rollback and policy.policy_epoch <= cur.policy_epoch:
            raise InvalidPolicy(
                f"activation: epoch {policy.policy_epoch} is not newer than live "
                f"{cur.policy_epoch} — a downgrade needs an explicit signed rollback (Δ8)"
            )
        self._current = policy  # atomic swap: single assignment, checks already passed


def transfer_gap(
    realized_delta: float, expected_delta: float, *, eps: float = 1.0, abs_band: float = 1.0
) -> float:
    """The transfer gap G that grades the compiler (§7). Normally `|realized-expected| / expected`,
    but that DIVIDES BY ZERO when `expected` ≈ 0 (a zero-value policy — exactly today's real-corpus
    output would make M5b's gate undefined). Guard: when `|expected| < eps`, fall back to an
    ABSOLUTE-dollar band — G = |realized − expected| / abs_band — so a policy predicted to save
    nothing is graded on how far realized strays from ~0 in dollars, not on a blow-up ratio
    (M5a.1 review). Returns G ≥ 0; the M5b gate is G ≤ 0.3."""
    if abs(expected_delta) < eps:
        return abs(realized_delta - expected_delta) / abs_band
    return abs(realized_delta - expected_delta) / abs(expected_delta)
