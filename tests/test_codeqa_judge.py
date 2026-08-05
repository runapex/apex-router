"""codeqa A/B judge — the blinded Opus prose-correctness scorer (primary decision axis).

The API call is a seam (call_fn), so scoring + blinding are tested offline. The judge must (a) parse
a 0..1 score tolerantly, (b) be BLIND — its prompt carries no variant label or digest, only question
+ answer + source excerpts, (c) surface a scoring failure instead of silently defaulting.
"""
from __future__ import annotations

from apex_router.codeqa.judge import (
    build_judge_prompt,
    is_transient_judge_error,
    opus_judge_fn,
    parse_judge_score,
)



# ---------- score parsing ----------

def test_parse_score_from_clean_json():
    assert parse_judge_score('{"score": 0.8, "why": "mostly right"}') == 0.8


def test_parse_score_tolerates_surrounding_prose():
    assert parse_judge_score('Here is my grade:\n{"score": 0.5, "why": "vague"}\nDone.') == 0.5


def test_parse_score_rejects_out_of_range_as_protocol_violation():
    # Codex A/B-judge-F6: an out-of-range score is a PROTOCOL VIOLATION, not something to clamp into
    # valid-looking evidence. It must RAISE (to be retried per-item), not silently become 1.0/0.0.
    import pytest
    for bad in ('{"score": 1.4}', '{"score": -0.2}', '{"score": 99}'):
        with pytest.raises(ValueError):
            parse_judge_score(bad)


def test_parse_score_rejects_corrupted_json_not_silently_accepts():
    # the greedy-regex bugs Codex reproduced: a nested score in `why`, a malformed number.
    import pytest
    # nested {"score":0.2} inside `why` must NOT win — the real top-level score is 0.9
    assert parse_judge_score('{"score": 0.9, "why": {"nested_score": 0.2}}') == 0.9
    with pytest.raises(ValueError):
        parse_judge_score('{"score": 0.8.2}')  # malformed number
    with pytest.raises(ValueError):
        parse_judge_score("I could not grade this answer.")  # no JSON at all


# ---------- grades against the LIVE tree (cross-validation fix), blinded ----------

def test_judge_prompt_reads_live_code_at_cited_locations(tmp_path):
    # The judge grades against the ACTUAL current source at the answer's cited lines — not the
    # answerer's excerpts. Write a live file; the prompt must contain its real content.
    (tmp_path / "auth.py").write_text("def login(u):\n    return check_password(u)\n")
    prompt = build_judge_prompt("what does login do?", "login validates via check_password (auth.py:1)",
                                tmp_path)
    assert "what does login do?" in prompt
    assert "check_password" in prompt  # pulled from the LIVE file at auth.py:1
    assert "ground truth" in prompt.lower()


def test_judge_does_not_penalize_correct_claim_absent_from_answerer_excerpts(tmp_path):
    # Codex A/B-judge-F1 (the fatal bug this rewrite fixes): a CORRECT digest-derived claim naming a
    # symbol NOT in the answerer's retrieved excerpts must be graded against the LIVE tree, where the
    # symbol IS real — so it is NOT penalized as 'unsupported'. The prompt must show the live source
    # of the CITED location, letting the judge verify the claim against real code.
    (tmp_path / "handler.py").write_text(
        "class RequestHandler:\n    def handle(self, r):\n        self.q.push(r)  # -> JobQueue\n")
    answer = "RequestHandler.handle delegates the request to the JobQueue (handler.py:3)"
    prompt = build_judge_prompt("how are requests handled?", answer, tmp_path)
    # the judge sees the ACTUAL handler.py:3 (which shows the JobQueue delegation) — so the correct
    # fresh-digest claim can be verified, not blindly scored 0.0 for being outside some excerpt set.
    assert "JobQueue" in prompt and "self.q.push" in prompt


def test_judge_prompt_is_blind_to_variant_and_digest(tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    pass\n")
    prompt = build_judge_prompt("what is foo?", "foo is a function, a.py:1", tmp_path)
    low = prompt.lower()
    assert "fresh" not in low and "stale" not in low and "absent" not in low
    assert "variant" not in low  # 'digest' no longer appears; the judge can't tell fresh from stale


def test_judge_prompt_flags_a_citation_to_a_missing_file(tmp_path):
    prompt = build_judge_prompt("q", "see gone.py:9 for the logic", tmp_path)
    assert "not found" in prompt.lower()  # the judge is told the cited location doesn't exist


# ---------- the judge_fn end to end (fake API seam) ----------

def test_opus_judge_fn_scores_via_injected_call(tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    return 1\n")
    seen = {}
    def fake_call(prompt):
        seen["prompt"] = prompt
        return '{"score": 0.9, "why": "correct and specific"}'
    judge = opus_judge_fn(tmp_path, call_fn=fake_call)
    assert judge("what is foo?", "foo returns 1, a.py:1") == 0.9
    assert "what is foo?" in seen["prompt"] and "variant" not in seen["prompt"].lower()


def test_opus_judge_distinguishes_correct_from_wrong_against_live_code(tmp_path):
    # The judge's discrimination comes from the model seeing the LIVE code: a false claim about the
    # real source scores low, a true one scores high. (Fake models the judgment; the point is the
    # prompt carries the answer + live source so the model CAN discriminate — groundedness could not.)
    (tmp_path / "auth.py").write_text("def login(u):\n    return check_password(u)\n")
    def fake_call(prompt):
        # a real judge would read the live source in the prompt; the fake keys off the answer claim
        return '{"score": 1.0}' if "check" in prompt.split("ANSWER")[1].split("SOURCE")[0] else '{"score": 0.1}'
    judge = opus_judge_fn(tmp_path, call_fn=fake_call)
    good = judge("what does login do?", "login checks the password (auth.py:1)")
    bad = judge("what does login do?", "login sends an email (auth.py:1)")
    assert good == 1.0 and bad == 0.1


# ---------- transient-failure retry (the 8-hole bug: one blip → permanent unscored hole) ----------

def test_transient_classifier_retries_429_timeout_not_401():
    import urllib.error, socket
    # 429 / 503 / timeout are transient (retry); 401 / 400 are permanent (don't waste retries).
    assert is_transient_judge_error(urllib.error.HTTPError("u", 429, "rate", {}, None)) is True
    assert is_transient_judge_error(urllib.error.HTTPError("u", 503, "down", {}, None)) is True
    assert is_transient_judge_error(urllib.error.URLError(socket.timeout("timed out"))) is True
    assert is_transient_judge_error(TimeoutError()) is True
    assert is_transient_judge_error(urllib.error.HTTPError("u", 401, "auth", {}, None)) is False
    assert is_transient_judge_error(urllib.error.HTTPError("u", 400, "bad", {}, None)) is False


def test_transient_classifier_covers_connection_resets_and_529():
    # The Step-3-rerun gap: 10/33 grades failed and PERSISTED through retries because connection
    # resets / broken pipes (common under 33 rapid proxied calls) were classed PERMANENT → 0 retries.
    # Anthropic's 529 'overloaded' is also transient. All of these must be retryable.
    import urllib.error
    assert is_transient_judge_error(urllib.error.HTTPError("u", 529, "overloaded", {}, None)) is True
    assert is_transient_judge_error(urllib.error.URLError(ConnectionResetError("reset"))) is True
    assert is_transient_judge_error(ConnectionResetError("reset")) is True
    assert is_transient_judge_error(ConnectionError("broken pipe")) is True
    assert is_transient_judge_error(urllib.error.URLError(ConnectionError("pipe"))) is True
    # a plain non-connection OSError is NOT assumed transient (could be anything)
    assert is_transient_judge_error(ValueError("bad json")) is False


def test_opus_judge_retries_a_transient_failure_then_succeeds(tmp_path):
    import urllib.error
    (tmp_path / "a.py").write_text("x=1\n")
    calls = {"n": 0}
    def flaky(prompt):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError("u", 429, "rate limited", {}, None)  # transient twice
        return '{"score": 0.7}'
    judge = opus_judge_fn(tmp_path, call_fn=flaky, max_attempts=3, sleep_fn=lambda s: None)
    assert judge("q", "a.py:1") == 0.7  # recovered instead of becoming an unscored hole
    assert calls["n"] == 3


def test_opus_judge_does_not_retry_a_permanent_auth_error(tmp_path):
    import urllib.error, pytest
    (tmp_path / "a.py").write_text("x=1\n")
    calls = {"n": 0}
    def always_401(prompt):
        calls["n"] += 1
        raise urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
    judge = opus_judge_fn(tmp_path, call_fn=always_401, max_attempts=3, sleep_fn=lambda s: None)
    with pytest.raises(urllib.error.HTTPError):
        judge("q", "a.py:1")
    assert calls["n"] == 1  # a bad credential is not going to fix itself — fail fast, don't burn retries


def test_opus_judge_gives_up_after_max_retries_on_persistent_transient(tmp_path):
    import urllib.error, pytest
    (tmp_path / "a.py").write_text("x=1\n")
    calls = {"n": 0}
    def always_429(prompt):
        calls["n"] += 1
        raise urllib.error.HTTPError("u", 429, "rate", {}, None)
    judge = opus_judge_fn(tmp_path, call_fn=always_429, max_attempts=3, sleep_fn=lambda s: None)
    with pytest.raises(urllib.error.HTTPError):
        judge("q", "a.py:1")
    assert calls["n"] == 3  # bounded — 3 attempts, then it's a real (recorded) error


def test_opus_judge_floors_attempts_at_one(tmp_path):
    # Codex P3: max_attempts=0 must NOT become a silent no-op (0 calls, then an AssertionError on the
    # unset last_exc). It floors to a single call — always at least one grade is attempted.
    (tmp_path / "a.py").write_text("x=1\n")
    calls = {"n": 0}
    def ok(prompt):
        calls["n"] += 1
        return '{"score": 0.5}'
    judge = opus_judge_fn(tmp_path, call_fn=ok, max_attempts=0, sleep_fn=lambda s: None)
    assert judge("q", "a.py:1") == 0.5
    assert calls["n"] == 1  # floored to one attempt, not zero


# ---------- protocol-repair retry (THE Step-3-rerun root cause: judge replies PROSE not JSON) ----------

def test_opus_judge_reprompts_when_reply_is_prose_not_json(tmp_path):
    # The real 10/33 failure: on hard/'I cannot answer' cases Opus preambles ("Let me evaluate...")
    # and never emits {"score":...}. That's a PROTOCOL failure, not transient — retrying the SAME
    # prompt won't help, but a CORRECTIVE reprompt does. The 2nd call must carry a stronger
    # format instruction, and the score must be recovered instead of becoming an unscored hole.
    (tmp_path / "a.py").write_text("x=1\n")
    prompts = []
    def prose_then_json(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return "Let me evaluate the claims against the actual source. The answer is mostly right."
        return '{"score": 0.6, "why": "recovered"}'
    judge = opus_judge_fn(tmp_path, call_fn=prose_then_json, max_attempts=3, sleep_fn=lambda s: None)
    assert judge("q", "a.py:1") == 0.6                       # recovered, not a hole
    assert len(prompts) == 2                                  # took exactly one corrective reprompt
    assert prompts[1] != prompts[0]                           # the reprompt is DIFFERENT (corrective)
    assert "json" in prompts[1].lower()                       # and demands JSON explicitly


def test_opus_judge_gives_up_after_max_attempts_on_persistent_prose(tmp_path):
    import pytest
    (tmp_path / "a.py").write_text("x=1\n")
    calls = {"n": 0}
    def always_prose(prompt):
        calls["n"] += 1
        return "I need to think about this more carefully before scoring."
    judge = opus_judge_fn(tmp_path, call_fn=always_prose, max_attempts=3, sleep_fn=lambda s: None)
    with pytest.raises(ValueError):                           # exhausted → the protocol error surfaces
        judge("q", "a.py:1")
    assert calls["n"] == 3                                    # bounded, same budget as transient retries


def test_transport_jsondecode_from_call_is_not_reprompted_as_protocol(tmp_path):
    # Codex P1: a JSONDecodeError raised INSIDE call() (a broken GATEWAY response, not a bad judge
    # reply) is TRANSPORT, not protocol. It must NOT get the corrective JSON reprompt — the prompt
    # must stay the base prompt across the transient retry, and it uses network backoff.
    import json as _json
    (tmp_path / "a.py").write_text("x=1\n")
    prompts, sleeps = [], []
    def broken_gateway(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            raise _json.JSONDecodeError("Expecting value", "", 0)  # gateway returned non-JSON
        return '{"score": 0.4}'
    judge = opus_judge_fn(tmp_path, call_fn=broken_gateway, max_attempts=3, sleep_fn=sleeps.append)
    assert judge("q", "a.py:1") == 0.4
    assert prompts[1] == prompts[0]          # SAME prompt — not the corrective reminder (it's transport)
    assert sleeps == [0.5]                    # transport path used backoff (protocol path would not)


def test_huge_integer_score_is_a_protocol_failure_and_reprompts(tmp_path):
    # Codex P3: float() of a giant JSON int raises OverflowError, not ValueError. parse_judge_score
    # now normalizes it to JudgeProtocolError, so the judge REPROMPTS instead of raising it as a
    # permanent transport error.
    (tmp_path / "a.py").write_text("x=1\n")
    prompts = []
    def huge_then_ok(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            # 5000 digits — ABOVE Python's 4300-digit json.loads int limit, so json.loads itself
            # raises a bare ValueError (not JSONDecodeError, not our float() guard). cross-validation:
            # the 400-digit version was below the limit and never exercised this path.
            return '{"score": ' + ("9" * 5000) + '}'
        return '{"score": 0.5}'
    judge = opus_judge_fn(tmp_path, call_fn=huge_then_ok, max_attempts=3, sleep_fn=lambda s: None)
    assert judge("q", "a.py:1") == 0.5
    assert len(prompts) == 2 and "json" in prompts[1].lower()  # recovered via corrective reprompt


# --------------------------------------------------------------------------- #
# HTTP-only judge (agentic-CLI grader dropped for security, the reference window)
# --------------------------------------------------------------------------- #
def test_call_opus_without_endpoint_raises_config_error(monkeypatch):
    # No CODEQA_JUDGE_BASE -> a CONFIG error (opt-in frontier judge), NOT an agentic-CLI
    # call. codeqa never routes grading through claude/codex.
    from apex_router.codeqa import judge
    monkeypatch.delenv("CODEQA_JUDGE_BASE", raising=False)
    try:
        judge._call_opus("grade this")
        assert False, "expected JudgeConfigError"
    except judge.JudgeConfigError:
        pass


def test_config_error_is_not_reprompt_retried(tmp_path, monkeypatch):
    # A JudgeConfigError from the call must be raised immediately (no 3x reprompt loop).
    from apex_router.codeqa import judge
    calls = []
    def boom(prompt):
        calls.append(1)
        raise judge.JudgeConfigError("no endpoint")
    jf = judge.opus_judge_fn(tmp_path, call_fn=boom, max_attempts=3, sleep_fn=lambda s: None)
    try:
        jf("q", "a")
    except judge.JudgeConfigError:
        pass
    assert len(calls) == 1                       # NOT retried 3x


def test_frontier_verifier_without_endpoint_returns_empty(monkeypatch):
    # freshness verifier with no endpoint -> "" (CANNOT-DECIDE), never an agentic CLI call.
    from apex_router.codeqa import freshness
    monkeypatch.delenv("CODEQA_JUDGE_BASE", raising=False)
    assert freshness.frontier_verifier("some claim", "def x(): pass") == ""


# --------------------------------------------------------------------------- #
# HTTP-path hardening (Codex final pass, the reference window)
# --------------------------------------------------------------------------- #
def test_text_block_with_null_value_does_not_crash():
    # BUG (Codex #3): {"content":[{"type":"text","text":null}]} caused TypeError in the
    # "".join(...). A non-str text value must be tolerated, not crash.
    from apex_router.codeqa import judge
    payload = {"content": [{"type": "text", "text": None}, {"type": "text", "text": "ok"}]}
    assert judge._extract_text(payload) == "ok"    # None skipped, str kept


def test_extract_text_non_dict_and_missing():
    from apex_router.codeqa import judge
    assert judge._extract_text(None) == ""
    assert judge._extract_text({"content": "not a list"}) == ""
    assert judge._extract_text({"content": [42, {"type": "text", "text": "x"}]}) == "x"


def test_http_post_bounds_the_read(monkeypatch):
    # BUG (Codex #2): r.read() was unbounded. The helper must read at most a bounded size.
    from apex_router.codeqa import judge
    big = b'{"x":"' + b"a" * (judge._HTTP_MAX_BYTES + 100) + b'"}'
    class R:
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def read(s, n=-1): return big[:n] if n and n > 0 else big
    monkeypatch.setattr("urllib.request.OpenerDirector.open", lambda self, req, **k: R())
    # a body larger than the cap must raise, not be fully buffered/parsed
    try:
        judge._http_post_json("http://x.test", b"{}", {}, timeout=5)
        raised = False
    except judge.JudgeProtocolError:
        raised = True
    assert raised


def test_http_post_strips_auth_on_cross_origin_redirect():
    # BUG (Codex #1): auth headers followed a 302 to another origin. The opener must NOT
    # carry Authorization/APIM key across a redirect to a different host.
    from apex_router.codeqa import judge
    h = judge._NoAuthRedirect()
    # a redirect handler must drop sensitive headers when building the redirected request
    hdrs = {"Authorization": "Bearer secret", "Ocp-Apim-Subscription-Key": "k", "Content-Type": "application/json"}
    cleaned = judge._strip_auth_headers(hdrs)
    assert "Authorization" not in cleaned
    assert "Ocp-Apim-Subscription-Key" not in cleaned
    assert cleaned.get("Content-Type") == "application/json"
