# Runbook: register & validate repos (avenger, ultron, apex-router)

End-to-end setup + **post-install validation** for the codeqa repos. Works for any repo;
avenger / ultron / apex-router are the worked examples. **Repo roots are user-defined** — set
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

Ornith server up (needed only for `ask`, not for `doctor`/`retrieve`):
```bash
curl -s http://127.0.0.1:8080/v1/models | grep -qi ornith && echo "ornith up" || echo "ornith DOWN"
```

## 1. Register each repo (root = YOUR checkout path)

A repo config is one JSON in `$CODEQA_REPOS/<name>.json`. Point `root` at where the repo
actually lives on this machine. Minimal config:

```bash
# EDIT these three to your machine — e.g. ROOT_BASE=~/Desktop or ~/dev
ROOT_BASE=~/Desktop

cat > "$CODEQA_REPOS/avenger.json" <<JSON
{ "name": "avenger", "root": "$ROOT_BASE/avenger", "language": "ruby",
  "search_globs": ["app/**","lib/**","components/**","services/**","webservices/**","config/**"],
  "code_exts": [".rb",".rake",".erb"],
  "definition_patterns": ["(class|module)\\\\s+{sym}\\\\b","def\\\\s+(self\\\\.)?{sym}\\\\b"] }
JSON

cat > "$CODEQA_REPOS/ultron.json" <<JSON
{ "name": "ultron", "root": "$ROOT_BASE/ultron", "language": "ruby",
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
  [OK ] avenger        root✓ code=1 digest✓   /Users/you/Desktop/avenger
  [BAD] ultron         root✗MISSING code=? digest✗   /Users/you/dev/ultron   ← root path wrong for THIS machine
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
# retrieval only — proves globs/root find code, NO Ornith needed:
$PY -m apex_router.codeqa.cli retrieve avenger "where is policy computed"

# full grounded answer — needs the Ornith server up:
$PY -m apex_router.codeqa.cli ask avenger "what does the policy service do" --max-tokens 1500
$PY -m apex_router.codeqa.cli ask ultron  "how are regression suites structured" --max-tokens 1500
$PY -m apex_router.codeqa.cli ask apex    "what does the proxy shadow handler do" --max-tokens 1500
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
