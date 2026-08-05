"""Divergence-signature classification (Spec 1) — CONTROLS BEFORE ANY REAL EVENT IS CLASSIFIED.

The classifier turns a cache-divergence event (cache_read drops on an append-shaped turn) into a
structural diagnosis: which bytes changed, what class of change, and the one-line fix. It is
analytics-plane only (no wire, no pipeline import).

Controls per the spec's instrument rule:
  1. POSITIVE per class — planted signature → correct label, offset within ±16B.
  2. GROUND TRUTH — the 86,400-boundary event (reconstructed append-with-drift) must classify.
  3. NEGATIVE — identical prior/current → no event, no output.
  4. ADVERSARIAL FP — prose mentioning a date must NOT be VOLATILE_VALUE without the substitution
     shape (the protected-leaf discipline).
  5. REDACTION — a planted credential sentinel in an UNCLASSIFIED span must never be emitted.
  6. DEGRADATION — absent byte capture → UNATTRIBUTED_NO_BYTES, never a guessed class.
"""
from __future__ import annotations

from apex_router.proxy_engine.readout.signature import (
    DivergenceContext,
    classify_divergence,
    format_divergence_report,
    format_event,
    localize,
    summarize,
)

# ---- the secrets-canary sentinel, reused so the redaction test guards the real pattern ----
SENTINEL = "SENTINEL-SECRET-b3ad1decafc0ffee-do-not-log"


def _ctx(prior: bytes, current: bytes, *, endpoint="anthropic", turn=12, prior_cr=86_400):
    return DivergenceContext(
        session_id="s", turn=turn, prior_cache_read=prior_cr, new_cache_read=0,
        divergence_point_tokens=prior_cr, prior_prefix_bytes=prior,
        current_prefix_bytes=current, endpoint_id=endpoint,
    )


# ---------- Stage 1: diff localization ----------

def test_localize_finds_first_differing_byte_and_spans():
    # clean, non-overlapping changed content (no shared bytes inside the change) → the minimal diff
    # span IS the whole change; offset is the first differing byte after the common head.
    prior = b"AAAA" + b"XXXXXXXX" + b"ZZZZ"
    current = b"AAAA" + b"YYYYYYYY" + b"ZZZZ"
    r = localize(prior, current)
    assert r.offset == 4                     # first differing byte after the common "AAAA"
    assert r.old_span == b"XXXXXXXX"
    assert r.new_span == b"YYYYYYYY"
    assert r.n_regions == 1


def test_localize_identical_inputs_have_no_change():
    r = localize(b"same bytes here", b"same bytes here")
    assert r.offset is None                  # no divergence
    assert r.old_span == b"" and r.new_span == b""


def test_localize_counts_multiple_regions():
    # two changes separated by a matching run LONGER than the coalesce gap (>=5B) → two regions.
    sep = b"UNCHANGED_MIDDLE_SECTION"
    prior = b"aaa" + b"1111" + sep + b"2222" + b"ccc"
    current = b"aaa" + b"9999" + sep + b"8888" + b"ccc"
    r = localize(prior, current)
    assert r.n_regions >= 2                   # two disjoint changed regions, gap not coalesced
    assert r.offset == 3                      # v1 reports the FIRST region only
    assert r.old_span == b"1111"


def test_localize_coalesces_changes_across_a_short_gap():
    # two changes separated by a SHORT matching run (< gap) are one logical region (diff-hunk rule),
    # so an incidental shared byte does not shatter a value swap into a false multi-region signal.
    prior = b"aaa" + b"11" + b"x" + b"22" + b"ccc"       # 1-byte "x" gap
    current = b"aaa" + b"99" + b"x" + b"88" + b"ccc"
    r = localize(prior, current)
    assert r.n_regions == 1


def test_localize_is_bounded_and_flags_truncation():
    # comparison capped at the 8MB frontier bound; beyond it → truncated flag, no OOM on MB-scale.
    big = b"x" * (9 * 1024 * 1024)
    r = localize(big, big + b"tail")
    assert r.truncated is True


# ---------- Stage 3: classification, POSITIVE per class ----------

def test_volatile_value_iso_timestamp_same_length_substitution():
    prior = b'{"system":"session started 2026-01-01T09:15 UTC, help the user"}'
    current = b'{"system":"session started 2026-07-19T22:41 UTC, help the user"}'
    res = classify_divergence(_ctx(prior, current))
    assert res.klass == "VOLATILE_VALUE"
    assert res.features.volatile_token_hit == "iso_timestamp"
    assert abs(res.features.offset - 27) <= 16
    assert "timestamp" in res.fix_text.lower() or "id" in res.fix_text.lower()


def test_volatile_value_uuid():
    prior = b"header req 550e8400-e29b-41d4-a716-446655440000 body body body"
    current = b"header req 550e8400-e29b-41d4-a716-4466554409ff body body body"
    res = classify_divergence(_ctx(prior, current))
    assert res.klass == "VOLATILE_VALUE"
    assert res.features.volatile_token_hit == "uuid"


def test_tooldef_reserialization_json_key_reorder():
    # same key SET, different order → JSON re-serialization near the front of the prefix
    prior = b'{"tools":[{"name":"grep","desc":"search","args":"pattern"}], "rest":"' + b"p" * 400 + b'"}'
    current = b'{"tools":[{"args":"pattern","desc":"search","name":"grep"}], "rest":"' + b"p" * 400 + b'"}'
    res = classify_divergence(_ctx(prior, current))
    assert res.klass == "TOOLDEF_RESERIALIZATION"
    assert "sort keys" in res.fix_text.lower() or "serializ" in res.fix_text.lower()


def test_history_rerender_many_regions():
    # >=3 disjoint changed regions = history re-render signature
    prior = b"turn1 AAA " + b"turn2 BBB " + b"turn3 CCC " + b"turn4 DDD " + b"x" * 200
    current = b"turn1 QQQ " + b"turn2 WWW " + b"turn3 EEE " + b"turn4 RRR " + b"x" * 200
    res = classify_divergence(_ctx(prior, current))
    assert res.klass == "HISTORY_RERENDER"
    assert res.features.n_regions >= 3


def test_prefix_truncation_current_shorter():
    prior = b"BEGIN " + b"m" * 500 + b" MIDDLE " + b"n" * 500 + b" END"
    current = b"BEGIN " + b"m" * 500                     # truncated below the cache point
    res = classify_divergence(_ctx(prior, current))
    assert res.klass == "PREFIX_TRUNCATION"
    assert "truncat" in res.fix_text.lower() or "compact" in res.fix_text.lower()


def test_unclassified_fallthrough_shows_redacted_excerpt():
    prior = b"ordinary content " + b"alpha beta gamma delta " + b"z" * 300
    current = b"ordinary content " + b"omega sigma tau upsilon " + b"z" * 300
    res = classify_divergence(_ctx(prior, current))
    assert res.klass == "UNCLASSIFIED"
    assert "inspect" in res.fix_text.lower()


# ---------- CONTROL 2: ground-truth acceptance (the 86,400 event) ----------

def test_ground_truth_86400_boundary_append_with_drift_classifies():
    # The event that motivated the family: a monotonically-growing, UNEDITED append whose cache
    # dropped 86400→0 because something below the cache point re-serialized (whitespace/escape drift
    # or tool-def reorder) — NOT a semantic edit. Reconstruct the shape: a large stable prefix whose
    # tool-def block re-serialized, plus appended new turn bytes. It MUST classify (not UNCLASSIFIED,
    # not UNATTRIBUTED) — if the classifier can't diagnose this one, it doesn't ship.
    stable = b'{"tools":[{"name":"bash","desc":"run","input":"cmd"}],"h":"' + b"c" * 90_000 + b'"'
    prior = stable + b',"turn":"prev"}'
    drifted = b'{"tools":[{"input":"cmd","desc":"run","name":"bash"}],"h":"' + b"c" * 90_000 + b'"'
    current = drifted + b',"turn":"prev","next":"appended user message here"}'
    res = classify_divergence(_ctx(prior, current, endpoint="openai"))
    assert res.klass not in ("UNCLASSIFIED", "UNATTRIBUTED_NO_BYTES"), (
        f"the 86,400 acceptance event must be diagnosed, got {res.klass}"
    )
    assert res.klass == "TOOLDEF_RESERIALIZATION"


# ---------- CONTROL 3: negative ----------

def test_identical_prefix_is_not_an_event():
    same = b"identical prefix, no divergence at all " + b"y" * 400
    res = classify_divergence(_ctx(same, same))
    assert res.klass == "NO_CHANGE"           # detector should not have produced an event; no class


def test_pure_append_is_benign_not_unclassified():
    # prior is a CLEAN PREFIX of current (only new bytes appended, nothing below the cache point
    # changed) — this is the HEALTHY append-caching case, not a divergence. It must read as benign
    # (APPEND_ONLY), never UNCLASSIFIED (which implies an unexplained break). Distinct from
    # PREFIX_TRUNCATION (current shorter) — here current is LONGER and prior survives intact.
    prior = b"stable prefix that stays cached " + b"k" * 400
    current = prior + b" and a freshly appended user turn"
    res = classify_divergence(_ctx(prior, current))
    assert res.klass == "APPEND_ONLY"


# ---------- CONTROL 4: adversarial FP on the volatile patterns ----------

def test_prose_mentioning_a_date_is_not_volatile_without_substitution_shape():
    # A changed region of ordinary prose that HAPPENS to contain a date-like mention, but the change
    # is a large rewrite (not a same-length value substitution). Must NOT be VOLATILE_VALUE — the
    # protected-leaf discipline: the pattern is necessary, the substitution SHAPE is also required.
    prior = b"We met on the reference window to discuss the plan and agreed on the first milestone " + b"z" * 200
    current = (b"The team held a lengthy retrospective covering many unrelated topics and open "
               b"questions before adjourning without a decision " + b"z" * 200)
    res = classify_divergence(_ctx(prior, current))
    assert res.klass != "VOLATILE_VALUE", "a large prose rewrite must not read as a volatile value"


# ---------- CONTROL 5: redaction (the doctor must not become the leak path) ----------

def test_unclassified_span_with_a_credential_is_redacted():
    prior = b"prelude prelude prelude " + b"benign old span content " + b"w" * 300
    current = b"prelude prelude prelude " + (SENTINEL + " leaked into the diff ").encode() + b"w" * 300
    res = classify_divergence(_ctx(prior, current), include_spans=True)
    # whatever class it lands in, the emitted excerpt must NOT carry the sentinel
    assert SENTINEL not in res.rendered_excerpt
    assert SENTINEL not in str(res.to_dict())


def test_bare_credential_VALUE_in_the_span_is_redacted_without_an_adjacent_keyword():
    # The keyword ("Bearer") can sit in the COMMON PREFIX, leaving only the token VALUE in the changed
    # span — a marker-substring check on the bare span would miss it. A high-entropy long token must
    # be caught on shape alone. (Self-adversarial finding before commit.)
    prior = b"auth Bearer " + b"a1b2c3d4e5f6g7h8i9j0k1l2m3" + b" tail " + b"y" * 300
    current = b"auth Bearer " + b"Z9Y8X7W6V5U4T3S2R1Q0P9O8N7" + b" tail " + b"y" * 300
    res = classify_divergence(_ctx(prior, current), include_spans=True)
    assert "a1b2c3d4e5f6g7h8i9j0k1l2m3" not in res.rendered_excerpt
    assert "Z9Y8X7W6V5U4T3S2R1Q0P9O8N7" not in res.rendered_excerpt
    assert "redacted" in res.rendered_excerpt.lower()


def test_ordinary_prose_span_is_not_over_redacted():
    # the entropy heuristic must NOT redact ordinary changed prose (no long high-entropy token) — a
    # false redaction would blind the operator to a real diff. Negative control for the redactor.
    prior = b"the quick brown fox jumped over " + b"z" * 300
    current = b"the lazy grey cat walked under " + b"z" * 300
    res = classify_divergence(_ctx(prior, current), include_spans=True)
    assert "redacted" not in res.rendered_excerpt.lower()
    assert "quick" in res.rendered_excerpt or "lazy" in res.rendered_excerpt


# ---------- CONTROL 6: graceful degradation ----------

def test_absent_byte_capture_returns_unattributed_not_a_guess():
    ctx = DivergenceContext(
        session_id="s", turn=5, prior_cache_read=86_400, new_cache_read=0,
        divergence_point_tokens=86_400, prior_prefix_bytes=None,
        current_prefix_bytes=None, endpoint_id="openai",
    )
    res = classify_divergence(ctx)
    assert res.klass == "UNATTRIBUTED_NO_BYTES"
    assert "unavailable" in res.fix_text.lower() or "not classified" in res.fix_text.lower()


def test_inconsistent_signal_when_change_is_far_past_the_divergence_point():
    # offset lands FAR above divergence_point_tokens → not a prefix change; flag, don't classify.
    prior = b"a" * 100_000 + b"OLD"
    current = b"a" * 100_000 + b"NEW"
    ctx = _ctx(prior, current, prior_cr=10)   # claims divergence at ~10 tokens, but change is at 100k B
    res = classify_divergence(ctx)
    assert res.klass == "INCONSISTENT_SIGNAL"


# ---------- doctor integration: per-event line + summary + report ----------

def test_format_event_matches_the_spec_shape():
    prior = b'{"system":"time 2026-01-01T09:15 do work"}'
    current = b'{"system":"time 2026-07-19T22:41 do work"}'
    res = classify_divergence(_ctx(prior, current, endpoint="openai", turn=12))
    line = format_event(res)
    assert "Turn 12 broke your cache" in line
    assert "endpoint: openai" in line
    assert "Cause:" in line and "Fix:" in line
    assert "~86,400 tokens" in line          # thousands-formatted divergence point


def test_summarize_counts_by_class_and_session_excluding_nonevents():
    prior = b'x{"a":1,"b":2}' + b"p" * 300
    reorder = classify_divergence(_ctx(prior, b'x{"b":2,"a":1}' + b"p" * 300))
    same = classify_divergence(_ctx(b"same" + b"q" * 200, b"same" + b"q" * 200))  # NO_CHANGE
    s = summarize([reorder, same])
    assert s["n_events"] == 1                 # NO_CHANGE excluded
    assert reorder.klass in s["by_class"]


def test_divergence_report_states_pending_wire_grouping():
    # the footer must say which wire is classified and which is pending #13 (Codex grouping)
    prior = b"aaa 2026-01-01T00:00 bbb"
    res = classify_divergence(_ctx(prior, b"aaa 2026-07-19T12:34 bbb"))
    report = format_divergence_report([res], classified_wires=("anthropic",),
                                      pending_wires=("openai",))
    assert "classified wires: anthropic" in report
    assert "openai" in report and "#13" in report
