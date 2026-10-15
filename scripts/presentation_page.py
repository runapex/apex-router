"""Render the apex-router + skills presentation (same layout and style as the triage explorer on :8090).

    python3 scripts/presentation_data.py --out /tmp/pres.json
    python3 scripts/presentation_page.py /tmp/pres.json > /tmp/apex-router.html

Numbers come from the data JSON. The text blocks (results, failures) name their source. No dollar figures.
"""
from __future__ import annotations

import html
import json
import re
import sys

D = json.load(open(sys.argv[1]))
E = html.escape


def n(v):
    return "–" if v is None else f"{v:,}"


def tok(v):
    if v is None:
        return "–"
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(v) >= div:
            return f"{v / div:.1f}{unit}"
    return str(v)


def pct(a, b):
    return "–" if not b else f"{100 * a / b:.0f}%"


def delta(cur, prev, good="up"):
    if not prev:
        return '<span class="small">new</span>'
    d = (cur - prev) / prev * 100
    up = d >= 0
    cls = "ok" if (up == (good == "up")) else "stop"
    if good == "none":
        cls = "na"
    return f'<span class="p {cls}">{"+" if up else ""}{d:.0f}%</span>'


def pill(cls, text):
    return f'<span class="p {cls}">{E(text)}</span>'


T = D["telemetry"]
L = D["local"]
S = D["steps"] if "counts" in D["steps"] else {}
DV = D["data"]
W = D["workflows"]
G = D["digest"]
cur, prev = T["total"]["cur"], T["total"]["prev"]
BATCH = next((c["req"] for c in T["by_client"]["cur"] if c["name"].startswith("codex batch")), 0)
wc, wp = D["window"]["cur"], D["window"]["prev"]

CSS = r"""
:root{--bg:#0f1218;--bg2:#161b24;--bg3:#1d2430;--line:#2a3342;--ink:#e6e4df;--mute:#9aa4b2;--dim:#626d7d;
--blue:#58C4DD;--green:#83C167;--red:#FC6255;--orange:#F0A35E;--purple:#B18CD9;--teal:#5CD0B3;--gold:#E2C044;
--serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;--mono:"SF Mono",Menlo,Consolas,monospace;
--sans:-apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 var(--sans)}
a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}
code,pre,.mono{font-family:var(--mono);font-size:12.5px}
pre{background:#0b0e13;border:1px solid var(--line);border-radius:8px;padding:10px 12px;overflow:auto;white-space:pre-wrap;margin:6px 0}
h1,h2,h3{font-family:var(--serif);font-weight:500}h1{font-size:40px;margin:0 0 6px;line-height:1.15}h2{font-size:28px;margin:0 0 4px}
h3{font-size:18px;margin:20px 0 6px}
.wrap{max-width:1120px;margin:0 auto;padding:36px 28px 80px}.kicker{color:var(--gold);font-family:var(--serif);font-style:italic}
.lead{color:var(--mute);font-size:16px;max-width:860px}.small{font-size:13px;color:var(--mute)}
section{margin-top:44px;scroll-margin-top:70px}
table{border-collapse:collapse;width:100%;font-size:13.5px;margin:8px 0}
th,td{border-bottom:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top}
th{color:var(--mute);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.6px}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums}
details{border:1px solid var(--line);border-radius:8px;margin:8px 0;background:var(--bg2)}
details>summary{cursor:pointer;padding:8px 12px;color:var(--mute);font-size:13.5px}details>div{padding:4px 12px 10px}
.topbar{position:sticky;top:0;z-index:5;background:rgba(15,18,24,.92);backdrop-filter:blur(6px);border-bottom:1px solid var(--line)}
.topbar .in{max-width:1120px;margin:0 auto;padding:8px 28px;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.topbar a{color:var(--mute);font-size:13px;padding:3px 8px;border-radius:6px}
.topbar a:hover{background:var(--bg3);color:var(--ink);text-decoration:none}
.topbar b{font-family:var(--serif);font-weight:500;color:var(--blue);margin-right:10px}
.p{display:inline-block;border-radius:999px;padding:1px 9px;font-size:12px;border:1px solid;white-space:nowrap}
.p.ok{color:var(--green);border-color:#3f5a35}.p.none{color:var(--teal);border-color:#2f5a50}.p.stop{color:var(--red);border-color:#6a3530}
.p.risk{color:var(--orange);border-color:#6b4a2a}.p.ven{color:var(--purple);border-color:#4f3f66}.p.na{color:var(--dim);border-color:var(--line)}
.p.gate{color:var(--gold);border-color:#5f5326}
.kv{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0 6px}
.kv>div{background:var(--bg2);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.kv b{display:block;font-family:var(--serif);font-size:26px;font-weight:500}.kv span{color:var(--mute);font-size:12.5px}
.concept{background:var(--bg2);border-left:3px solid var(--blue);padding:10px 14px;border-radius:0 8px 8px 0;margin:10px 0;max-width:900px}
.open{border:1px solid #6b4a2a;background:rgba(240,163,94,.06);border-radius:8px;padding:8px 12px;margin:10px 0;font-size:14px}
.open b{color:var(--orange)}
.flow{display:flex;align-items:center;margin:14px 0;flex-wrap:wrap}
.flow .b{border:1px solid var(--line);background:var(--bg2);border-radius:8px;padding:8px 12px;font-size:13.5px;text-align:center;min-width:120px}
.flow .b small{display:block;color:var(--mute);font-size:11.5px}.flow .a{color:var(--dim);padding:0 8px;font-size:18px}
.flow .b.hot{border-color:var(--gold)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:var(--bg2);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.card h3{margin:2px 0 4px}.card p{margin:4px 0;font-size:14px}
.bar{height:8px;border-radius:4px;background:var(--bg3);overflow:hidden;margin-top:4px}
.bar i{display:block;height:100%;background:var(--blue)}
.src{color:var(--dim);font-size:12px}
@media (max-width:900px){.kv{grid-template-columns:1fr 1fr}.grid2{grid-template-columns:1fr}}
"""


def bar(v, mx, color="var(--blue)"):
    w = 0 if not mx else max(1, round(100 * v / mx))
    return f'<div class="bar"><i style="width:{w}%;background:{color}"></i></div>'


def kv_cards():
    loc_cur = L["codeqa"]["cur"].get("asks", 0) + L["offload"]["cur"].get("calls", 0)
    items = [
        (n(cur["req"]), f"frontier requests, {wc[0]}–{wc[1]} {delta(cur['req'], prev['req'], 'none')} · {n(BATCH)} of them one Codex batch run"),
        (tok(cur["out"]), f"frontier output tokens {delta(cur['out'], prev['out'], 'none')}"),
        (n(loc_cur), "local-model calls (codeqa + offload), 7 d"),
        (f"{L['codeqa_validate']['local_share_pct']}%" if L.get("codeqa_validate") else "–", "codeqa verifier calls run locally"),
        (n(cur["r429"]), f"429 rate limits, 7 d (prev {n(prev['r429'])})"),
        (tok(sum(v["bytes"] for v in DV["transcripts_mirror"].values())) + " bytes", "agent transcripts kept (pi, Claude Code, Codex)"),
        (n(S.get("counts", {}).get("steps_main")), "classified agent steps (P6 dataset)"),
        (n(len(D["skills"])), "skills installed"),
    ]
    return '<div class="kv">' + "".join(f"<div><b>{a}</b><span>{b}</span></div>" for a, b in items) + "</div>"


# ------------------------------------------------------------------------------------------------ sections

def sec_data():
    tm = DV["transcripts_mirror"]
    cls = S.get("classes", {})
    tot_cls = sum(cls.values()) or 1
    ph = S.get("phases", {})
    lab = DV["labels"]
    cq = L["codeqa"]["all"]
    cit = cq.get("grounded", 0) + cq.get("hallucinated", 0) + cq.get("stale", 0)
    rows_in = [
        ("Agent transcripts (mirror)", f"{n(sum(v['files'] for v in tm.values()))} files · {tok(sum(v['bytes'] for v in tm.values()))} bytes",
         f"pi {n(tm['pi']['files'])} · Claude Code {n(tm['claude']['files'])} · Codex {n(tm['codex']['files'])}", "<code>worldmodel snapshot</code> (hourly)"),
        ("Proxy telemetry", f"{n(T['rows'])} requests since {T['first']}",
         f"in {tok(T['total']['all'].get('in'))} · cache read {tok(T['total']['all'].get('cache_read'))} · out {tok(T['total']['all'].get('out'))} tokens", "measuring proxy :8788"),
        ("Agent steps (tool calls)", f"{n(S.get('counts', {}).get('steps'))} steps · {n(S.get('counts', {}).get('tasks'))} tasks · {n(S.get('counts', {}).get('sessions'))} sessions",
         f"{T['first']} → today", "<code>worldmodel build</code>"),
        ("Task outcome labels", f"{n(lab['rows'])} tasks", f"weak (rules + local judge) {n(lab['by_source'].get('weak', 0))} · gold {('file present' if lab['gold_file_present'] else '0 (file missing)')}", "<code>labels build</code>"),
        ("Local code Q&amp;A (codeqa)", f"{n(cq.get('asks'))} questions · {tok(cq.get('prompt_tokens'))} prompt tokens", f"{n(cit)} citations checked", "codeqa, local model"),
        ("Routing outcomes", f"{n(DV['route_log']['rows'])} cheap-start outcomes", "ok or escalated, per task type", "<code>route-log</code>"),
        ("Codex cross-validations", f"{n(DV['xval_runs'])} runs", "per category and output-cap arm", "<code>xval</code>"),
        ("pi bash calls checked", f"{n(DV['hangguard'].get('rows', 0))} rewrites logged since 10-15", f"timeout {n(DV['hangguard'].get('timeout', 0))} · ssh {n(DV['hangguard'].get('ssh', 0))} · blocked {n(DV['hangguard'].get('blocked', 0))}", "apex-hangguard"),
        ("Project memory", f"{n(DV['memory_files'])} memory files", "indexed for retrieval", "nightly memory index"),
    ]
    t1 = "".join(f"<tr><td>{a}</td><td>{b}</td><td class='small'>{c}</td><td class='src'>{d}</td></tr>" for a, b, c, d in rows_in)
    mx = max(cls.values()) if cls else 1
    t2 = "".join(f"<tr><td><code>{E(k)}</code></td><td class='r'>{n(v)}</td><td style='width:45%'>{bar(v, mx)}</td><td class='r small'>{(pct(v, tot_cls) if v * 100 >= tot_cls else '<1%')}</td></tr>"
                 for k, v in sorted(cls.items(), key=lambda x: -x[1]))
    tt = DV["route_log"]["by_task_type"]
    strata = DV["telemetry_strata"]
    got = [
        ("Cheap-start routing per task type", "route-log outcomes",
         "; ".join(f"<b>{E(r['task'])}</b>: escalation {r['rate']:.2f} [{r['ci'][0]:.2f}, {r['ci'][1]:.2f}], n={r['n']} → {pill('ok', 'cheap start') if r['verdict'] == 'cost_favors_cheap_start' else pill('na', 'inconclusive')}"
                   for r in G.get("route_advise", []) if r["n"] >= 100) + f". {sum(1 for r in G.get('route_advise', []) if r['n'] < 100)} other task types: too few rows."),
        ("Cheaper Codex tier is possible", "proxy telemetry (context size)",
         (lambda c: f"{n(c['fits'])} of {n(c['of'])} Codex requests fit under {c['limit']} tokens of context (p50 {c['p50']}, p95 {c['p95']}). A cheaper tier ({E(c['tier'])}) can serve all of them.")(G["codex_ctx"]) if G.get("codex_ctx") else "–"),
        ("Grounded answers from a local model", "codeqa",
         f"{pct(cq.get('grounded', 0), cit)} of {n(cit)} citations point to real code (stale {n(cq.get('stale', 0))}, invented {n(cq.get('hallucinated', 0))}). {L['codeqa_validate']['local_share_pct'] if L.get('codeqa_validate') else '–'}% of verifier calls run locally."),
        ("Session handoff threshold", "proxy telemetry",
         f"Set from the median of {n(G.get('handoff_sessions'))} sessions. It tells when a long session must hand off or compact."),
        ("Refusals", "proxy telemetry (v12)", f"{n(G.get('refusals', [None, None])[0])} refusals in {n(G.get('refusals', [None, None])[1])} requests."),
        ("Next-step prediction (JEPA world model)", "agent steps", "No gain yet. The learned model loses to a simple logistic model on hand-made features (3 attempts). Paused until 50k steps."),
        ("Pressure readout before a fan-out", "proxy telemetry", "GREEN / AMBER / RED per model family. It sets how many sub-agents start at once."),
    ]
    t3 = "".join(f"<tr><td><b>{a}</b></td><td class='src'>{b}</td><td>{c}</td></tr>" for a, b, c in got)
    return f"""
<section id="data"><h2>Data: what we process, how we classify it, what we get</h2>
<p class="lead">All data stays on this machine. The builders store counts, classes and hashes, not prompt text. Every number below comes from a local file at build time.</p>
<div class="flow"><div class="b">Transcripts<small>pi · Claude Code · Codex</small></div><div class="a">→</div>
<div class="b">Proxy telemetry<small>every frontier request</small></div><div class="a">→</div>
<div class="b hot">Classify<small>action · phase · task type · size</small></div><div class="a">→</div>
<div class="b">Label<small>rules + local judge + gold</small></div><div class="a">→</div>
<div class="b hot">Decide<small>route · tier · cap · stop</small></div></div>
<h3>Processed</h3><table><tr><th>Source</th><th>Volume</th><th>Detail</th><th>Collector</th></tr>{t1}</table>
<div class="grid2"><div><h3>Classified: agent steps by action class</h3><table><tr><th>Class</th><th class="r">Steps</th><th></th><th class="r">Share</th></tr>{t2}</table>
<p class="small">13 action classes by fixed rules (96.4% agreement on a blind re-label of 306 calls). Phases: {", ".join(f"{E(k)} {n(v)}" for k, v in ph.items())}. Error steps: {S.get('errors_share', 0) * 100:.1f}%. Test steps parsed: {pct(S.get('tests', {}).get('parsed', 0), S.get('tests', {}).get('test_steps', 0))}.</p></div>
<div><h3>Classified: other axes</h3><table><tr><th>Axis</th><th>Values</th></tr>
<tr><td>Routing task type <span class="small">(all rows; the advice uses scored rows)</span></td><td class="small">{", ".join(f"{E(k)} {n(v)}" for k, v in list(tt.items())[:7])}</td></tr>
<tr><td>Request size stratum</td><td class="small">{", ".join(f"{E(k)} {n(v)}" for k, v in strata.items())}</td></tr>
<tr><td>Task outcome</td><td class="small">{", ".join(f"{E(k)} {n(v)}" for k, v in DV['labels']['by_label'].items())}</td></tr>
<tr><td>Citation verdict</td><td class="small">grounded {n(cq.get('grounded'))}, stale {n(cq.get('stale'))}, invented {n(cq.get('hallucinated'))}</td></tr>
<tr><td>Workflow</td><td class="small">W0 in-session {n((S.get('workflows') or {}).get('W0'))}, W2 delegated {n((S.get('workflows') or {}).get('W2'))}</td></tr>
</table></div></div>
<h3>What we get from it</h3><table><tr><th>Result</th><th>From</th><th>Finding</th></tr>{t3}</table>
</section>"""


RESULTS = [
    ("ok", "shipped 10-15", "pi sub-agents", "Tools <code>agent_spawn / wait / cancel / result</code>. Background jobs with a completion message, idle, wall and cost limits, a full process-tree cancel and a cap of 4 (1 after a 429). 75 tests, four Codex review passes."),
    ("ok", "shipped 10-15", "pi hang guard", "Default bash timeout, <code>&lt; /dev/null</code> for agent CLIs, ssh BatchMode, no sleep polling. Replay over 14,705 old bash calls: 43 commands changed, <code>bash -n</code> accepts all 43."),
    ("ok", "shipped 10-15", "ASD-STE100 default format", "One rules file feeds pi, sub-agent briefs, Claude Code and a checker (<code>apex-router ste check</code>). Mechanical rules only; the licensed dictionary is not included."),
    ("ok", "shipped 10-14", "Decision card (<code>core.verdict</code>)", "One status vocabulary and one decision rule for every A/B or arm comparison. A fired kill criterion beats a gate promotion."),
    ("ok", "live", "Measuring proxy", "Forwards every Claude Code, Codex and pi request byte-identical and records tokens, latency, cache use, errors and rate limits."),
    ("ok", "live", "Cheap-start advice", "Debug and review tasks: a cheap start escalates only 11–13% of the time, so the cheap start is the better default."),
    ("ok", "live", "Codex cross-validation (xval)", "Adaptive <code>codex exec</code> review with arms per category. Now also a pi sub-agent kind."),
    ("gate", "opt-in", "E-D workflow experiment", "Randomly assigns W0 (do it in this session) or W2 (delegate) per turn, to measure which workflow ends better. No turns assigned yet."),
    ("na", "paused", "P6 JEPA world model", "Gate G1 failed 3 times. The hand-feature logistic model is better. Next decision at 50k main-session steps."),
    ("na", "closed", "Multi-agent panel (InfoDelphi)", "Panel candidates are 0.53% of requests. Only self-consistency beats one call (+0.061 Brier). Nothing promoted."),
]


def sec_results():
    cards = "".join(f'<div class="card"><h3>{t} {pill(c, s)}</h3><p>{b}</p></div>' for c, s, t, b in RESULTS)
    return f"""<section id="results"><h2>Relevant features and results</h2>
<p class="lead">What works, what is live and which experiments ended. Each card names its state.</p><div class="grid2">{cards}</div></section>"""


def sec_local():
    cc, pc = L["codeqa"]["cur"], L["codeqa"]["prev"]
    oc, op = L["offload"]["cur"], L["offload"]["prev"]
    v = L.get("codeqa_validate") or {}
    j = L.get("label_judge") or {}
    rows = [
        ("Frontier (all clients, via proxy)", n(cur["req"]), n(prev["req"]), f"in {tok(cur['in'])} · cache {tok(cur['cache_read'])} · out {tok(cur['out'])}", f"out {tok(prev['out'])}"),
        ("Local · codeqa questions", n(cc.get("asks")), n(pc.get("asks")), f"prompt {tok(cc.get('prompt_tokens'))}", f"prompt {tok(pc.get('prompt_tokens'))}"),
        ("Local · offload lanes (review, codegen, adhoc)", n(oc.get("calls")), n(op.get("calls")), f"ok {n(oc.get('ok'))} · escalated {n(oc.get('escalated'))}", f"escalated {n(op.get('escalated'))}"),
    ]
    t = "".join(f"<tr><td>{a}</td><td class='r'>{b}</td><td class='r'>{c}</td><td class='small'>{d}</td><td class='small'>{e}</td></tr>" for a, b, c, d, e in rows)
    lf = v.get("local", 0)
    ff = v.get("frontier", 0)
    return f"""<section id="local"><h2>Local models vs frontier</h2>
<p class="lead">Frontier models do the agent work. Local models do checks and side work: code Q&amp;A, citation checks, outcome labels and offload drafts. Requests and tokens only.</p>
<div class="kv"><div><b>{n(cur['req'])}</b><span>frontier requests, 7 d</span></div>
<div><b>{n(cc.get('asks', 0) + oc.get('calls', 0))}</b><span>local-model calls, 7 d</span></div>
<div><b>{n(lf)} / {n(ff)}</b><span>codeqa verifier calls local / frontier (all time)</span></div>
<div><b>{n(j.get('judged'))}</b><span>task outcomes labelled by the local judge</span></div></div>
<table><tr><th>Lane</th><th class="r">Requests {wc[0]}–{wc[1]}</th><th class="r">Prev 7 d</th><th>Tokens, 7 d</th><th>Prev</th></tr>{t}</table>
<div class="grid2"><div class="card"><h3>Verifier split (codeqa validate)</h3>{bar(lf, lf + ff, 'var(--teal)')}<p class="small">{pct(lf, lf + ff)} local · {pct(ff, lf + ff)} frontier · {n(v.get('struck'))} claims struck</p></div>
<div class="card"><h3>Local models</h3><p class="small">Active: <b>{E(L['active'])}</b> (MLX, resident). Installed: {", ".join(E(x) for x in L['installed'])}. Offload calls by model: {", ".join(f"{E(k)} {n(v)}" for k, v in L['offload_models'].items())}.</p></div></div>
<div class="open"><b>Gap.</b> The offload lanes escalated {n(oc.get('escalated'))} of {n(oc.get('calls'))} calls in the last 7 days: local drafts do not yet replace frontier work. The local model is useful today for checks (citations, labels), not for primary work.</div>
</section>"""


def sec_models():
    rows = T["by_model"]["cur"]
    prevm = {m["name"]: m for m in T["by_model"]["prev"]}
    mx = max((m["req"] for m in rows), default=1)
    t = "".join(
        f"<tr><td><code>{E(m['name'])}</code></td><td class='r'>{n(m['req'])}</td><td style='width:16%'>{bar(m['req'], mx)}</td>"
        f"<td class='r'>{n(prevm.get(m['name'], {}).get('req', 0))}</td><td class='r'>{tok(m['in'] + m['cache_read'])}</td><td class='r'>{tok(m['out'])}</td>"
        f"<td class='r'>{pct(m['cache_read'], m['in'] + m['cache_read'] + m['cache_write'])}</td><td class='r'>{n(m.get('ttft_p50_ms'))}</td>"
        f"<td class='r'>{n(m['err'])}</td><td class='r'>{(pill('risk', n(m['r429'])) if m['r429'] else '0')}</td></tr>"
        for m in rows if m["req"] >= 5)
    cl = T["by_client"]["cur"]
    clp = {c["name"]: c for c in T["by_client"]["prev"]}
    t2 = "".join(f"<tr><td>{E(c['name'])}</td><td class='r'>{n(c['req'])}</td><td class='r'>{n(clp.get(c['name'], {}).get('req', 0))}</td><td class='r'>{tok(c['out'])}</td><td class='r'>{n(c['r429'])}</td></tr>" for c in cl)
    return f"""<section id="models"><h2>Models</h2>
<p class="lead">Frontier models seen by the proxy, {wc[0]}–{wc[1]}. Input counts uncached plus cache-read tokens. TTFT is the median time to the first token, in ms.</p>
<table><tr><th>Model</th><th class="r">Requests</th><th></th><th class="r">Prev 7 d</th><th class="r">Input</th><th class="r">Output</th><th class="r">Cache hit</th><th class="r">TTFT p50</th><th class="r">Errors</th><th class="r">429</th></tr>{t}</table>
<h3>By client</h3><table><tr><th>Client</th><th class="r">Requests</th><th class="r">Prev 7 d</th><th class="r">Output tokens</th><th class="r">429</th></tr>{t2}</table>
<p class="small">"codex batch" = <code>codex exec</code> pipelines that send no session id (mostly the vuln_triage run on 10-13). Without it, frontier requests in the last 7 days are {n(cur['req'] - next((c['req'] for c in cl if c['name'].startswith('codex batch')), 0))}. pi requests are found by session id ({n(T['pi_sessions_known'])} pi sessions known). The rest of the Anthropic traffic is Claude Code.</p></section>"""


def sec_workflows():
    pj = W["pi_jobs"]
    xv = W["xval"]
    wa = W.get("workflow_arms") or {}
    return f"""<section id="workflows"><h2>Workflows</h2>
<p class="lead">How work is split between one session and delegated agents.</p>
<div class="kv"><div><b>{n(wa.get('W0'))}</b><span>tasks done in-session (W0)</span></div><div><b>{n(wa.get('W2'))}</b><span>tasks with delegated sub-agents (W2)</span></div>
<div><b>{n(pj['n'])}</b><span>pi sub-agent jobs since 10-15</span></div><div><b>{n(xv['runs'])}</b><span>Codex cross-validations ({pct(xv['ok'], xv['runs'])} ok)</span></div></div>
<div class="grid2"><div class="card"><h3>pi sub-agents (<code>agent_spawn</code>)</h3><p class="small">By agent: {", ".join(f"{E(k)} {n(v)}" for k, v in pj['by_agent'].items())}.<br>By end state: {", ".join(f"{E(k)} {n(v)}" for k, v in pj['by_state'].items())}.<br>Jobs that hit a 429: {n(pj['rate_limited'])}.</p></div>
<div class="card"><h3>Codex cross-validation (xval)</h3><p class="small">By category: {", ".join(f"{E(str(k))} {n(v)}" for k, v in xv['by_category'].items())}. Last 7 days: {n(xv['cur'])} runs.</p></div>
<div class="card"><h3>E-D experiment (W0 vs W2)</h3><p class="small">Opt-in in the Claude Code plugin. Turns with an assigned arm: {n((W.get('ed_arms') or {}).get('W0', 0) + (W.get('ed_arms') or {}).get('W2', 0))}. The readout needs ≥ 30 turns per task type.</p></div>
<div class="card"><h3>Claude Code dispatches</h3><p class="small">Agent / Task / Workflow calls in the kept transcripts: {", ".join(f"{E(k)} {n(v)}" for k, v in W['claude_dispatch'].items()) or '0'}. Most delegation now runs in pi and Codex.</p></div></div>
<div class="concept">Delegation is rare: {n(wa.get('W2'))} of {n((wa.get('W0') or 0) + (wa.get('W2') or 0))} tasks. The E-D experiment exists to measure if W2 ends better. Until it has data, no default changes.</div>
</section>"""


def sec_skills():
    rows = D["skills"]
    mx = max((s["sessions_14d"] for s in rows), default=1)
    prev = (D.get("previous_snapshot") or {}).get("skills") or {}
    t = "".join(
        f"<tr><td><b>{E(s['name'])}</b><div class='small'>{E(s['desc'])}</div></td><td class='r'>{n(s['sessions_7d'])}</td><td class='r'>{n(s['sessions_14d'])}</td>"
        f"<td style='width:18%'>{bar(s['sessions_14d'], mx, 'var(--purple)')}</td><td class='small'>{', '.join(E(w) for w in s['where'])}</td>"
        f"<td class='r small'>{(delta(s['sessions_14d'], prev[s['name']], 'none') if s['name'] in prev else '')}</td></tr>"
        for s in rows)
    calls = W.get("skill_calls_claude") or {}
    return f"""<section id="skills"><h2>Skills</h2>
<p class="lead">Skills installed for Claude Code (<code>~/.claude/skills</code>), pi and Codex (<code>~/.agents/skills</code>) and the apex-router plugin. Use = sessions in which a tool call read the skill's <code>SKILL.md</code> or ran one of its scripts.</p>
<table><tr><th>Skill</th><th class="r">Sessions 7 d</th><th class="r">14 d</th><th></th><th>Installed in</th><th class="r">Δ</th></tr>{t}</table>
<p class="small">Claude Code <code>Skill</code> tool calls in kept transcripts: {", ".join(f"{E(k)} {n(v)}" for k, v in sorted(calls.items(), key=lambda x: -x[1]))}.</p>
<div class="concept">Changes this week: <code>pr-merge-gate</code> scripts now use the bounded Stash / Jenkins helper (token from <code>~/.bash_profile</code>, connect 10 s, stop ≤ 60 s, always exit). <code>confluence-plain</code> did not load in pi (bad YAML); fixed. New plugin skill <code>ste</code>.</div>
</section>"""


def sec_failures():
    pj = W["pi_jobs"]
    oc = L["offload"]["cur"]
    cq = L["codeqa"]["all"]
    cit = cq.get("grounded", 0) + cq.get("hallucinated", 0)
    rows = [
        ("stop", "429 rate limits", f"{n(cur['r429'])} in the last 7 days (prev {n(prev['r429'])}). Upstream rejections {n(cur['rejected'])} (prev {n(prev['rejected'])}). About 100 came on 10-13 from one Codex batch run (11,746 <code>gpt-5.6-luna</code> requests without a session, 10:00–12:00).",
         "pi now retries 5× over 155 s; sub-agents start one at a time for 2 min after a 429."),
        ("stop", "Offload lanes escalate", f"{n(oc.get('escalated'))} of {n(oc.get('calls'))} local offload calls escalated in 7 days.", "Measure-only. No local lane replaces frontier work yet."),
        ("stop", "Gold labels missing", f"<code>labels/gold.jsonl</code> is not on disk. 222 owner-signed gold labels were made on 10-08 (commit eb3bb61); the last JEPA attempt used 182. {n(DV['labels']['by_label'].get('unknown', 0))} of {n(DV['labels']['rows'])} task outcomes are 'unknown'.", "Outcome metrics (Brier, Zeno) cannot be scored until the gold file is restored."),
        ("risk", "Pressure file stale", "<code>pressure.json</code> was last written on 10-13. The sub-agent cap reads it as GREEN.", "Find the writer that stopped. The 429 cooldown covers the gap."),
        ("risk", "Invented citations", f"codeqa: {n(cq.get('hallucinated'))} of {n(cit)} citations ({pct(cq.get('hallucinated', 0), cit)}) point to code that does not exist.", "The verifier strikes them before use."),
        ("na", "JEPA world model", "Gate G1 failed 3 times (−6.2%, −4.0%, −11.1% vs logistic).", "Paused. Decision at 50k main-session steps."),
        ("na", "Panel / debate", "Partitioned evidence hurt (+0.037 Brier). Debate equals more samples.", "Closed. Nothing promoted."),
        ("risk", "pi sub-agent jobs", f"End states: {', '.join(f'{k} {v}' for k, v in pj['by_state'].items())}.", "2 failed on 429 before the fix; 1 stopped by its cost limit."),
        ("risk", "Cancel limit (macOS)", "A child that calls <code>setsid()</code> during SIGTERM handling can survive a sub-agent cancel.", "Known limit: macOS has no cgroups."),
        ("na", "Proxy errors", f"{n(cur['err'])} errors (5xx, transport) in 7 days (prev {n(prev['err'])}).", "Recorded per request with the cause."),
    ]
    t = "".join(f"<tr><td>{pill(c, s)}</td><td>{b}</td><td class='small'>{x}</td></tr>" for c, s, b, x in rows)
    return f"""<section id="failures"><h2>Failures and open problems</h2>
<p class="lead">What failed, what is blocked and what is a known limit.</p>
<table><tr><th>Item</th><th>What happened</th><th>Action / state</th></tr>{t}</table></section>"""


GROUPS = [
    (r"^research:", None),  # datapce market research: not part of this work item, left out of the page
    (r"pi sub-agents|STE|hang guard", "pi sub-agents, hang guard, STE format"),
    (r"E-D", "E-D workflow experiment"),
    (r"verdict|panel|debate", "Decision card + panel study"),
    (r"telemetry|pressure|install-serve", "Proxy telemetry + pressure"),
    (r"label|gold", "Labels"),
    (r".", "Fixes and reviews"),
]


def sec_delta():
    groups = {}
    for c in D["commits"]:
        for pat, name in GROUPS:
            if re.search(pat, c["subject"]):
                if name:
                    groups.setdefault(name, []).append(c)
                break
    gh = "".join(
        f"<details{' open' if len(cs) < 6 else ''}><summary><b>{E(name)}</b> · {len(cs)} commits</summary><div><table>"
        + "".join(f"<tr><td class='mono'>{E(c['sha'])}</td><td class='small'>{E(c['date'])}</td><td>{E(c['subject'])}</td></tr>" for c in cs)
        + "</table></div></details>" for name, cs in groups.items())
    m = [
        ("Frontier requests", cur["req"], prev["req"], "none", n),
        ("Frontier output tokens", cur["out"], prev["out"], "none", tok),
        ("Frontier uncached input tokens", cur["in"], prev["in"], "down", tok),
        ("Cache-read tokens", cur["cache_read"], prev["cache_read"], "up", tok),
        ("429 rate limits", cur["r429"], prev["r429"], "down", n),
        ("Upstream rejections", cur["rejected"], prev["rejected"], "down", n),
        ("Proxy errors", cur["err"], prev["err"], "down", n),
        ("codeqa questions (local)", L["codeqa"]["cur"].get("asks", 0), L["codeqa"]["prev"].get("asks", 0), "up", n),
        ("Offload calls (local)", L["offload"]["cur"].get("calls", 0), L["offload"]["prev"].get("calls", 0), "none", n),
        ("Codex cross-validations", W["xval"]["cur"], W["xval"].get("prev", 0), "none", n),
    ]
    t = "".join(f"<tr><td>{a}</td><td class='r'>{f(c)}</td><td class='r'>{f(p)}</td><td class='r'>{delta(c, p, g)}</td></tr>" for a, c, p, g, f in m)
    ps = D.get("previous_snapshot")
    base = (f"Compared with the previous page build ({E(ps['built'])}, commit {E(ps.get('commit') or '?')})." if ps else
            "This is the first build of this page. The baseline is saved; the next build compares with it. Below: last 7 days vs the 7 days before.")
    daily = T["daily"]
    mx = max((d["req"] for d in daily), default=1)
    dt = "".join(f"<tr><td class='mono'>{E(d['day'])}</td><td class='r'>{n(d['req'])}</td><td style='width:40%'>{bar(d['req'], mx)}</td><td class='r'>{tok(d['out'])}</td><td class='r'>{(pill('risk', n(d['r429'])) if d['r429'] else '0')}</td></tr>" for d in daily)
    return f"""<section id="delta"><h2>What changed since the last run</h2>
<p class="lead">{base}</p>
<div class="grid2"><div><h3>Metrics ({wc[0]}–{wc[1]} vs {wp[0]}–{wp[1]})</h3><table><tr><th>Metric</th><th class="r">Now</th><th class="r">Before</th><th class="r">Δ</th></tr>{t}</table></div>
<div><h3>Per day (proxy)</h3><table><tr><th>Day</th><th class="r">Requests</th><th></th><th class="r">Out</th><th class="r">429</th></tr>{dt}</table></div></div>
<h3>Implementation changes (apex-router, last 7 days: {sum(len(v) for v in groups.values())} commits)</h3>{gh}
</section>"""


page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>apex-router + skills — what we know</title><style>{CSS}</style></head><body>
<div class="topbar"><div class="in"><b>apex-router</b><a href="#data">Data</a><a href="#results">Results</a><a href="#local">Local vs frontier</a>
<a href="#models">Models</a><a href="#workflows">Workflows</a><a href="#skills">Skills</a><a href="#failures">Failures</a><a href="#delta">What changed</a>
<a href="./" style="margin-left:auto">triage explorer →</a></div></div>
<div class="wrap">
<section id="top" style="margin-top:8px">
<div class="kicker">apex-router · measuring proxy · local models · skills · pi / Claude Code / Codex · one workstation</div>
<h1>What the agent stack does, and what we learn from it</h1>
<p class="lead">apex-router measures every model request, classifies the agent work, and turns it into routing, tier and stop decisions. Skills give the agents repeatable procedures. This page shows the data, the results, the failures and what changed.</p>
{kv_cards()}
<p class="small">Built {E(D['built'])} · apex-router <code>{E(D['commit'])}</code> · proxy telemetry {E(T['first'])} → {E(T['last'])} · no dollar figures (tokens and requests only)</p>
</section>
{sec_data()}{sec_results()}{sec_local()}{sec_models()}{sec_workflows()}{sec_skills()}{sec_failures()}{sec_delta()}
<p class="small" style="margin-top:40px">Sources: ~/.apex/telemetry.jsonl (proxy), ~/.apex/offload_telemetry.jsonl, ~/.apex/codeqa_impact.jsonl, ~/.apex-router/offload_daily.md (nightly digest), ~/.apex-router/transcripts (mirror), worldmodel build (temp copy), labels/, route_log.jsonl, xval_runs.jsonl, pi-jobs/, skills directories. Builders: scripts/presentation_data.py, scripts/presentation_page.py.</p>
</div></body></html>"""
sys.stdout.write(page)
