"""M5b — the lossy JSON deletion-crusher (fork/lossy-json-crusher).

The first tenant of the lossy_ccr machinery. Deletion-only on the JSON grammar: elide array middles
and long-leaf tails, keep everything retained BYTE-VERBATIM, and carry the original for CCR. The
fidelity floor is three mechanical, pure-function-checkable properties (Fable Q7):
  1. every RETAINED leaf value appears byte-identical in the output (verbatim-of-retained);
  2. every elision marker is COUNTED and LOCATED — cardinality + index range in the emitted bytes,
     so "how many" and "where" are answerable without retrieval;
  3. one intact EXEMPLAR survives per elided-tail array — schema shape is never retrieval-only.
Plus: deterministic pure fn, fail-open on non-JSON, fidelity=lossy_ccr carrying the original, and
adjacent elisions collapse into one marker (F6 — marker overhead must not swamp small savings).

These tests are RED first — the module does not exist yet.
"""

from __future__ import annotations

import json

import pytest

from apex_router.proxy_engine.pipeline.transforms import json_crush
from apex_router.proxy_engine.pipeline.transforms.base import Block


def _arr(n, seed=0):
    """A JSON array of n distinctive records — the crusher's target shape."""
    return json.dumps(
        [
            {"id": 100000 + i + seed, "name": f"item_{i}_{seed}", "status": "active"}
            for i in range(n)
        ],
        indent=2,
    )


# ---- floor 1: verbatim-of-retained ------------------------------------------------------------


def test_retained_values_are_byte_verbatim():
    """Every value the crusher KEEPS must appear byte-identical in the output — deletion never
    rewrites a retained value (the core verbatim floor)."""
    text = _arr(50)
    r = json_crush.run(Block(content=text, tool_name="Read"), {})
    obj = json.loads(text)
    # head + tail records are retained; their distinctive values must be present verbatim
    for rec in obj[:3] + obj[-2:]:
        assert rec["name"] in r.text
        assert str(rec["id"]) in r.text


def test_crusher_actually_reduces_a_large_array():
    """On a large array the crusher must shed bytes (else it's a no-op that shouldn't fire)."""
    text = _arr(200)
    r = json_crush.run(Block(content=text, tool_name="Read"), {})
    assert len(r.text) < len(text)


# ---- floor 2: counted + located elision markers -----------------------------------------------


def test_elision_marker_carries_cardinality_and_range():
    """Q7 addition 1: the marker states HOW MANY elements were elided and their index range, so
    cardinality + position survive in the emitted bytes (no retrieval needed for 'how many')."""
    text = _arr(200)
    r = json_crush.run(Block(content=text, tool_name="Read"), {})
    # 200 elements, keep_head + keep_tail retained → the marker names the elided count and range
    import re

    m = re.search(r"elided (\d+) of (\d+).*?idx (\d+)\D+(\d+)", r.text)
    assert m is not None, f"no counted+located marker in: {r.text[:200]}"
    elided, total = int(m.group(1)), int(m.group(2))
    assert total == 200
    assert elided == 200 - _default_kept(json_crush)


def test_marker_count_matches_actual_elision():
    """The number in the marker must equal the real count of dropped elements — not an estimate."""
    text = _arr(100)
    r = json_crush.run(Block(content=text, tool_name="Read"), {})
    import re

    kept = _default_kept(json_crush)
    m = re.search(r"elided (\d+) of 100", r.text)
    assert m and int(m.group(1)) == 100 - kept


# ---- floor 3: one intact exemplar per elided array --------------------------------------------


def test_elided_array_keeps_an_intact_exemplar():
    """Q7 addition 2: the first element of an elided-tail array survives WHOLE, so the schema shape
    is inferable from the wire, never retrieval-only."""
    text = _arr(200)
    r = json_crush.run(Block(content=text, tool_name="Read"), {})
    obj = json.loads(text)
    first = obj[0]
    # every key of the first record is present (the exemplar carries the full shape)
    for k in first:
        assert f'"{k}"' in r.text or f"{k}:" in r.text


# ---- determinism, fidelity, fail-open, marker collapse ----------------------------------------


def test_run_is_deterministic():
    text = _arr(200)
    a = json_crush.run(Block(content=text, tool_name="Read"), {})
    b = json_crush.run(Block(content=text, tool_name="Read"), {})
    assert a.text == b.text


def test_fidelity_is_lossy_ccr_and_carries_original():
    """A deletion drops content from the wire, so fidelity is lossy_ccr and the original must be
    carried for the CCR store (retrieval path)."""
    text = _arr(200)
    r = json_crush.run(Block(content=text, tool_name="Read"), {})
    assert r.fidelity == "ccr_retrieval"
    assert r.original == text  # the full pre-transform bytes


def test_run_on_non_json_raises_fail_open():
    """run() on non-JSON raises — the pipeline's fail-open signal (ships the original, 200)."""
    with pytest.raises(ValueError):
        json_crush.run(Block(content="not json at all", tool_name="Read"), {})


def test_applies_only_to_compressible_json_arrays():
    """applies() is True for a large JSON array, False for small arrays / non-JSON / objects with
    no elidable array."""
    assert json_crush.applies(Block(content=_arr(200), tool_name="Read")) is True
    assert json_crush.applies(Block(content=_arr(2), tool_name="Read")) is False  # nothing to drop
    assert json_crush.applies(Block(content="plain prose", tool_name="Read")) is False


def test_adjacent_elisions_collapse_to_one_marker():
    """F6: two arrays elided back-to-back must not emit two full markers where structure allows one
    marker overhead must not swamp small savings. Here: a single array yields exactly one marker."""
    text = _arr(200)
    r = json_crush.run(Block(content=text, tool_name="Read"), {})
    assert r.text.count("elided") == 1  # one array → one marker, not per-element


# ---- floor 1b: protected-span leaves — atomic locators/ids never prefix-truncated (Fable F-a) --


def _arr_with_leaf(leaf_value, n=50, key="v"):
    """A large array (so the crush fires on the array) whose records each carry `leaf_value` in a
    string leaf — lets us assert what the crusher does to a RETAINED record's leaf."""
    return json.dumps([{"id": 100000 + i, key: leaf_value} for i in range(n)])


def test_under_budget_locator_leaf_is_kept_verbatim():
    """A locator SHORTER than max_leaf is kept byte-verbatim — no marker cost, and truncation was
    never at issue. Only OVER-budget locators need the degradation rule below."""
    url = "https://api.example.com/v1/get?id=42"  # < max_leaf
    assert len(url) < json_crush.DEFAULT_MAX_LEAF
    r = json_crush.run(Block(content=_arr_with_leaf(url, key="url"), tool_name="Read"), {})
    assert url in r.text


def test_over_budget_url_leaf_is_whole_value_elided_never_partial():
    """Fable Q1 invariant: a locator OVER budget is elided WHOLE (a counted value-marker), never
    prefix-truncated. A partial URL wears completeness; the safe degradation is total absence + a
    marker, so 'any locator present on the wire is whole' holds unconditionally."""
    url = "https://api.example.com/v1/download?file=report&sig=" + "a" * 300
    r = json_crush.run(Block(content=_arr_with_leaf(url, key="download_url"), tool_name="Read"), {})
    assert url not in r.text  # not whole-verbatim (that was the old, savings-capping rule)
    assert "https://api.example.com/v1/download" not in r.text  # and NO partial prefix leaks
    assert "bytes · ccr://" in r.text  # replaced by a counted+located value-marker


def test_over_budget_path_leaf_is_whole_value_elided():
    """A filesystem path over budget → whole-value marker; no partial path prefix reads as real."""
    path = "/home/user/project/" + "/".join(f"segment_{i}" for i in range(30)) + "/file.json"
    assert len(path) > json_crush.DEFAULT_MAX_LEAF
    r = json_crush.run(Block(content=_arr_with_leaf(path, key="src"), tool_name="Read"), {})
    assert "/home/user/project/segment_0" not in r.text  # no partial path
    assert "ccr://" in r.text


def test_over_budget_hash_leaf_is_whole_value_elided():
    """A long hex hash/oid over budget → whole-value marker; no partial hash that looks real."""
    h = "a3f8b2c1d0e9" * 24  # 288 hex chars, > max_leaf
    assert len(h) > json_crush.DEFAULT_MAX_LEAF
    r = json_crush.run(Block(content=_arr_with_leaf(h, key="sha"), tool_name="Read"), {})
    assert h not in r.text
    assert h[:64] not in r.text  # no partial hash prefix


def test_data_uri_payload_is_elided_not_shipped_whole():
    """Fable Q1 FP: a data: URI matches the locator pattern but its base64 PAYLOAD is the most
    truncatable content in JSON — protecting it whole ships a multi-MB blob. The scheme+mime prefix
    is kept (schema signal, safe to show); the payload is elided with a counted+located marker."""
    payload = "A" * 4000  # a big base64-ish payload, >> max_leaf
    uri = "data:image/png;base64," + payload
    r = json_crush.run(Block(content=_arr_with_leaf(uri, key="img"), tool_name="Read"), {})
    assert uri not in r.text  # NOT shipped whole (the exploit)
    assert "data:image/png;base64," in r.text  # mime prefix preserved on the wire
    assert payload not in r.text  # payload elided
    assert "bytes · ccr://" in r.text  # counted+located marker for the payload


def test_data_uri_is_not_claimed_by_the_protected_predicate():
    """Negative control (Fable Q1): a data: URI is NOT an atomic-locator (it's a prefix + bulk
    payload), so it must not be classed protected-verbatim — it has its own payload-eliding path."""
    assert json_crush._is_protected_leaf("data:image/png;base64," + "A" * 300) is False


def test_protected_leaf_predicate_covers_id_hash_url_path():
    """The protected class is the entity floor's: url / path / hash / uuid / opaque single-token id.
    Free text (has whitespace) is NOT protected — it may be truncated with a counted marker."""
    P = json_crush._is_protected_leaf
    assert P("https://api.example.com/download?sig=" + "a" * 300)  # url
    assert P("/home/user/project/very/long/path/to/a/file.json")  # abs path
    assert P("a3f8b2c1" * 8)  # 64-hex hash
    assert P("550e8400-e29b-41d4-a716-446655440000")  # uuid
    assert P("req_011Cbgo9m9K5DAvHm4wK7Att")  # opaque id token


def test_protected_leaf_covers_tokens_with_dash_dot_plus_separators():
    """Adversarial FN controls (standing rule: classifiers need negative + boundary controls): api
    keys, JWTs, and bearer tokens are atomic secrets — a truncated one is a WRONG value that looks
    valid. Their separators are `- . +`, not just `/ : ? # = _`."""
    P = json_crush._is_protected_leaf
    assert P("sk-proj-" + "X" * 80)  # api key (dash)
    assert P(
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0wabcdef"
    )  # jwt (dot)
    assert P("ghp_" + "aB3" * 12)  # github pat (underscore prefix)


def test_free_text_leaf_is_not_protected():
    """Negative control: prose / whitespace-bearing values are NOT protected — they may be truncated
    (a prose prefix is self-evidently partial once the counted marker is on the wire)."""
    P = json_crush._is_protected_leaf
    assert not P("This is a long human readable description that " * 6)  # prose → truncatable
    assert not P(
        "Error: connection refused while contacting the upstream service at this time"
    )  # log
    assert not P(
        "col_a, col_b, col_c, col_d, col_e, col_f, col_g, col_h, col_i, col_j, col_k"
    )  # csv


# ---- floor 2b: when a NON-protected leaf IS truncated, its marker is counted + located (Q7) -----


def test_truncated_prose_leaf_marker_is_counted_and_located():
    """Q7 parity for the leaf path: a truncated free-text leaf must carry a marker naming N of M
    chars and a ccr ref — same 'counted + located' contract as the array marker, so the truncation
    is self-evident on the wire (not a verbatim costume)."""
    prose = "This is a long log line that should truncate. " * 8  # >200 chars, has whitespace
    assert len(prose) > json_crush.DEFAULT_MAX_LEAF
    r = json_crush.run(Block(content=_arr_with_leaf(prose, key="msg"), tool_name="Read"), {})
    import re

    m = re.search(r"elided (\d+) of (\d+) bytes · ccr://", r.text)
    assert m is not None, (
        f"leaf marker must be counted+located ('N of M bytes · ccr://'): {r.text[:300]}"
    )
    assert int(m.group(2)) == len(prose)  # M = the full original leaf length


# ---- helper ------------------------------------------------------------------------------------


def _default_kept(mod) -> int:
    """keep_head + keep_tail under the module's default knobs — the retained element count."""
    return mod.DEFAULT_KEEP_HEAD + mod.DEFAULT_KEEP_TAIL
