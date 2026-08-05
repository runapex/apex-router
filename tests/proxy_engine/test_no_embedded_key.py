"""No embedded signing key — the grep IS the test (WP1a, pre-distribution).

A distributed (obfuscated) wheel that ships ANY hardcoded HMAC key makes `verify()` decorative for
every external user: they all share the key and can forge a "signed" policy. The per-install key
(`resolve_seal_key`) removed the old `"apex-policy-v1"` default — this test makes its ABSENCE a
standing contract by scanning the whole source tree, so a future re-introduction of an embedded key
fails CI. Same meta-assertion pattern as the secrets canary: the guard also proves non-vacuity.
"""
from __future__ import annotations

import re
from pathlib import Path

# The retired default, plus any string literal assigned to a key-shaped name — shapes a hardcoded
# signing key would take. A real key is bytes from secrets/env/file, NEVER a source literal.
_RETIRED_DEFAULT = "apex-policy-v1"
# `_SEAL_KEY = "..."` / `POLICY_KEY = b"..."` / `default...key... = "..."` — a literal on the RHS.
_KEYLIT_RE = re.compile(
    r"""(?ix)
    \b (\w*(?:seal|policy|hmac|signing)\w*key\w*) \s* [:=] \s*   # a key-shaped identifier
    (?: b?["'] [^"']+ ["'] )                                     # assigned a STRING/BYTES literal
    """,
    re.VERBOSE,
)


def _source_files() -> list[Path]:
    root = Path(__file__).resolve().parents[2] / "src" / "apex_router" / "proxy_engine"
    return [p for p in root.rglob("*.py") if "__pycache__" not in str(p)]


def test_no_hardcoded_signing_key_literal_in_source():
    offenders = []
    for p in _source_files():
        text = p.read_text(encoding="utf-8")
        if _RETIRED_DEFAULT in text:
            offenders.append(f"{p}: retired default {_RETIRED_DEFAULT!r} reintroduced")
        for m in _KEYLIT_RE.finditer(text):
            # env-default like os.environ.get("APEX_POLICY_KEY", <literal>) is the exact hole — the
            # regex catches `= "literal"`; allow the env NAME itself (a str key, not a signing key).
            line = text[: m.start()].count("\n") + 1
            snippet = m.group(0)
            if 'os.environ.get("APEX_POLICY_KEY"' in text.splitlines()[line - 1]:
                # env READ is fine ONLY if no literal fallback; the regex fired on the fallback.
                offenders.append(f"{p}:{line}: APEX_POLICY_KEY has a literal fallback")
            else:
                offenders.append(f"{p}:{line}: key-shaped name assigned a literal: {snippet!r}")
    assert not offenders, "embedded signing key literal(s) found:\n" + "\n".join(offenders)


def test_the_grep_is_not_vacuous():
    """Meta-assertion (canary pattern): the detector MUST fire on a planted key literal, else
    a real embedded key would pass silently."""
    planted = '_SEAL_KEY = "apex-policy-v1"'
    assert _RETIRED_DEFAULT in planted
    assert _KEYLIT_RE.search(planted), "key-literal regex does not fire on a planted key"
    # and a legitimate line (env read, no literal fallback) must NOT trip it
    legit = 'env = os.environ.get("APEX_POLICY_KEY")'
    assert not _KEYLIT_RE.search(legit), "the regex false-positives on a bare env read"
