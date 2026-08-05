"""Per-install seal key — the trust chain must be valid OFF this machine (pre-obfuscation).

The seal is HMAC; its security rests entirely on the KEY. A distributed (obfuscated) artifact that
ships a shared/default key makes `verify()` theater for every external user: anyone with the
artifact has the key and can forge a "signed" policy, and the whole EvidenceBundle edifice rests on
a string everyone has. This is the authority-defaults doctrine (`test_authority_defaults.py`)
applied to the KEY itself — "is this a policy or a bound?": a default key is the signing authority
for every caller who doesn't state one, so there must be NO shared default.

Contract pinned here:
  1. No hardcoded/shared fallback key in the source (the old "apex-policy-v1" default is gone).
  2. A fresh install generates a per-install key (persisted 0600), so two installs seal DIFFERENTLY:
     a policy sealed under install A's key fails `verify()` / `load_verified` under install B's key.
  3. An explicit APEX_POLICY_KEY env still wins (deployment override preserved).
  4. Fail-closed: if no key can be established (no env, unwritable path), signing REFUSES rather
     than minting a default.
"""
from __future__ import annotations

import inspect
import os
import stat

import pytest

import apex_router.proxy_engine.policy as policy_mod
from apex_router.proxy_engine.policy import CONTENT_CLASSES, ClassRule, ExpectedReport, PolicyVersion, T2Policy


def _total_policy() -> PolicyVersion:
    strata = ("xs", "s", "m", "l", "xl")
    raw = ClassRule(transform=None, enabled=False, min_bytes=1 << 30, ratio_floor=0.0)
    rules = {c: {st: raw for st in strata} for c in CONTENT_CLASSES}
    return PolicyVersion(
        version=1, compiled_at=1.0, compiler_hash="h", corpus_hash="c", band=(6.0, 30.0),
        rules=rules, t2=T2Policy(consolidate_on=("ttl",), min_turn_count=5),
        expected=ExpectedReport(0.0, {}),
    )


# 1 — no shared/default key literal survives in the source
def test_no_hardcoded_default_seal_key_in_source():
    src = inspect.getsource(policy_mod)
    assert "apex-policy-v1" not in src, (
        "the old shared default seal key 'apex-policy-v1' is still in the source — an obfuscated "
        "artifact would ship it, making verify() forgeable by every external user"
    )


# 2 — two installs (two generated keys) must seal differently
def test_two_installs_seal_differently(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_POLICY_KEY", raising=False)
    key_a = policy_mod.resolve_seal_key(tmp_path / "install_a")
    key_b = policy_mod.resolve_seal_key(tmp_path / "install_b")
    assert key_a != key_b, "two fresh installs generated the same key — not per-install"
    sealed_a = _total_policy().sealed(key_a)
    assert sealed_a.verify(key_a) is True
    assert sealed_a.verify(key_b) is False, (
        "a policy sealed under install A verifies under install B's key — seal not install-bound"
    )


# 2b — a persisted key is stable across resolves on the SAME install
def test_key_persists_across_resolves(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_POLICY_KEY", raising=False)
    home = tmp_path / "inst"
    k1 = policy_mod.resolve_seal_key(home)
    k2 = policy_mod.resolve_seal_key(home)
    assert k1 == k2, "same install generated a different key on the second resolve — not persisted"


# 2c — the persisted key file is 0600 (not world/group readable)
def test_key_file_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_POLICY_KEY", raising=False)
    home = tmp_path / "inst"
    policy_mod.resolve_seal_key(home)
    key_file = home / "key"
    assert key_file.exists()
    mode = stat.S_IMODE(key_file.stat().st_mode)
    assert mode == 0o600, f"key file mode is {oct(mode)}, must be 0o600 (owner-only)"


# 3 — explicit env override still wins (deployment control)
def test_env_key_overrides_generated(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_POLICY_KEY", "deployment-controlled-key")
    key = policy_mod.resolve_seal_key(tmp_path / "inst")
    assert key == b"deployment-controlled-key"
    # and no key file is written when the env supplies it
    assert not (tmp_path / "inst" / "key").exists()


# 2c-perms — a world/group-READABLE key file is REFUSED (authority fails closed). A secret another
# user can read is compromised; 0600 is the contract, and an existing key wider than that must not be
# adopted (WP1a — multi-tenant trust minimum).
def test_world_or_group_readable_key_is_refused(tmp_path, monkeypatch):
    import stat
    monkeypatch.delenv("APEX_POLICY_KEY", raising=False)
    home = tmp_path / "inst"
    home.mkdir()
    kf = home / "key"
    kf.write_bytes(b"K" * 32)
    kf.chmod(0o644)  # owner rw, GROUP+WORLD readable — a leaked secret
    assert stat.S_IMODE(kf.stat().st_mode) == 0o644
    with pytest.raises(OSError):
        policy_mod.resolve_seal_key(home)


# 2d — TOCTOU: the file appears AFTER our exists()-check but BEFORE our create (another process won
# the race). We must adopt the winner's key, never O_TRUNC over it — else two processes return
# different keys and seals don't cross-verify. This requires O_EXCL: create-exclusive, and on the
# EEXIST loss, read the winner. Force the exact window by writing the file when os.open is first
# called (models the concurrent writer landing in the gap).
def test_concurrent_writer_in_the_race_window_is_adopted(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_POLICY_KEY", raising=False)
    home = tmp_path / "inst"
    home.mkdir()
    winner_key = b"W" * 32
    real_open = os.open
    state = {"raced": False}

    def racing_open(path, flags, mode=0o777):
        # The instant WE try to create the key, a concurrent process has already written it — with
        # OWNER-ONLY perms, exactly as the real writer does (else the adopt path rejects it as leaked).
        if str(path).endswith("/key") and not state["raced"]:
            state["raced"] = True
            (home / "key").write_bytes(winner_key)
            (home / "key").chmod(0o600)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", racing_open)
    adopted = policy_mod.resolve_seal_key(home)
    assert adopted == winner_key, (
        "resolve clobbered the concurrent winner's key (needs O_EXCL + adopt-on-EEXIST) — two "
        "first-run processes would end with different keys and seals would fail to cross-verify"
    )


# 2e — partial/short key file is REFUSED, not adopted as the HMAC key (Codex xval #3). A crash-
# truncated or 1-byte file must fail closed, not become a weak predictable key.
def test_partial_key_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_POLICY_KEY", raising=False)
    home = tmp_path / "inst"
    home.mkdir()
    (home / "key").write_bytes(b"short")  # 5 bytes — a crash-truncated write
    with pytest.raises(OSError):
        policy_mod.resolve_seal_key(home)


# 2f — an explicit empty key argument to compute_seal must NOT silently fall back to the resolved
# key (Codex xval #7): `key or resolve()` truthiness treats b"" as "no key given". A caller passing
# b"" is asserting an (invalid) empty key — reject it rather than substitute a different one.
def test_explicit_empty_key_arg_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_HOME", str(tmp_path / "inst"))
    monkeypatch.delenv("APEX_POLICY_KEY", raising=False)
    pol = _total_policy()
    # sealing with an explicit empty key must raise, not silently seal under the resolved key
    with pytest.raises((ValueError, OSError)):
        pol.compute_seal(b"")


# 4 — fail-closed: no env AND an unwritable home → refuse, never mint a default
def test_unwritable_home_refuses_rather_than_defaulting(tmp_path, monkeypatch):
    monkeypatch.delenv("APEX_POLICY_KEY", raising=False)
    unwritable = tmp_path / "ro"
    unwritable.mkdir()
    os.chmod(unwritable, 0o500)  # r-x, no write
    try:
        # must RAISE (fail-closed), not return a shared default — the OS refuses the write/mkdir
        with pytest.raises(OSError):
            policy_mod.resolve_seal_key(unwritable / "key_home")
    finally:
        os.chmod(unwritable, 0o700)  # restore for cleanup
