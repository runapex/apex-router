"""Zeno frontier: the math, the live-data report, and the mid-stream error label it exposed."""
from __future__ import annotations

import asyncio
import json
import math

import httpx
import pytest

from apex_router import zeno


# ---- horizon -----------------------------------------------------------------------------------

def test_horizon_compounding_matches_the_worked_numbers():
    assert zeno.horizon_success(0.99, 100) == pytest.approx(0.366, abs=5e-4)
    assert zeno.horizon_success(0.999, 100) == pytest.approx(0.905, abs=5e-4)
    assert zeno.per_step_needed(0.9, 100) == pytest.approx(0.998947, abs=1e-6)
    assert zeno.max_horizon(0.99, 0.9) == 10      # 0.99^10 = 0.904 >= 0.9; 0.99^11 = 0.895 < 0.9
    assert zeno.max_horizon(1.0, 0.9) == math.inf
    assert zeno.max_horizon(0.0, 0.9) == 0


def test_nines():
    assert zeno.nines(0.9) == pytest.approx(1)
    assert zeno.nines(0.999) == pytest.approx(3)
    assert zeno.nines(1.0) == math.inf
    assert str(zeno.nines(0.0)) == "0.0"           # not "-0.0"
    with pytest.raises(ValueError):
        zeno.nines(1.5)


# ---- engineering -------------------------------------------------------------------------------

def test_frontier_flags_the_10x_compute_diminishing_returns_example():
    # 10x compute -> 30% fewer failures, another 10x -> 15% fewer, another 10x -> 7% fewer
    f = zeno.frontier([(1, 1.0), (10, 0.7), (100, 0.595), (1000, 0.595 * 0.93)])
    assert f["verdict"] == "zeno"
    a = [s["alpha"] for s in f["segments"]]
    assert a == pytest.approx([0.7, 0.85, 0.93])
    d = [s["decades_per_nine"] for s in f["segments"]]
    assert d == pytest.approx([6.456, 14.168, 31.729], abs=1e-3)
    assert all(s["elasticity"] < 0 for s in f["segments"])


def test_frontier_other_verdicts():
    # constant power law f = c^-1: one decade of cost per nine everywhere
    assert zeno.frontier([(1, 1), (10, 0.1), (100, 0.01)])["verdict"] == "steady"
    assert zeno.frontier([(1, 1), (10, 0.5), (100, 0.01)])["verdict"] == "accelerating"
    assert zeno.frontier([(1, 0.5), (10, 0.5), (100, 0.1)])["verdict"] == "stalled"
    assert zeno.frontier([(1, 0.5), (10, 0.1)])["verdict"] == "insufficient"
    # a zero observed rate is a bound, not a value: no finite elasticity, not counted
    z = zeno.frontier([(1, 0.5), (10, 0.1), (100, 0.0)])
    assert z["segments"][1]["elasticity"] is None and z["verdict"] == "insufficient"
    with pytest.raises(ValueError):
        zeno.frontier([(0, 0.5), (10, 0.1)])


def test_ladder_gap_converges_cost_diverges():
    rows = zeno.ladder(alpha=0.5, growth=10, gens=4)
    assert [r["gap"] for r in rows] == [1, 0.5, 0.25, 0.125, 0.0625]
    assert [r["step_cost"] for r in rows] == [1, 10, 100, 1000, 10000]
    assert rows[-1]["cum_cost"] == 11111
    assert rows[0]["nines"] == 0.0 and rows[1]["nines"] == pytest.approx(math.log10(2))


def test_steps_to():
    assert zeno.steps_to(1.0, 0.5, 0.0625) == 4
    assert zeno.steps_to(1.0, 0.5, 0.06) == 5
    assert zeno.steps_to(0.01, 0.5, 0.1) == 0
    with pytest.raises(ValueError):
        zeno.steps_to(1.0, 1.0, 0.1)


# ---- compounding -------------------------------------------------------------------------------

def test_compounding_compares_observed_clean_sessions_with_p_to_the_n():
    # pooled p = 1 - 10/1000 = 0.99. Ten 10-call sessions all clean; three 300-call sessions with
    # 10 errors packed into one of them.
    sess = [(10, 0)] * 10 + [(300, 10), (300, 0), (300, 0)]
    c = zeno.compounding(sess)
    assert c["p"] == pytest.approx(1 - 10 / 1000)
    short, long_ = c["buckets"]
    assert short["calls"] == "1-10" and short["observed_clean"] == 1.0
    assert short["predicted_clean"] == pytest.approx(0.99 ** 10)
    assert long_["calls"] == "201+" and long_["observed_clean"] == pytest.approx(2 / 3)
    assert long_["predicted_clean"] == pytest.approx(0.99 ** 300)
    assert long_["ratio"] > 1        # errors clustered in one session: better than independence
    assert zeno.compounding([])["p"] is None


# ---- epistemic ---------------------------------------------------------------------------------

def test_discovery_good_turing_chao1_and_blind_spot():
    labels = ["A", None, "A", "B", "A", None, "C", "B", "D"]
    d = zeno.discovery(labels)
    assert d["failures"] == 9 and d["labeled"] == 7
    assert d["unlabeled_share"] == pytest.approx(2 / 9)
    assert d["kinds"] == 4 and d["singletons"] == 2 and d["doubletons"] == 1   # C, D once; B twice
    assert d["good_turing_p_new"] == pytest.approx(2 / 7)
    assert d["chao1"] == pytest.approx(4 + 4 / 2)
    assert [p["label"] for p in d["curve"]] == ["A", "B", "C", "D"]
    assert d["events_since_new"] == 0
    # no doubletons: bias-corrected Chao1
    assert zeno.discovery(["A", "B", "C", "C", "C"])["chao1"] == pytest.approx(3 + 1)
    empty = zeno.discovery([None, None])
    assert empty["unlabeled_share"] == 1.0 and empty["chao1"] is None


def test_coverage_finds_what_the_aggregate_hides():
    cov = zeno.coverage({"big": (10, 10000), "minority": (12, 200), "rare": (0, 5)},
                        expected=("big", "minority", "rare", "never_sampled"))
    assert cov["aggregate"] == pytest.approx(22 / 10205)
    s = cov["strata"]
    assert s["big"]["status"] == "ok"
    assert s["minority"]["status"] == "worse"      # 6% vs a ~0.2% headline
    assert s["rare"]["status"] == "thin"            # 0/5 still allows ~43%
    assert s["never_sampled"]["status"] == "unmeasured"
    assert cov["masking"] > 20


# ---- live-data report (synthetic files) --------------------------------------------------------

def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_report_reads_telemetry_and_xval(tmp_path):
    tel = tmp_path / "telemetry.jsonl"
    rows = [{"ev": "hb", "ts": 1.0, "requests": 9}]  # heartbeat: skipped
    for i in range(40):
        rows.append({"ts": 100.0 + i, "is_error": i in (5, 30), "session_id": "s1" if i < 20 else "s2",
                     "client": "codex" if i % 2 else "claude-code", "stratum": "l",
                     "schema_version": 9, "error_cause": "ConnectError" if i == 5 else None})
    rows.append({"ts": 200.0, "is_error": None})  # no verdict: skipped
    _write(tel, rows)
    xv = tmp_path / "xval_runs.jsonl"
    _write(xv, [{"arm": "2000/open", "ok": True, "cost": 10.0},
                {"arm": "2000/open", "ok": False, "cost": 30.0},
                {"arm": None, "ok": True, "cost": None}])
    rep = zeno.report(tel, xv, min_arm_runs=20)
    assert rep["source"]["requests"] == 40
    assert rep["reliability"]["errors"] == 2 and rep["reliability"]["p"] == pytest.approx(0.95)
    assert rep["compounding"]["sessions"] == 2
    assert rep["discovery"]["unlabeled_share"] == 0.5
    assert rep["engineering"]["arms"] == [
        {"arm": "2000/open", "runs": 2, "failed": 1, "costed": 2, "mean_cost": 20.0, "fail_rate": 0.5,
         "fail_upper": pytest.approx(0.9055, abs=1e-3), "evidence": "thin"}]
    assert rep["engineering"]["verdict"] == "insufficient"
    assert set(rep["coverage_stratum"]["strata"]) == {"l", "xs", "s", "m"}
    assert rep["coverage_stratum"]["strata"]["xs"]["status"] == "unmeasured"
    text = zeno.render(rep)
    assert "NOT answer correctness" in text and "50% carry no cause" in text


def test_report_on_missing_files_is_empty_not_an_error(tmp_path):
    rep = zeno.report(tmp_path / "nope.jsonl", tmp_path / "nope2.jsonl")
    assert rep["reliability"] is None
    assert "nothing to measure" in zeno.render(rep)


def test_cli_dispatch(tmp_path, capsys):
    from apex_router.cli import main
    assert main(["zeno", "frontier", "1:1", "10:0.7", "100:0.595", "1000:0.55335"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "zeno"
    assert main(["zeno", "frontier", "1:2:3"]) == 2
    assert main(["zeno", "horizon", "--p", "0.999", "--steps", "100"]) == 0
    assert json.loads(capsys.readouterr().out)["table"][0]["p_success"] == pytest.approx(0.905, abs=5e-4)
    assert main(["zeno", "report", "--telemetry", str(tmp_path / "x"),
                 "--xval-runs", str(tmp_path / "y")]) == 0


# ---- cross-validation pass 1 findings (each pinned) ----------------------------------------------

def test_xval_failed_run_without_cost_still_counts(tmp_path):
    xv = tmp_path / "x.jsonl"
    _write(xv, [{"arm": "a", "ok": True, "cost": 10.0, "ts": 5}] * 3
           + [{"arm": "a", "ok": False, "cost": None, "ts": 5}] * 2)
    arm = zeno._xval_frontier(xv, min_n=1)["arms"][0]
    assert (arm["runs"], arm["failed"], arm["costed"], arm["mean_cost"]) == (5, 2, 3, 10.0)


def test_xval_respects_since(tmp_path):
    xv = tmp_path / "x.jsonl"
    _write(xv, [{"arm": "a", "ok": True, "cost": 1.0, "ts": 10},
                {"arm": "a", "ok": False, "cost": 1.0, "ts": 100}])
    assert zeno._xval_frontier(xv, 1, since_ts=50)["arms"][0]["runs"] == 1


def test_max_horizon_exact_boundary():
    assert zeno.max_horizon(0.99, 0.990000000001) == 0     # one step is only 0.99
    assert zeno.max_horizon(0.99, 0.99) == 1
    assert zeno.max_horizon(0.5, 0.25) == 2
    for p, t in ((0.9, 0.5), (0.999, 0.9), (0.97, 0.36)):
        n = zeno.max_horizon(p, t)
        assert p ** n >= t > p ** (n + 1)
    # an underflow plateau (p^n == target for ~1e12 consecutive n) must return, not spin
    assert zeno.max_horizon(0.9999999999999999, 5e-324) > 0


def test_frontier_ten_percent_band_is_symmetric():
    # decades per nine 1.0 -> 0.905 is within 10%: steady, not accelerating
    f = 10 ** -(1 / 0.905)
    v = zeno.frontier([(1, 1.0), (10, 0.1), (100, 0.1 * f)])
    assert [round(s["decades_per_nine"], 3) for s in v["segments"]] == [1.0, 0.905]
    assert v["verdict"] == "steady"


def test_render_shows_xval_when_proxy_telemetry_is_missing(tmp_path):
    xv = tmp_path / "x.jsonl"
    _write(xv, [{"arm": "2000/open", "ok": True, "cost": 7.0}])
    text = zeno.render(zeno.report(tmp_path / "none.jsonl", xv))
    assert "nothing to measure" in text and "2000/open" in text


def test_render_names_the_compounding_population(tmp_path):
    tel = tmp_path / "t.jsonl"
    _write(tel, [{"ts": 1.0 + i, "is_error": True} for i in range(9)]           # no session id
           + [{"ts": 20.0 + i, "is_error": False, "session_id": "s"} for i in range(10)])
    rep = zeno.report(tel, tmp_path / "none.jsonl")
    assert rep["reliability"]["p"] == pytest.approx(10 / 19)
    assert rep["compounding"]["p"] == 1.0
    assert "rows with a session id only" in zeno.render(rep)


# ---- Markov chain beside p^n (independent signal) -----------------------------------------------

def _bursty_telemetry(path, sessions=12, calls=40):
    """Every third session has one 3-call failure burst mid-way; the rest are clean."""
    rows, ts = [], 1.0
    for s in range(sessions):
        for i in range(calls):
            rows.append({"ts": ts, "session_id": f"s{s}",
                         "is_error": s % 3 == 0 and 20 <= i < 23})
            ts += 1.0
    _write(path, rows)


def test_report_adds_markov_beside_compounding(tmp_path):
    tel = tmp_path / "t.jsonl"
    _bursty_telemetry(tel)
    rep = zeno.report(tel, tmp_path / "none.jsonl")
    m = rep["markov"]
    assert m["sessions"] == 12 and m["pairs"] == 12 * 39
    # 4 bursts: ok->fail 4, fail->fail 8, fail->ok 4; with the 0.5 prior
    assert m["counts"]["fail_fail"] == 8 and m["counts"]["ok_fail"] == 4
    assert m["p_fail_fail"] == pytest.approx(8.5 / 13)
    assert m["mean_fail_run"] == pytest.approx(1 / (1 - 8.5 / 13))
    assert m["verdict"] == "independence violated" and m["r1"] > 0.1
    assert [r["steps"] for r in m["table"]] == [r["steps"] for r in rep["reliability"]["table"]]
    b = m["buckets"][0]
    assert b["calls"] == "11-50" and b["observed_clean"] == pytest.approx(8 / 12)
    assert b["iid_clean"] == pytest.approx(rep["compounding"]["buckets"][0]["predicted_clean"], rel=0.05)
    assert b["markov_clean"] > b["iid_clean"]
    assert (m["holdout"]["n_train"], m["holdout"]["n_test"]) == (8, 4)
    # zeno's own numbers are untouched by the new key
    assert rep["compounding"]["p"] == pytest.approx(1 - 12 / 480)


def test_markov_verdict_thresholds():
    few = zeno.markov_horizon([[0, 1, 1, 0]] * 10)                     # 30 pairs
    assert few["verdict"] == "insufficient"
    alternating = zeno.markov_horizon([[0, 1] * 50] * 5)                # r1 = -1, 495 pairs
    assert alternating["verdict"] == "independence holds"
    assert zeno.markov_horizon([])["verdict"] == "insufficient"
    assert zeno.markov_horizon([[0] * 300])["r1"] is None               # no failures: r1 undefined


def test_render_shows_markov_section(tmp_path):
    tel = tmp_path / "t.jsonl"
    _bursty_telemetry(tel)
    rep = zeno.report(tel, tmp_path / "none.jsonl")
    text = zeno.render(rep)
    assert "1b. horizon — Markov (bursty failures; independent of p^n)" in text
    assert "P(fail|fail)" in text and "mean failure run" in text
    assert "observed" in text and "markov" in text and "held out" in text
    assert "heterogeneity is not modeled" in text
    # the p^n section is still there, ahead of the new one
    assert text.index("1. horizon — per-call completion compounds") < text.index("1b. horizon")
    assert "markov" in json.loads(json.dumps(rep, default=str))


def test_markov_sequences_follow_ts_not_file_order(tmp_path):
    # Rows of one session written out of time order: the chain must see ok,fail,fail,ok.
    tel = tmp_path / "t.jsonl"
    order = [(4.0, False), (2.0, True), (1.0, False), (3.0, True)]
    _write(tel, [{"ts": ts, "session_id": "s", "is_error": e} for ts, e in order])
    c = zeno.report(tel, tmp_path / "none.jsonl")["markov"]["counts"]
    assert (c["ok_fail"], c["fail_fail"], c["fail_ok"], c["ok_ok"]) == (1, 1, 1, 0)


def test_render_holdout_needs_enough_test_sessions(tmp_path):
    tel = tmp_path / "t.jsonl"
    _bursty_telemetry(tel)                                   # 4 test sessions
    assert "too few sessions to compare" in zeno.render(zeno.report(tel, tmp_path / "none.jsonl"))
    _bursty_telemetry(tel, sessions=72)                      # 22 test sessions
    assert "clean/not log-lik" in zeno.render(zeno.report(tel, tmp_path / "none.jsonl"))


def test_render_markov_without_session_ids(tmp_path):
    tel = tmp_path / "t.jsonl"
    _write(tel, [{"ts": 1.0 + i, "is_error": i == 3} for i in range(9)])
    text = zeno.render(zeno.report(tel, tmp_path / "none.jsonl"))
    assert "nothing to chain" in text


# ---- the label the report exposed: a stream that breaks mid-way gets a cause --------------------

def _drive(handler_name, status, *, break_stream):
    from apex_router.proxy_engine.proxy.handlers import passthrough
    from apex_router.proxy_engine.proxy.handlers import shadow as shadow_h

    class _Resp:
        status_code = status
        headers = httpx.Headers({"content-type": "text/event-stream"})

        async def aiter_raw(self):
            yield b'data: {"message":{"usage":{"input_tokens":5}}}\n\n'
            if break_stream:
                raise httpx.ReadError("peer closed")

        async def aclose(self):
            pass

    class _Up:
        def build_url(self, k, p, q):
            return "http://up" + p

        def endpoint_id(self, client_kind):
            return "anthropic"

        async def inject_auth(self, headers, client_kind, *, raw_headers=None):
            return headers

        async def send_stream(self, m, u, *, headers, content, stats=None):
            return _Resp()

    class _Tel:
        ev: list = []

        def emit(self, e):
            self.ev.append(e)

    class _URL:
        path = "/v1/messages"

    class _Req:
        method = "POST"
        url = _URL()
        headers = {"x-request-id": "r"}
        scope = {"raw_path": b"/v1/messages", "query_string": b"",
                 "headers": [(b"content-type", b"application/json")]}

        async def body(self):
            return b'{"model":"m","messages":[{"role":"user","content":"hi"}]}'

    async def run():
        tel = _Tel()
        tel.ev = []
        if handler_name == "passthrough":
            resp = await passthrough.handle(_Req(), _Up(), tel)
        else:
            resp = await shadow_h.handle(_Req(), _Up(), tel, None)
        try:
            async for _ in resp.body_iterator:
                pass
        except httpx.ReadError:
            pass
        return tel.ev[0]

    return asyncio.run(run())


@pytest.mark.parametrize("handler", ["passthrough", "shadow"])
def test_midstream_break_is_labeled(handler):
    ev = _drive(handler, 200, break_stream=True)
    assert ev.is_error is True
    assert ev.error_cause == "midstream_ReadError"


@pytest.mark.parametrize("handler", ["passthrough", "shadow"])
def test_http_status_label_wins_over_midstream(handler):
    ev = _drive(handler, 429, break_stream=True)
    assert ev.error_cause == "http_429"          # rate-limit label preserved for pressure


@pytest.mark.parametrize("handler", ["passthrough", "shadow"])
def test_clean_stream_has_no_cause(handler):
    ev = _drive(handler, 200, break_stream=False)
    assert ev.is_error is False and ev.error_cause is None


def test_pressure_ignores_midstream_like_before():
    from apex_router.pressure import _classify
    assert _classify({"error_cause": "midstream_ReadError"}) is None
    assert _classify({"error_cause": None}) is None            # what the same row was before
    assert _classify({"error_cause": "ReadError"}) == "transport"
    assert _classify({"error_cause": "http_429"}) == "rate_limited"
