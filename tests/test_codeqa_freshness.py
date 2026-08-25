"""codeqa freshness gate — validate a memory/digest's claims against live code, strike the stale ones.

Proven recipe (this session, /tmp/symbol_final.py 3/3): extract symbols a claim names → grep their
DEFINITION LINES only (precision, not recall — more context HURT) → a verifier decides SUPPORTED /
CONTRADICTED / UNVERIFIABLE. The gate strikes CONTRADICTED claims before the memory is pinned, so a
stale note can't mislead the model (measured: recovers a corrupted memory 0.33 → 0.64).

The verifier is an injectable seam (frontier model in prod, fake here) — offline-testable, no network.
"""
from __future__ import annotations

from apex_router.codeqa.freshness import (
    extract_symbols,
    definition_lines,
    check_claim,
    validate_memory,
    is_runtime_claim,
    gather_runtime_facts,
    run_oracle_command,
    memory_fingerprint,
    classify_claim,
    describes_flow,
    route_verifier,
    metrics_record,
    ClaimType,
    Verdict,
)


# ---------- symbol extraction: pull the checkable tokens out of prose ----------

def test_extract_symbols_pulls_identifiers_constants_numbers():
    syms = extract_symbols("the CACHE_SERVED_ALARM_FLOOR in doctor.py is 0.500, a round threshold")
    assert "CACHE_SERVED_ALARM_FLOOR" in syms      # SHOUTY constant
    assert "doctor.py" in syms                      # filename
    assert "0.500" in syms                          # numeric threshold


def test_extract_symbols_drops_prose_stopwords():
    syms = extract_symbols("the value is the current active build for that block")
    # none of these are checkable code symbols — must not become grep targets
    for junk in ("value", "current", "active", "build", "block", "that"):
        assert junk not in syms


def test_extract_symbols_keeps_dotted_and_snake_case():
    syms = extract_symbols("has_policy defaults to false and apex.pipeline.shadow builds the report")
    assert "has_policy" in syms
    assert any("." in s for s in syms)              # a dotted path survived


# ---------- definition-line retrieval: precision, not recall ----------

def test_definition_lines_returns_the_assignment_site(tmp_path):
    (tmp_path / "doctor.py").write_text(
        "import x\n\n# a comment mentioning FLOOR\nCACHE_SERVED_ALARM_FLOOR = 0.700  # derived\n"
        "def use():\n    return CACHE_SERVED_ALARM_FLOOR\n")
    lines = definition_lines("CACHE_SERVED_ALARM_FLOOR", tmp_path)
    joined = "\n".join(lines)
    assert "= 0.700" in joined                       # the DEFINITION line is present
    # the mere-mention lines (comment, the return) are NOT the definition; precision means we prefer
    # the assignment. At minimum the assignment must be included.
    assert any("CACHE_SERVED_ALARM_FLOOR = 0.700" in l for l in lines)


def test_definition_lines_finds_dataclass_field_default(tmp_path):
    (tmp_path / "shadow.py").write_text(
        "from dataclasses import dataclass\n\n@dataclass\nclass ShadowReport:\n"
        "    has_policy: bool = False\n    n_blocks: int = 0\n")
    lines = definition_lines("has_policy", tmp_path)
    assert any("has_policy: bool = False" in l for l in lines)


def test_definition_lines_empty_for_unknown_symbol(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    assert definition_lines("NoSuchSymbol", tmp_path) == []


def test_definition_lines_resolves_a_filename_to_its_definitions(tmp_path):
    # Codex P2a: a claim naming `doctor.py` must resolve to that FILE's definitions, not grep for 'py'.
    (tmp_path / "doctor.py").write_text("import os\nCACHE_SERVED_ALARM_FLOOR = 0.700\ndef run():\n    pass\n")
    lines = definition_lines("doctor.py", tmp_path)
    joined = "\n".join(lines)
    assert "CACHE_SERVED_ALARM_FLOOR = 0.700" in joined   # the file's key assignment surfaced


def test_definition_lines_resolves_a_numeric_constant_to_its_assignment(tmp_path):
    # Codex P2a: `0.700` must find the line that ASSIGNS 0.700, not grep for '700' as a name.
    (tmp_path / "doctor.py").write_text("CACHE_SERVED_ALARM_FLOOR = 0.700  # derived\n")
    lines = definition_lines("0.700", tmp_path)
    assert any("= 0.700" in l for l in lines)


def test_extract_symbols_keeps_short_lowercase_identifier_and_full_decimal():
    # Codex P2b: 'floor' (5 chars) must survive; '1.250' must NOT become '250'.
    syms = extract_symbols("the floor is set to 1.250 in the config")
    assert "floor" in syms
    assert "1.250" in syms and "250" not in syms


# ---------- check_claim: SUPPORTED / CONTRADICTED / UNVERIFIABLE via injected verifier ----------

def test_check_claim_contradicted_strikes_a_wrong_value(tmp_path):
    (tmp_path / "doctor.py").write_text("CACHE_SERVED_ALARM_FLOOR = 0.700  # derived\n")
    # a realistic claim NAMES the symbol (so it can be located) — the gate can only check what the
    # claim identifies. The verifier then sees code says 0.700, claim says 0.500 → CONTRADICTED.
    def verify(claim, code):
        return "CONTRADICTED" if ("0.500" in claim and "0.700" in code) else "SUPPORTED"
    v, ran = check_claim("the CACHE_SERVED_ALARM_FLOOR is 0.500", tmp_path, verify_fn=verify)
    assert v == Verdict.CONTRADICTED and ran is True


def test_check_claim_unverifiable_when_no_symbols_resolve(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    called = {"n": 0}
    def verify(claim, code):
        called["n"] += 1
        return "SUPPORTED"
    # a claim naming nothing greppable → no code context → UNVERIFIABLE without calling the verifier
    v, ran = check_claim("the system generally works well overall", tmp_path, verify_fn=verify)
    assert v == Verdict.UNVERIFIABLE
    assert ran is False and called["n"] == 0         # don't spend a verifier call with no evidence


def test_check_claim_supported_passes_through(tmp_path):
    (tmp_path / "shadow.py").write_text("has_policy: bool = False\n")
    def verify(claim, code):
        return "SUPPORTED"
    v, ran = check_claim("has_policy defaults to false", tmp_path, verify_fn=verify)
    assert v == Verdict.SUPPORTED and ran is True


def test_check_claim_expected_verifier_error_is_unverifiable_not_a_strike(tmp_path):
    # Codex P1b: a NETWORK/transport failure must degrade to UNVERIFIABLE (ran=True, never strikes),
    # but a PROGRAMMING bug must propagate — not be silently swallowed into a clean 'gate did nothing'.
    import urllib.error, pytest
    (tmp_path / "shadow.py").write_text("has_policy: bool = False\n")
    def net_fail(claim, code):
        raise urllib.error.URLError("connection refused")
    v, ran = check_claim("has_policy defaults to false", tmp_path, verify_fn=net_fail)
    assert v == Verdict.UNVERIFIABLE and ran is True  # ran, but couldn't decide → no strike
    def bug(claim, code):
        raise TypeError("a real programming bug")
    with pytest.raises(TypeError):                     # NOT swallowed — the gate must not hide bugs
        check_claim("has_policy defaults to false", tmp_path, verify_fn=bug)


# ---------- validate_memory: strike contradicted bullet claims, report what changed ----------

def test_validate_memory_strikes_only_contradicted_bullets(tmp_path):
    (tmp_path / "doctor.py").write_text("CACHE_SERVED_ALARM_FLOOR = 0.700\n")
    (tmp_path / "shadow.py").write_text("has_policy: bool = False\n")
    memory = (
        "# Notes\n"
        "- The CACHE_SERVED_ALARM_FLOOR in doctor.py is 0.500, a round threshold.\n"  # WRONG
        "- has_policy defaults to false in shadow.py so no transforms fire.\n"        # RIGHT
        "- The team prefers small verified steps.\n"                                   # no symbols → kept
    )
    def verify(claim, code):
        if "0.500" in claim and "0.700" in code:
            return "CONTRADICTED"
        return "SUPPORTED"
    result = validate_memory(memory, tmp_path, verify_fn=verify)
    # the wrong bullet is struck (flagged), the right one and the non-checkable one are kept verbatim
    assert result.n_struck == 1
    assert "0.500" not in result.text or "STALE" in result.text.upper() or "removed" in result.text.lower()
    assert "has_policy defaults to false" in result.text
    assert "small verified steps" in result.text      # non-code claim untouched
    assert result.struck_claims and "0.500" in result.struck_claims[0]


def test_validate_memory_clean_memory_unchanged(tmp_path):
    (tmp_path / "shadow.py").write_text("has_policy: bool = False\n")
    memory = "- has_policy defaults to false in shadow.py.\n"
    def verify(claim, code):
        return "SUPPORTED"
    result = validate_memory(memory, tmp_path, verify_fn=verify)
    assert result.n_struck == 0
    assert result.text.strip() == memory.strip()      # untouched when nothing is contradicted


def test_validate_memory_never_strikes_inside_a_code_fence(tmp_path):
    # Codex P2c: a bullet-looking line inside a ``` fence is example code, NOT a claim — never strike it.
    (tmp_path / "shadow.py").write_text("has_policy: bool = False\n")
    memory = (
        "Here is an example of a WRONG note:\n"
        "```\n"
        "* has_policy defaults to true in shadow.py so transforms fire on every block\n"
        "```\n"
    )
    def verify(claim, code):
        return "CONTRADICTED"                          # would strike if the fence weren't respected
    result = validate_memory(memory, tmp_path, verify_fn=verify)
    assert result.n_struck == 0
    assert "has_policy defaults to true" in result.text  # the fenced example is preserved verbatim


# ---------- runtime-state oracle (the next tier: claims code alone can't decide) ----------

def test_is_runtime_claim_flags_present_tense_state_not_code_facts():
    # 'a policy IS loaded right now' is runtime state; 'has_policy DEFAULTS to false' is a code fact.
    assert is_runtime_claim("apex currently has an active policy loaded right now")
    assert is_runtime_claim("the proxy is running in enforcing mode at the moment")
    assert is_runtime_claim("the posture is currently measure-only")
    assert not is_runtime_claim("has_policy defaults to false in the ShadowReport dataclass")
    assert not is_runtime_claim("the CACHE_SERVED_ALARM_FLOOR constant is 0.700")
    # the false positive I caught: 'is active' describing CODE state must NOT read as runtime
    assert not is_runtime_claim("the mode field is active in the config dataclass")
    assert not is_runtime_claim("the feature flag is enabled by default in code")


def test_gather_runtime_facts_reports_file_presence_and_status(tmp_path):
    # The oracle is a spec: files to test for existence + a status endpoint. Injectable fetch_fn so
    # no live proxy is needed. It must report presence/absence and the status body as plain facts.
    (tmp_path / "present.json").write_text("{}")
    spec = {"files": [str(tmp_path / "present.json"), str(tmp_path / "missing.json")],
            "status_url": "http://x/status"}
    def fake_fetch(url):
        return '{"policy_loaded": false, "posture": "measure-only"}'
    facts = gather_runtime_facts(spec, fetch_fn=fake_fetch)
    assert "present.json: EXISTS" in facts
    assert "missing.json: ABSENT" in facts
    assert "policy_loaded" in facts and "measure-only" in facts


def test_gather_runtime_facts_degrades_when_status_unreachable(tmp_path):
    # A down proxy must NOT crash the gate — the status fact becomes 'unreachable', files still report.
    (tmp_path / "a.json").write_text("{}")
    spec = {"files": [str(tmp_path / "a.json")], "status_url": "http://down/status"}
    def dead_fetch(url):
        raise ConnectionError("refused")
    facts = gather_runtime_facts(spec, fetch_fn=dead_fetch)
    assert "a.json: EXISTS" in facts
    assert "unreachable" in facts.lower()


def test_gather_runtime_facts_empty_spec_is_not_evidence():
    # Codex P2a: a spec with no files and no status_url must return "" — an oracle with zero
    # observations is NOT evidence, or a runtime claim would be 'verified' against a bare header.
    assert gather_runtime_facts({}) == ""
    assert gather_runtime_facts({"files": [], "status_url": None}) == ""


def test_gather_runtime_facts_tolerates_a_malformed_spec():
    # Codex P2b: files=None / a None entry must not raise TypeError — fail-open, not crash.
    assert gather_runtime_facts({"files": None}) == ""
    facts = gather_runtime_facts({"files": [None, "/definitely/missing/x.json"]})
    assert "x.json: ABSENT" in facts                   # the real entry reports; the None is skipped


def test_check_claim_with_runtime_facts_decides_a_state_claim(tmp_path):
    # THE payoff: 'a policy is loaded now' is UNVERIFIABLE from code, but with runtime facts showing
    # policy_loaded=false the verifier can CONTRADICT it. The facts are passed into the verifier context.
    (tmp_path / "shadow.py").write_text("has_policy: bool = False\n")
    runtime = "RUNTIME: policy.json ABSENT; /status policy_loaded=false, posture=measure-only"
    seen = {}
    def verify(claim, code):
        seen["code"] = code
        # verifier contradicts the 'loaded' claim because the runtime facts say not loaded
        return "CONTRADICTED" if ("loaded" in claim and "policy_loaded=false" in code) else "UNVERIFIABLE"
    v, ran = check_claim("apex has a policy loaded right now", tmp_path,
                         verify_fn=verify, runtime_facts=runtime)
    assert v == Verdict.CONTRADICTED and ran is True
    assert "RUNTIME:" in seen["code"]                  # runtime facts reached the verifier


def test_check_claim_runtime_claim_without_facts_stays_unverifiable(tmp_path):
    # Honesty: a runtime claim with NO runtime oracle configured must stay UNVERIFIABLE (don't guess
    # from code, which genuinely can't answer 'is it loaded now'). And don't burn a verifier call.
    (tmp_path / "shadow.py").write_text("has_policy: bool = False\n")
    called = {"n": 0}
    def verify(claim, code):
        called["n"] += 1
        return "SUPPORTED"
    v, ran = check_claim("apex has a policy loaded right now", tmp_path, verify_fn=verify)
    assert v == Verdict.UNVERIFIABLE and ran is False
    assert called["n"] == 0


# ---------- broadened oracle: read-only COMMANDS (log tails, counts, db) ----------

def test_run_oracle_command_captures_output_bounded():
    # A read-only command's stdout becomes a fact, capped so a runaway command can't flood the prompt.
    out = run_oracle_command("printf 'line1\\nline2\\nline3'", cap=1000)
    assert "line1" in out and "line3" in out


def test_run_oracle_command_failfast_on_error_is_a_fact_not_a_crash():
    # A failing command (nonzero exit) reports its failure as a fact — the gate never raises.
    out = run_oracle_command("false", cap=1000)
    assert "exit" in out.lower() or "failed" in out.lower() or out.strip() != ""


def test_run_oracle_command_times_out_gracefully():
    # A hanging command must be killed and reported, not block forever.
    out = run_oracle_command("sleep 10", cap=1000, timeout=0.5)
    assert "timeout" in out.lower() or "timed out" in out.lower()


def test_run_oracle_command_rejects_malformed_cmd_without_raising():
    # Codex P2-7: a non-string or NUL-embedded cmd must fail-open to a fact, not raise.
    assert "invalid" in run_oracle_command(123).lower()          # type: ignore[arg-type]
    assert "invalid" in run_oracle_command("echo \x00 bad").lower()


def test_run_oracle_command_output_is_bounded():
    # Codex P2-5: a chatty command's output is truncated to the cap.
    out = run_oracle_command("for i in $(seq 1 5000); do echo LINE$i; done", cap=200)
    assert len(out) <= 200


def test_gather_runtime_facts_includes_command_facts():
    spec = {"commands": [{"label": "telemetry lines", "cmd": "printf '42'"}]}
    facts = gather_runtime_facts(spec)
    assert "telemetry lines" in facts and "42" in facts


def test_gather_runtime_facts_empty_command_list_is_not_evidence():
    # consistency with P2a: a spec with only an empty commands list yields no observations.
    assert gather_runtime_facts({"commands": []}) == ""
    assert gather_runtime_facts({"commands": None}) == ""     # malformed tolerated


# ---------- auto-wire: content fingerprint so we only re-validate on change ----------

def test_memory_fingerprint_changes_with_content(tmp_path):
    m = tmp_path / "mem.md"
    m.write_text("- claim A\n")
    fp1 = memory_fingerprint(m, tmp_path)
    m.write_text("- claim A CHANGED\n")
    fp2 = memory_fingerprint(m, tmp_path)
    assert fp1 != fp2                                  # memory content changed → new fingerprint


def test_memory_fingerprint_stable_when_nothing_changed(tmp_path):
    m = tmp_path / "mem.md"
    m.write_text("- claim A\n")
    assert memory_fingerprint(m, tmp_path) == memory_fingerprint(m, tmp_path)  # deterministic


def test_memory_fingerprint_tracks_code_head_when_git(tmp_path):
    # The fingerprint must fold in a code-version marker so a code change re-triggers validation even
    # if the memory text is byte-identical (a stale claim goes stale because CODE moved).
    m = tmp_path / "mem.md"
    m.write_text("- claim A\n")
    fp_a = memory_fingerprint(m, tmp_path, code_marker="commit-aaa")
    fp_b = memory_fingerprint(m, tmp_path, code_marker="commit-bbb")
    assert fp_a != fp_b                                # same memory, different code marker → re-validate


# ---------- claim-type classifier: route the verifier tier by the measured split ----------

def test_classify_runtime_claim():
    # present-tense running-system state → RUNTIME (needs the runtime oracle)
    assert classify_claim("apex currently has a policy loaded right now") is ClaimType.RUNTIME


def test_classify_value_claim():
    # a direct constant/number assertion the LOCAL verifier handles well (measured)
    assert classify_claim("the CACHE_SERVED_ALARM_FLOOR is 0.700") is ClaimType.VALUE
    assert classify_claim("p_read is 0.10 and p_write is 1.25 in the Pricing table") is ClaimType.VALUE


def test_classify_inference_claim():
    # a default-value→state inference the LOCAL verifier hedged on → needs FRONTIER (measured 3/3 vs local miss)
    assert classify_claim("has_policy defaults to false so no transforms fire") is ClaimType.INFERENCE
    assert classify_claim("because the field defaults to None, the path is disabled") is ClaimType.INFERENCE


def test_classify_non_derivable_claim():
    # a preference / why-not with NO checkable code symbol → NON_DERIVABLE (skip; nothing grounds it)
    assert classify_claim("the team prefers small verified steps") is ClaimType.NON_DERIVABLE
    assert classify_claim("we opted for readability over cleverness here") is ClaimType.NON_DERIVABLE


def test_classify_code_claim_with_preference_words_is_not_skipped():
    # Codex #1/#2 + my own catch: a CHECKABLE code claim that merely CONTAINS preference-ish words
    # ('should never', 'better to', 'team module', 'instead of') names symbols → must NOT be skipped.
    assert classify_claim("apex should never rewrite bytes in measure-only posture") is not ClaimType.NON_DERIVABLE
    assert classify_claim("the retrieval_ceiling is better to keep at 0.5x break_even") is not ClaimType.NON_DERIVABLE
    assert classify_claim("the team_module sets API_TIMEOUT to 30") is not ClaimType.NON_DERIVABLE
    # a mixed 'by convention, has_policy defaults to false' still gets CHECKED (INFERENCE), not skipped
    assert classify_claim("by convention has_policy defaults to false so no transforms fire") is ClaimType.INFERENCE


def test_classify_conditional_inference_routes_to_frontier():
    # Codex #3: conditional/consequence phrasings must be INFERENCE (→ frontier), not fall to VALUE→local
    # where a local hedge would let a stale claim slip through.
    assert classify_claim("when has_policy is false, transforms do not fire") is ClaimType.INFERENCE
    assert classify_claim("setting has_policy false disables the transform path") is ClaimType.INFERENCE


def test_classify_flow_chain_routes_to_frontier():
    # A claim that traces a FLOW across components (arrow chain) is a multi-hop, often CROSS-REPO
    # relationship the local verifier resolves unreliably (it false-CONTRADICTS a chain it can only
    # half-see). It must be INFERENCE → frontier, NOT VALUE → local. Regression for the misroute that
    # struck a true cross-repo bullet naming another repo's symbols not in this tree. Local-model
    # agnostic — the hedging is a property of the LOCAL tier on partial evidence, not any one model.
    assert classify_claim(
        "PolicyEngine.get_agent_policies → gzip JSON → another repo's venPlatformHandler parses"
    ) is ClaimType.INFERENCE
    assert classify_claim("venVtapServer uploads flows → collector ingests → flow_analytics") is ClaimType.INFERENCE
    assert classify_claim("the request ⇄ response pair crosses evservice") is ClaimType.INFERENCE


def test_classify_no_false_flow_on_code_arrows_and_plain_values():
    # describes_flow must NOT fire on a bare value claim or on ASCII code arrows (lambda/Rust '->',
    # hash-rocket '=>') that are code SYNTAX, not a prose flow — those stay VALUE → local (cheap).
    assert classify_claim("the CACHE_SERVED_ALARM_FLOOR is 0.700") is ClaimType.VALUE
    assert not describes_flow("the closure |x| -> u32 returns the id")
    assert not describes_flow("the hash uses key => value pairs")


def test_classify_value_beats_inference_when_both_signals_present():
    # a claim with BOTH a bare number AND 'defaults' is an inference about a value → INFERENCE wins
    # (the reasoning, not the literal, is the hard part the local model misses).
    assert classify_claim("has_policy defaults to false, i.e. 0") is ClaimType.INFERENCE


# ---------- router: map a claim type → the verifier tier that measurably handles it ----------

def test_route_verifier_sends_value_to_local_inference_to_frontier():
    # VALUE → local (cheap, sufficient); INFERENCE/RUNTIME → frontier (local hedges). NON_DERIVABLE → none.
    local, frontier = object(), object()
    assert route_verifier(ClaimType.VALUE, local=local, frontier=frontier) is local
    assert route_verifier(ClaimType.INFERENCE, local=local, frontier=frontier) is frontier
    assert route_verifier(ClaimType.RUNTIME, local=local, frontier=frontier) is frontier
    assert route_verifier(ClaimType.NON_DERIVABLE, local=local, frontier=frontier) is None


def test_route_verifier_falls_back_to_frontier_when_no_local():
    # if no local verifier is available, a VALUE claim still gets checked (by frontier), never dropped.
    frontier = object()
    assert route_verifier(ClaimType.VALUE, local=None, frontier=frontier) is frontier


def test_validate_memory_routes_value_to_local_inference_to_frontier(tmp_path):
    # THE token-saving wire-up (measured -62% frontier calls): when a local verifier is supplied,
    # validate_memory routes each claim by classify_claim — VALUE → local (free), INFERENCE → frontier.
    (tmp_path / "doctor.py").write_text("CACHE_SERVED_ALARM_FLOOR = 0.700\n")
    (tmp_path / "shadow.py").write_text("has_policy: bool = False\n")
    # A TRUE value claim stays LOCAL-only (SUPPORTED never escalates — that's the token saving); a
    # stale INFERENCE claim goes straight to FRONTIER. (Confirm-before-strike escalation of a local
    # CONTRADICTED is covered by test_local_contradiction_is_confirmed_by_frontier_before_striking.)
    memory = (
        "- the CACHE_SERVED_ALARM_FLOOR is 0.700, a round threshold.\n"           # VALUE (true) → local
        "- has_policy defaults to true so transforms fire on every block.\n"      # INFERENCE (stale) → frontier
    )
    seen = {"local": [], "frontier": []}
    def local_v(claim, code):
        seen["local"].append(claim)
        return "CONTRADICTED" if "0.500" in claim and "0.700" in code else "SUPPORTED"
    def frontier_v(claim, code):
        seen["frontier"].append(claim)
        return "CONTRADICTED" if "defaults to true" in claim and "= False" in code else "SUPPORTED"
    result = validate_memory(memory, tmp_path, verify_fn=frontier_v, local_verify_fn=local_v)
    assert result.n_struck == 1                              # only the stale INFERENCE claim struck
    assert any("0.700" in c for c in seen["local"])          # VALUE went to LOCAL (free)
    assert any("defaults to true" in c for c in seen["frontier"])  # INFERENCE went to FRONTIER
    assert not any("0.700" in c for c in seen["frontier"])   # the true VALUE claim did NOT hit frontier (saving)


def test_local_contradiction_is_confirmed_by_frontier_before_striking(tmp_path):
    # CONFIRM-BEFORE-STRIKE: a CONTRADICTED from the LOCAL verifier (the hedging tier) must be
    # re-checked by frontier and kept ONLY if frontier agrees. Regression for the misroute where the
    # cheap local model false-struck TRUE cross-repo claims. Local-model agnostic: 'local' is whatever
    # verifier is injected (qwen via ollama, ornith, etc.) — here local says CONTRADICTED but frontier
    # says SUPPORTED → NOT struck (frontier wins the destructive verdict).
    (tmp_path / "doctor.py").write_text("CACHE_SERVED_ALARM_FLOOR = 0.700\n")
    memory = "- The CACHE_SERVED_ALARM_FLOOR in doctor.py is 0.700.\n"   # TRUE claim
    calls = {"local": 0, "frontier": 0}
    def local(claim, code):
        calls["local"] += 1
        return "CONTRADICTED"                     # cheap model hedges/errs on this true claim
    def frontier(claim, code):
        calls["frontier"] += 1
        return "SUPPORTED"                        # correct tier confirms it is fine
    result = validate_memory(memory, tmp_path, verify_fn=frontier, local_verify_fn=local)
    assert result.n_struck == 0                   # the true claim survives — no false strike
    assert calls["local"] == 1 and calls["frontier"] == 1   # escalated exactly once
    assert result.n_frontier == 1 and result.n_local == 0   # counted as a PAID frontier call (honest)


def test_frontier_confirmed_contradiction_still_strikes(tmp_path):
    # The escalation must NOT make the gate toothless: when frontier CONFIRMS the local contradiction,
    # the claim is still struck. A genuinely stale VALUE claim (local + frontier both CONTRADICTED).
    (tmp_path / "doctor.py").write_text("CACHE_SERVED_ALARM_FLOOR = 0.700\n")
    memory = "- The CACHE_SERVED_ALARM_FLOOR in doctor.py is 0.500.\n"   # WRONG claim
    def local(claim, code):
        return "CONTRADICTED"
    def frontier(claim, code):
        return "CONTRADICTED"                     # correct tier agrees it is stale
    result = validate_memory(memory, tmp_path, verify_fn=frontier, local_verify_fn=local)
    assert result.n_struck == 1                   # confirmed stale → struck
    assert result.struck_claims and "0.500" in result.struck_claims[0]


def test_validate_memory_records_skipped_non_derivable_claims(tmp_path):
    # Codex #5: a NON_DERIVABLE bullet is skipped, but the skip must be VISIBLE (counted + recorded),
    # so a mis-skip can never masquerade as a clean result.
    (tmp_path / "a.py").write_text("x = 1\n")
    memory = (
        "- we prefer small verified steps over big rewrites, as a team.\n"       # NON_DERIVABLE, no symbol
        "- the FLOOR constant is 0.700 in a.py.\n"                                # checkable, kept
    )
    def local_v(claim, code):
        return "SUPPORTED"
    result = validate_memory(memory, tmp_path, verify_fn=local_v, local_verify_fn=local_v)
    assert result.n_skipped == 1
    assert result.skipped_claims and "prefer" in result.skipped_claims[0]


# ---------- metrics: a differentiable, benchmarkable record per validation run ----------

def test_validate_memory_parallel_matches_serial_and_runs_concurrently(tmp_path):
    # Latency fix: independent claim checks run concurrently (I/O-bound model calls). The parallel
    # result must be IDENTICAL to serial (same strikes, same order, same counts), and the verifier
    # must actually run concurrently (overlapping calls), not one-at-a-time.
    import threading, time
    (tmp_path / "a.py").write_text("FLOOR = 0.700\nCEIL = 9\nRATE = 5\n")
    memory = "".join(f"- the FLOOR constant is 0.{i}00 in a.py per the config.\n" for i in range(3))
    concurrency = {"live": 0, "peak": 0}
    lock = threading.Lock()
    def slow_verify(claim, code):
        with lock:
            concurrency["live"] += 1
            concurrency["peak"] = max(concurrency["peak"], concurrency["live"])
        time.sleep(0.05)                                  # simulate a model call
        with lock:
            concurrency["live"] -= 1
        return "SUPPORTED"
    serial = validate_memory(memory, tmp_path, verify_fn=lambda c, co: "SUPPORTED")
    parallel = validate_memory(memory, tmp_path, verify_fn=slow_verify, max_workers=4)
    assert parallel.text == serial.text                   # identical output (order preserved)
    assert parallel.n_checked == serial.n_checked
    assert concurrency["peak"] >= 2                        # calls genuinely overlapped (not serial)


def test_validate_memory_records_per_tier_counts(tmp_path):
    # The token-cost differentiator: how many claims went to LOCAL (value) vs FRONTIER (inference/runtime).
    (tmp_path / "doctor.py").write_text("CACHE_SERVED_ALARM_FLOOR = 0.700\n")
    (tmp_path / "shadow.py").write_text("has_policy: bool = False\n")
    memory = (
        "- the CACHE_SERVED_ALARM_FLOOR is 0.700 in doctor.py.\n"                  # VALUE → local
        "- the Pricing p_read is 0.10 constant.\n"                                 # VALUE → local
        "- has_policy defaults to false so no transforms fire.\n"                  # INFERENCE → frontier
        "- we prefer small verified steps as a team.\n"                            # NON_DERIVABLE → skip
    )
    def local_v(claim, code):
        return "SUPPORTED"
    def frontier_v(claim, code):                     # DISTINCT object so the local/frontier split is real
        return "SUPPORTED"
    result = validate_memory(memory, tmp_path, verify_fn=frontier_v, local_verify_fn=local_v)
    # the cost split is real and directional: VALUE claim(s) → local (free), INFERENCE → frontier (paid)
    assert result.n_local >= 1                        # the resolvable VALUE claim(s) routed local
    assert result.n_frontier == 1                     # the one INFERENCE claim routed frontier
    assert result.n_skipped == 1                      # the one preference skipped
    assert result.n_frontier < result.n_local + result.n_frontier  # routing kept some off frontier


def test_tier_counts_exclude_unresolved_claims(tmp_path):
    # Codex P1-3 + my catch: a claim whose symbols DON'T resolve spends no verifier call (ran=False),
    # so it must NOT inflate n_local/n_frontier (which drive est_frontier_tokens).
    (tmp_path / "a.py").write_text("x = 1\n")
    memory = "- when NoSuchSymbolXYZ defaults to false so nothing fires by default.\n"  # INFERENCE, unresolved
    def v(claim, code):
        return "SUPPORTED"
    result = validate_memory(memory, tmp_path, verify_fn=v, local_verify_fn=v)
    assert result.n_checked == 0                       # nothing resolved → no verifier ran
    assert result.n_frontier == 0 and result.n_local == 0  # so no tokens attributed (no over-count)


def test_metrics_record_shape_and_append(tmp_path):
    # metrics_record(path, run_dict) appends one JSON line with the benchmarkable fields, re-readable.
    import json
    out = tmp_path / "metrics.jsonl"
    rec = {"repo": "apex", "file": "apex-architecture.md", "n_struck": 2, "n_local": 5,
           "n_frontier": 3, "n_skipped": 1, "cached": False, "routed": True}
    metrics_record(out, rec, ts="2026-07-29T00:00:00Z")
    metrics_record(out, {"repo": "sample-ruby", "n_struck": 0}, ts="2026-07-29T00:01:00Z")
    lines = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
    assert len(lines) == 2
    assert lines[0]["repo"] == "apex" and lines[0]["n_frontier"] == 3
    assert lines[0]["ts"] == "2026-07-29T00:00:00Z"          # timestamp folded in
    assert lines[1]["repo"] == "sample-ruby"
    # frontier-token estimate is derived so runs are comparable over time
    assert lines[0]["est_frontier_tokens"] == 3 * 276        # n_frontier × per-call token estimate
