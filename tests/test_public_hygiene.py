"""No private references in tracked files.

Banned identifiers are stored only as SHA-256 digests of normalised tokens (lower-case,
non-alphanumerics stripped), so this file does not disclose what it bans. A file is normalised
the same way: lower-case, drop every non-alphanumeric, then every fixed-length window of that
stream (at each banned identifier's length) is hashed and checked. Spaced, hyphenated, snake,
camel, concatenated, embedded (compound, prefixed, inflected) spellings are all caught.
Personal home paths are matched by a generic pattern.
"""
import hashlib
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BANNED_DIGESTS = frozenset({
    "ab4187e2ca08d9fa6fea5a62812f567d906605aecf19b19dfd085f10b24c73ea",  # banned identifier #1
    "02540942346fdfbf21a94f8b823af2ae315f20cb71721c2cc6e205097d560840",  # banned identifier #2
    "4a1da8fe11bd3a4b35a8c53137356c0ad776f75c14f542e27af449b7a83ceeec",  # banned identifier #3
    "af70cb9b55dd3ea2dcdba90b0f896b11c8d9b4a5b564ffa066dfc4c47caf17a6",  # banned identifier #4
    "8ea0e5a8f99bc0fbe1c9c3c51a323ef485b12920e377571450fd10d7227d3890",  # banned identifier #5
    "e664ac2dd226e724553870232df15232b465f989e79f6e262bfa874315f6886c",  # banned identifier #6
    "90ec9f23d73fb18e2b8c4c7556aa6ac25172563d73c1af3793c4c2202f784ec0",  # banned identifier #7
    "f08164c31e9f6fc094f9063c7fd83ee2b96258584aa9e090fb404877ef99af84",  # banned identifier #8
})
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
BANNED_LENGTHS = (6, 9, 10, 11, 12, 13, 23)  # normalised lengths of the banned identifiers
_PLACEHOLDER = r"(?!(?:you|x|u|user|name|me)(?![A-Za-z0-9._]))"  # generic placeholders already public
_END = r"(?=[^A-Za-z0-9._-]|$)"
_HOME = re.compile(
    r"/Users/" + _PLACEHOLDER + r"[A-Za-z0-9._-]+" + _END
    + r"|/home/" + _PLACEHOLDER + r"[A-Za-z0-9._-]+" + _END
    + r"|[A-Za-z]:\\Users\\" + _PLACEHOLDER + r"[A-Za-z0-9._-]+" + _END
    + r"|-Users-" + _PLACEHOLDER + r"[A-Za-z0-9._]+-")

# pre-existing, already public (on origin/main): path -> line numbers
HOME_ALLOWLIST = {
    "tests/proxy_engine/test_m5bS_delta2_bytestrata.py": {37},
    "tests/proxy_engine/test_m5bS_delta9_frontier.py": {108},
    "docs/superpowers/specs/2026-08-21-project-memory-compaction-design.md": {11},
}


def _digest(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def banned_hits(text: str) -> int:
    """Number of distinct banned windows in `text` (normalised stream, every banned length)."""
    stream = _NON_ALNUM.sub("", text.lower())
    n = 0
    for length in BANNED_LENGTHS:
        windows = {stream[i:i + length] for i in range(len(stream) - length + 1)}
        n += sum(1 for w in windows if _digest(w) in BANNED_DIGESTS)
    return n


def home_path_hits(name: str, text: str):
    allowed = HOME_ALLOWLIST.get(name, set())
    return [lineno for lineno, line in enumerate(text.splitlines(), 1)
            if _HOME.search(line) and lineno not in allowed]


def tracked_text_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    for name in out.decode().split("\0"):
        p = ROOT / name
        if not name or not p.is_file():
            continue
        try:
            yield name, p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binaries (png, etc.)


def test_no_banned_identifier_in_any_tracked_file():
    hits = [name for name, text in tracked_text_files() if banned_hits(text)]
    assert hits == []


def test_no_personal_home_path_in_any_tracked_file():
    hits = [(name, ln) for name, text in tracked_text_files() for ln in home_path_hits(name, text)]
    assert hits == []


def test_allowlist_entries_still_exist():
    files = dict(tracked_text_files())
    for name, lines in HOME_ALLOWLIST.items():
        for ln in lines:
            assert _HOME.search(files[name].splitlines()[ln - 1]), (name, ln)


def test_scan_is_not_vacuous():
    files = dict(tracked_text_files())
    assert len(files) > 100 and "README.md" in files


def _local_probes():
    """Probe strings live in an untracked local file (they would disclose the banned list)."""
    f = ROOT / ".superpowers" / "hygiene-probes.txt"
    if not f.is_file():
        return []
    return [tuple(line.split("\t", 1)) for line in f.read_text(encoding="utf-8").splitlines() if "\t" in line]


def test_detector_catches_every_local_probe_class():
    import pytest
    probes = _local_probes()
    if not probes:
        pytest.skip("no local probe file (untracked)")
    missed = [kind for kind, probe in probes
              if not (banned_hits(probe) or home_path_hits("probe.txt", probe))]
    assert missed == []
