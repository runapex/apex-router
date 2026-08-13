# Runbook: collect metrics from teammates' machines (pull-based)

The apex/codeqa/ornith stack writes metrics **locally on each machine** and ships nothing
off-box by design. This is the pull-based collection flow: each teammate runs one export
command that emits a **redacted aggregate JSON**, hands it back, and you merge N of them
locally. No server, no daemon, no auto-exfiltration — they see exactly what's shared.

**What's shared (aggregates only):** codeqa grounding rates + token counts, codeqa
local-vs-frontier routing share, offload tokens-saved by lane, per-task-type escalation rate,
per-repo health (name + booleans). **What's NOT shared:** raw prompts, file contents, file
paths, hostnames (the host is a hash), usernames. The exporter self-checks its own output for
leaks and refuses (exit 2) if anything looks like a path/email/long-text value.

---

## Kick it — each teammate runs this ONE command

```bash
CODEQA_REPOS=~/.apex-router/codeqa/repos \
  ~/.apex-router/.venv/bin/python ~/.apex-router/scripts/metrics_export.py \
  > ~/metrics-$(hostname -s)-$(date +%Y%m%d).json
```
(If they run from a source checkout instead of the install, use that path to
`scripts/metrics_export.py`; the script adds its own `src/` to the path.)

- Exit 0 + a JSON file → send you that file (Slack/email/drop it in a shared folder).
- Exit 2 → the redaction self-check tripped; it prints what. Do NOT send the file; report the
  flagged value so the exporter can be tightened before anyone shares data.

Sanity-check what they're about to send (optional, reassuring):
```bash
python3 -m json.tool ~/metrics-*.json | head -40      # it's short numeric aggregates
```

## Automate it (optional) — one command they can alias/cron themselves

They can wrap it so it's a single word. This is still pull-based (writes a local file); nothing
leaves the machine until they hand you the file:
```bash
echo 'alias apex-metrics="CODEQA_REPOS=~/.apex-router/codeqa/repos ~/.apex-router/.venv/bin/python ~/.apex-router/scripts/metrics_export.py"' >> ~/.zshrc
# then any time:  apex-metrics > ~/metrics-$(hostname -s).json
```

---

## Collect + analyze (your side)

Drop every `metrics-*.json` you receive into one dir, then merge:
```bash
mkdir -p ~/apex-metrics && mv ~/Downloads/metrics-*.json ~/apex-metrics/ 2>/dev/null
python3 - ~/apex-metrics/*.json <<'PY'
import json, sys
rows = [json.load(open(f)) for f in sys.argv[1:]]
print(f"{'host':14} {'ask_n':>6} {'grounded%':>9} {'local_share':>11} {'esc_rate':>8} {'repos_ok':>9}")
for r in rows:
    a = r.get("codeqa_ask", {}); v = r.get("codeqa_validate", {})
    g = (a.get("grounded",0) / (a.get("grounded",0)+a.get("hallucinated",0))) if (a.get("grounded",0)+a.get("hallucinated",0)) else None
    esc = r.get("escalation", {})
    esc_rate = sum(c.get("escalated",0) for c in esc.values()) / max(1, sum(c.get("n",0) for c in esc.values())) if esc else None
    rh = r.get("repos_health", [])
    ok = f"{sum(1 for x in rh if x.get('ok'))}/{len(rh)}"
    print(f"{r.get('host','?'):14} {a.get('n_questions',0):>6} "
          f"{('%.2f'%g) if g is not None else '  -':>9} "
          f"{('%.3f'%v['local_share']) if v.get('local_share') is not None else '  -':>11} "
          f"{('%.2f'%esc_rate) if esc_rate is not None else '  -':>8} {ok:>9}")
PY
```
Add columns as needed — every field in `apex-metrics/1` is a plain number or bool, so slicing
it (pandas, a spreadsheet, a chart) is trivial. The schema is versioned (`"schema":
"apex-metrics/1"`) so a future change is detectable.

## What each metric means (for interpretation)

| Field | Meaning | Good direction |
|---|---|---|
| `codeqa_ask.grounded / (grounded+hallucinated)` | codeqa answer grounding rate | higher |
| `codeqa_validate.local_share` | fraction of verifier calls kept on the FREE local model | higher = more frontier tokens avoided |
| `offload.by_lane.*.frontier_completion_tokens_saved` | tokens the gated offload actually saved | higher (only `codegen`/gated counts) |
| `escalation.<task>.rate` | how often a cheap-started task bounced to frontier | lower = cheap-start paying off |
| `repos_health[].ok` | codeqa repo registered + reachable on that machine | all true |

---

## One-shot checklist
```
[ ] teammate ran metrics_export.py, got exit 0 (or reported the exit-2 leak)
[ ] file received; json.tool shows short numeric aggregates, no paths/text
[ ] merged all metrics-*.json; per-host table renders
[ ] interpreted against the meaning table (grounding, local_share, escalation, repos_ok)
```
