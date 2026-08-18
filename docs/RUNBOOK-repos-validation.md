# Runbook: register & validate repos (webapp, qa-suite, apex-router)

End-to-end setup + **post-install validation** for the codeqa repos. Works for any repo;
webapp / qa-suite / apex-router are the worked examples. **Repo roots are user-defined** — set
them to wherever *your* checkouts live (`~/dev`, `~/Desktop`, anywhere); nothing is assumed.

The validation gate is `codeqa doctor` — it reports per-repo health and, with `--check`, exits
nonzero if any repo is unusable, so it can gate an install script or CI.

---

## 0. Prerequisites

```bash
PY=~/.apex-router/.venv/bin/python                 # apex-router's own venv (has the codeqa engine)
export CODEQA_REPOS=~/.apex-router/codeqa/repos    # where codeqa reads repo configs; add to ~/.zshrc
mkdir -p "$CODEQA_REPOS"
```

Local model reachable (needed only for `ask`/`ground`, not for `doctor`/`retrieve`). codeqa reaches
the model at `ORNITH_URL` (default `http://127.0.0.1:8080`); this works whether that endpoint is a
direct model server or a proxy fronting an OpenAI-compatible backend:
```bash
BASE="${ORNITH_URL:-http://127.0.0.1:8080}"
curl -s -m 3 -o /dev/null -w '%{http_code}\n' "$BASE/v1/models" | grep -q 200 \
  && echo "model up" || echo "model DOWN"
```

## 1. Register each repo (root = YOUR checkout path)

A repo config is one JSON in `$CODEQA_REPOS/<name>.json`. Point `root` at where the repo
actually lives on this machine. Minimal config:

```bash
# EDIT these three to your machine — e.g. ROOT_BASE=~/Desktop or ~/dev
ROOT_BASE=~/Desktop

cat > "$CODEQA_REPOS/webapp.json" <<JSON
{ "name": "webapp", "root": "$ROOT_BASE/webapp", "language": "ruby",
  "search_globs": ["app/**","lib/**","components/**","services/**","webservices/**","config/**"],
  "code_exts": [".rb",".rake",".erb"],
  "definition_patterns": ["(class|module)\\\\s+{sym}\\\\b","def\\\\s+(self\\\\.)?{sym}\\\\b"] }
JSON

cat > "$CODEQA_REPOS/qa-suite.json" <<JSON
{ "name": "qa-suite", "root": "$ROOT_BASE/qa-suite", "language": "ruby",
  "search_globs": ["**"], "code_exts": [".rb",".rake"],
  "definition_patterns": ["(class|module)\\\\s+{sym}\\\\b","def\\\\s+(self\\\\.)?{sym}\\\\b"] }
JSON

cat > "$CODEQA_REPOS/apex.json" <<JSON
{ "name": "apex", "root": "$ROOT_BASE/apex-router", "language": "python",
  "search_globs": ["src/**","tests/**"], "code_exts": [".py"],
  "definition_patterns": ["def\\\\s+{sym}\\\\b","class\\\\s+{sym}\\\\b"] }
JSON
```

`digest` is **optional** — omit it (codeqa degrades cleanly) or point it at a markdown file
under `$CODEQA_REPOS/../digests/`. `definition_patterns` use `{sym}` as the identifier slot.

## 2. VALIDATE — the gate

```bash
$PY -m apex_router.codeqa.cli doctor
```
Reads every config in `$CODEQA_REPOS` and prints one line each:
```
  [OK ] webapp        root✓ code=1 digest✓   /Users/you/Desktop/webapp
  [BAD] qa-suite         root✗MISSING code=? digest✗   /Users/you/dev/qa-suite   ← root path wrong for THIS machine
```
- `root✗MISSING` → the `root` doesn't exist here. **Fix the path** (this is the #1 issue —
  a config from another machine, or `~/dev` vs `~/Desktop`).
- `code=0` → root exists but `search_globs`/`code_exts` match no file. Fix the globs/exts.
- `digest✗` → non-fatal warning; codeqa still answers, just without pinned architecture context.

Gate an install / CI (nonzero if ANY repo is unusable):
```bash
$PY -m apex_router.codeqa.cli doctor --check ; echo "exit=$?"
```

## 3. End-to-end smoke (per repo)

```bash
# retrieval only — proves globs/root find code, NO model needed:
$PY -m apex_router.codeqa.cli retrieve webapp "where is policy computed"

# full grounded answer — needs the local model reachable (see the readiness check above).
# --max-tokens is optional: omit it to use the repo config's max_tokens, else the 1200 default.
$PY -m apex_router.codeqa.cli ask webapp "what does the policy service do"
$PY -m apex_router.codeqa.cli ask qa-suite  "how are regression suites structured"
$PY -m apex_router.codeqa.cli ask apex    "what does the proxy shadow handler do"

# ground a finding's file:line citations against live code — NO model needed (deterministic).
# exits 2 if any citation is 'stale' (file exists but the cited line is past end-of-file).
echo "the bug is at webapp/app/policy.rb:42" | $PY -m apex_router.codeqa.cli ground --check
```

## 4. apex-router package validation (separate from repos)

```bash
apex-router status            # routing / embedding / ornith tiers
apex-router verify            # exits 0 if routing works
$PY -m pytest ~/.apex-router/tests -q   # the package's own tests (skip proxy_engine if numpy absent)
```

---

## Quick reference — the validation one-liner

```bash
CODEQA_REPOS=~/.apex-router/codeqa/repos \
  ~/.apex-router/.venv/bin/python -m apex_router.codeqa.cli doctor --check
```
Green (`all repos healthy`, exit 0) → registered + reachable. Any `[BAD]` line names the repo and
the reason. Roots are yours to set — `doctor` only checks they exist here; it never rewrites them.
