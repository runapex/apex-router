"""Action classes for tool calls (DESIGN-worldmodel-P6.md §2) and the test-output parser.

The lesson this module exists for (PLAN-workflow-graph-optimizer.md, "three lessons" #1): a
first-word bash classifier calls almost half the calls "shell" because of ``echo`` / ``timeout`` /
``cd`` / ``VAR=`` prefixes, and lands ``.venv/bin/pytest`` and ``python -m pytest`` in "run". So a
bash command is cut into **segments** (``;`` ``&&`` ``||`` ``|`` ``&`` newlines, outside quotes,
heredoc bodies skipped, ``$(...)`` scanned as its own command), each segment has its prefixes
stripped (``cd``-only segments, ``timeout N``, ``env``, ``VAR=x``, ``time``, ``nohup``, ``xargs``,
``uv run``, ``npx`` ...) and is classified by its command word (basename, so ``.venv/bin/pytest``
is ``pytest``), and the command takes the highest-priority class over its segments:

    test > build > vcs > remote > run > edit > write > search > read      (nothing: ``other``)

The contract fixes the order of the first five; the four below ``run`` are this module's choice
so that a bash ``rg``/``cat``/``sed -i`` is not lumped into ``run`` (pi runs most reads through
bash). A segment of pure shell plumbing (``echo``, ``cd``, ``sleep``, ``export``, ``[ -f x ]``)
contributes nothing; a command made only of those is ``other``.

Non-bash tools map by tool name (``TOOL_CLASS``). Nothing here stores or returns text.
"""
from __future__ import annotations

import os
import re
import shlex

CLASSES = ("search", "read", "edit", "write", "run", "test", "build", "vcs", "remote", "plan",
           "delegate", "ask", "other")
PRIORITY = ("test", "build", "vcs", "remote", "run", "edit", "write", "search", "read")
_RANK = {c: i for i, c in enumerate(PRIORITY)}
PHASES = ("explore", "edit", "verify", "deliver", "other")

BASH_TOOLS = {"bash", "Bash", "BashOutput", "shell", "exec_command", "local_shell"}
SPAWN_TOOLS = {"Agent", "Task", "Workflow"}
TOOL_CLASS = {
    "read": "read", "Read": "read", "NotebookRead": "read", "ls": "read", "LS": "read",
    "Glob": "search", "glob": "search", "Grep": "search", "grep": "search", "find": "search",
    "WebSearch": "search",
    "Edit": "edit", "edit": "edit", "MultiEdit": "edit", "NotebookEdit": "edit",
    "apply_patch": "edit",
    "Write": "write", "write": "write",
    "Agent": "delegate", "Task": "delegate", "Workflow": "delegate", "SendMessage": "delegate",
    "TaskStop": "delegate", "TaskOutput": "delegate", "ListAgents": "delegate",
    "KillShell": "run",
    "AskUserQuestion": "ask", "SubagentHandback": "ask",
    "TodoWrite": "plan", "ExitPlanMode": "plan", "EnterPlanMode": "plan", "Skill": "plan",
    "TaskCreate": "plan", "TaskUpdate": "plan", "TaskList": "plan", "CronCreate": "plan",
    "CronDelete": "plan", "CronList": "plan",
    "WebFetch": "remote", "computer": "remote",
}

# ---- command vocabularies -------------------------------------------------------------------

NEUTRAL = {"cd", "pushd", "popd", "echo", "printf", "export", "set", "unset", "source", ".",
           "true", "false", ":", "sleep", "wait", "exit", "return", "read", "local", "declare",
           "typeset", "trap", "ulimit", "alias", "unalias", "shopt", "setopt", "test", "[", "[[",
           "]]", "]", "date", "clear", "fi", "done", "esac", "for", "case", "in", "function",
           "break", "continue", "shift", "let", "hash", "emulate", "autoload", "then", "else",
           "do", "select", "umask", "history", "builtin", "noglob", "rehash", "}", ")", "{", "(",
           "seq", "yes"}
TEST_BINS = {"pytest", "py.test", "jest", "vitest", "mocha", "rspec", "tox", "nox", "ctest",
             "bats", "phpunit", "ava", "karma", "nose2", "unittest", "pytest-3"}
TEST_MODS = {"pytest", "unittest", "nose", "nose2", "tox", "nox"}
BUILD_MODS = {"build", "pip", "compileall", "py_compile", "mypy", "ruff", "black", "flake8",
              "pylint", "isort", "venv", "ensurepip", "pyright", "pyflakes", "pycodestyle"}
BUILD_BINS = {"tsc", "eslint", "prettier", "ruff", "mypy", "black", "isort", "flake8", "pylint",
              "pyright", "gcc", "g++", "clang", "clang++", "cc", "c++", "javac", "rustc", "swiftc",
              "cmake", "ninja", "meson", "pip", "pip3", "pre-commit", "shellcheck", "biome",
              "webpack", "esbuild", "rollup", "brew", "pipx", "maturin", "autoreconf",
              "configure", "ld", "ar", "rustup", "nvm", "pod", "bundle", "gem", "conda", "mamba",
              "terraform-fmt", "actionlint", "yamllint", "markdownlint", "hadolint", "stylelint",
              "xcodegen", "protoc", "swiftlint", "swiftformat", "gofmt", "golangci-lint"}
VCS_BINS = {"git", "gh", "hg", "svn", "jj", "glab"}
REMOTE_BINS = {"curl", "wget", "http", "https", "ssh", "scp", "sftp", "nc", "telnet", "ping",
               "dig", "nslookup", "host", "openssl", "kubectl", "helm", "aws", "gcloud", "az",
               "fly", "flyctl", "vercel", "netlify", "heroku", "terraform", "gsutil", "mosh",
               "ncat", "socat", "traceroute", "whois", "ftp", "rclone", "wrangler", "doctl",
               "linode-cli", "doctl", "hcloud", "oci", "ibmcloud"}
SEARCH_BINS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "find", "fd", "locate", "mdfind",
               "ugrep", "zgrep"}
READ_BINS = {"cat", "bat", "less", "more", "head", "tail", "ls", "tree", "stat", "file", "wc",
             "du", "df", "pwd", "which", "whereis", "type", "realpath", "readlink", "basename",
             "dirname", "diff", "cmp", "sed", "awk", "gawk", "jq", "yq", "cut", "sort", "uniq",
             "tr", "column", "nl", "xxd", "hexdump", "od", "strings", "sha256sum", "shasum", "md5",
             "md5sum", "sha1sum", "printenv", "ps", "lsof", "id", "uname", "sw_vers", "otool",
             "nm", "zcat", "comm", "paste", "fold", "rev", "tac", "fmt", "expand", "tokei",
             "cloc", "pgrep", "whoami", "hostname", "groups", "uptime", "vm_stat",
             "sysctl", "top", "free", "nproc", "getconf", "ioreg", "system_profiler", "tput",
             "locale", "command-v", "mdls", "xattr", "codesign", "lipo", "dwarfdump", "ldd",
             "pbpaste", "look", "csvlook", "pdftotext", "exiftool", "identify", "plutil",
             "pdfinfo", "mdfind-read"}
WRITE_BINS = {"mkdir", "touch", "cp", "mv", "rm", "rmdir", "ln", "chmod", "chown", "unzip",
              "trash", "dd", "mktemp", "truncate", "install", "ditto", "gzip", "gunzip", "zip",
              "split", "chflags", "setfacl", "mkfifo"}
EDIT_BINS = {"patch", "ed", "ex", "apply_patch"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
# Runners that execute their argument: classify what they run.
_RUN_PREFIX = {"poetry": "run", "pipenv": "run", "pdm": "run", "hatch": "run", "rye": "run",
               "conda": "run", "mamba": "run", "pixi": "run"}
_SKIP_WORDS = {"time", "nohup", "sudo", "command", "exec", "builtin", "caffeinate", "stdbuf",
               "unbuffer", "nice", "ionice", "chronic", "doas", "env", "timeout", "gtimeout",
               "xargs", "watch", "then", "do", "else", "elif", "if", "while", "until", "!", "{",
               "(", "noglob", "arch", "flock", "taskpolicy"}
# Flags of skip-words that take a value.
_SKIP_ARGFLAGS = {"timeout": {"-s", "-k", "--signal", "--kill-after"},
                  "gtimeout": {"-s", "-k", "--signal", "--kill-after"},
                  "env": {"-u", "-C", "-S", "--unset", "--chdir"},
                  "nice": {"-n"}, "xargs": {"-I", "-n", "-P", "-L", "-d", "-E", "-s", "-J"},
                  "sudo": {"-u", "-g"}, "watch": {"-n"}, "flock": {"-w"},
                  "stdbuf": set(), "arch": set(), "taskpolicy": {"-c"}}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_PY = re.compile(r"^(python|pypy)(\d+(\.\d+)?)?$|^py$")
_REDIR = re.compile(r"^(\d*|&)(>>?|>\|)(.*)$")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_VARREF = re.compile(r"^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))$")
_PLACEHOLDER = re.compile(r"^<[A-Za-z_]+>$")      # a word like <path> is not a redirection
_FUNCDEF = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*\(\)\{?$")
_SINK = ("/dev/null", "/dev/stderr", "/dev/stdout", "/dev/tty")


def _best(classes) -> str | None:
    cs = [c for c in classes if c in _RANK]
    return min(cs, key=_RANK.__getitem__) if cs else None


# ---- splitting ------------------------------------------------------------------------------

def _strip_heredocs(cmd: str) -> str:
    """Drop heredoc bodies (they are data — a python script, a commit message), keep the line
    that opens them. Line continuations are joined first."""
    cmd = cmd.replace("\\\n", " ")
    out, pending = [], []
    for line in cmd.split("\n"):
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        out.append(line)
        # markers in quoted strings are rare enough to accept; a commit message saying "<<EOF"
        # would only drop the rest of that command (fail toward fewer segments, not wrong ones)
        pending = [m.group(2) for m in _HEREDOC.finditer(line)]
    return "\n".join(out)


def _split(cmd: str) -> tuple[list[str], list[str]]:
    """-> (segments, command substitutions). Quote-aware; ``#`` comments dropped."""
    segs, subs = [], []
    cur: list[str] = []
    i, n = 0, len(cmd)
    q = None                # quote char
    while i < n:
        ch = cmd[i]
        if q:
            if ch == "\\" and q == '"' and i + 1 < n:
                cur.append(cmd[i:i + 2])
                i += 2
                continue
            if ch == q:
                q = None
            elif q == '"' and cmd.startswith("$(", i):
                j = _match_paren(cmd, i + 1)
                subs.append(cmd[i + 2:j])
                cur.append(cmd[i:j + 1])
                i = j + 1
                continue
            cur.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            cur.append(cmd[i:i + 2])
            i += 2
            continue
        if ch in "'\"`":
            q = ch
            cur.append(ch)
            i += 1
            continue
        if cmd.startswith("$(", i):
            j = _match_paren(cmd, i + 1)
            subs.append(cmd[i + 2:j])
            cur.append(" __SUBST__ ")
            i = j + 1
            continue
        if ch == "#" and (not cur or cur[-1][-1:].isspace() or cur[-1][-1:] in ";&|("):
            while i < n and cmd[i] != "\n":
                i += 1
            continue
        if ch in ";\n|&":
            # keep redirections like 2>&1, &>, >& intact
            if ch == "&" and (cmd[i - 1:i] in (">", "<") or cmd[i + 1:i + 2] == ">"):
                cur.append(ch)
                i += 1
                continue
            segs.append("".join(cur))
            cur = []
            i += 2 if cmd[i:i + 2] in ("&&", "||", "|&", ";;") else 1
            continue
        cur.append(ch)
        i += 1
    segs.append("".join(cur))
    return [s.strip() for s in segs if s.strip()], subs


def _match_paren(s: str, i: int) -> int:
    """Index of the ``)`` closing the ``(`` at ``i`` (quote-aware); len(s)-1 if unbalanced."""
    depth, q = 0, None
    for j in range(i, len(s)):
        c = s[j]
        if q:
            if c == q:
                q = None
            continue
        if c in "'\"":
            q = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j
    return len(s) - 1


def _tokens(seg: str) -> list[str]:
    try:
        return shlex.split(seg, posix=True)
    except ValueError:
        return seg.split()


# ---- per-segment classification ------------------------------------------------------------

def _strip_redirs(toks: list[str]) -> tuple[list[str], bool]:
    """Remove redirections; -> (tokens, writes_a_file)."""
    out, writes, skip = [], False, False
    for k, t in enumerate(toks):
        if skip:
            skip = False
            continue
        if t in ("<", "<<", "<<<", "<<-"):
            skip = True
            continue
        if t.startswith("<") and len(t) > 1 and not _PLACEHOLDER.match(t):
            continue
        m = _REDIR.match(t)
        if m:
            target = m.group(3)
            if not target:
                target = toks[k + 1] if k + 1 < len(toks) else ""
                skip = True
            if target and not target.startswith("&") and not target.startswith(_SINK):
                writes = True
            continue
        out.append(t)
    return out, writes


def _strip_prefixes(toks: list[str]) -> list[str]:
    i = 0
    while i < len(toks):
        t = toks[i]
        if _ASSIGN.match(t):
            i += 1
            continue
        w = t.lstrip("({!")
        if not w:
            i += 1
            continue
        if w != t:
            toks = toks[:i] + [w] + toks[i + 1:]
            if _ASSIGN.match(w):
                i += 1
                continue
        if w in _SKIP_WORDS:
            argflags = _SKIP_ARGFLAGS.get(w, set())
            i += 1
            while i < len(toks) and (toks[i].startswith("-") or _ASSIGN.match(toks[i])):
                f = toks[i].split("=", 1)[0]
                i += 2 if (f in argflags and "=" not in toks[i]) else 1
            if w in ("timeout", "gtimeout") and i < len(toks) and re.match(r"^\d", toks[i]):
                i += 1
            continue
        break
    return toks[i:]


def _sub(args: list[str]) -> tuple[str | None, list[str]]:
    """First non-flag argument and the rest after it."""
    for k, a in enumerate(args):
        if not a.startswith("-"):
            return a, args[k + 1:]
    return None, []


def _after_flags(args: list[str], argflags=()) -> list[str]:
    k = 0
    while k < len(args) and args[k].startswith("-"):
        k += 2 if (args[k] in argflags and "=" not in args[k]) else 1
    return args[k:]


def _js_pm(base: str, args: list[str], depth: int) -> str:
    sub, rest = _sub(args)
    if sub is None:
        return "build" if base == "yarn" else "run"
    if sub in ("test", "t", "tst"):
        return "test"
    if sub in ("run", "run-script"):
        script, _ = _sub(rest)
        if script is None:
            return "run"
        s = script.lower()
        if "test" in s or s in ("spec", "e2e"):
            return "test"
        if re.search(r"build|lint|typecheck|type-check|tsc|compile|format|check|fmt", s):
            return "build"
        return "run"
    if sub in ("install", "i", "ci", "add", "remove", "rm", "uninstall", "update", "upgrade",
               "build", "rebuild", "link", "pack", "prune", "dedupe", "audit", "outdated",
               "init", "lint"):
        return "build"
    if sub in ("publish", "login", "view", "info"):
        return "remote"
    if sub in ("exec", "dlx", "x"):
        return _classify_tokens(rest, depth + 1) or "run"
    if base in ("yarn", "pnpm", "bun") and sub not in ("start", "dev", "serve"):
        if base == "bun" and re.search(r"\.(ts|js|mjs|tsx)$", sub):
            return "run"
        return _classify_tokens([sub] + rest, depth + 1) or "run"
    return "run"


def _classify_tokens(toks: list[str], depth: int = 0) -> str | None:
    if depth > 6:
        return "run"
    toks = [t.rstrip(")}") if t not in (")", "}") else t for t in toks]
    if toks[:1] == ["command"] and toks[1:2] in (["-v"], ["-V"]):
        return "read"
    toks = _strip_prefixes(toks)
    if not toks:
        return None
    head = toks[0]
    args = toks[1:]
    if head == "__SUBST__":
        return None
    if _FUNCDEF.match(head) or head == "function":
        # ``name() { body; }``: the body's commands are segments of their own and are scanned
        rest = toks[1:]
        if head == "function":
            rest = rest[1:]
        rest = [t for t in rest if t not in ("{", "}", "()")]
        return _classify_tokens(rest, depth + 1) if rest else None
    vm = _VARREF.match(head)
    if vm:
        # an unresolved $VAR head: ``$PY -m pytest`` is still pytest; anything else is run
        name = (vm.group(1) or vm.group(2)).upper()
        if "-m" in args or "PY" in name:
            return _classify_tokens(["python"] + args, depth + 1)
        return "run"
    base = os.path.basename(head.rstrip("/")) if "/" in head else head
    if base in NEUTRAL:
        return None
    if _PY.match(base):
        if "-m" in args:
            k = args.index("-m")
            mod = (args[k + 1] if k + 1 < len(args) else "").split(".")[0]
            if mod in TEST_MODS:
                return "test"
            if mod in BUILD_MODS:
                return "build"
        return "run"
    if base in TEST_BINS:
        return "test"
    if base in SHELLS:
        for k, a in enumerate(args):
            if a.startswith("-") and "c" in a[1:] and not a.startswith("--"):
                body = args[k + 1] if k + 1 < len(args) else ""
                return classify_bash(body, depth + 1, default=None) or None
        return "run" if _sub(args)[0] else None
    if base == "eval":
        return classify_bash(" ".join(args), depth + 1, default=None)
    if base == "uv":
        sub, rest = _sub(args)
        if sub == "run":
            inner = _after_flags(rest, {"--with", "--python", "-p", "--extra", "--group",
                                        "--directory", "--project", "--env-file", "--package",
                                        "--with-requirements", "--index", "--from"})
            return (_classify_tokens(inner, depth + 1) or "run") if inner else "run"
        if sub == "tool":
            s2, rest2 = _sub(rest)
            if s2 == "run":
                inner = _after_flags(rest2, {"--with", "--python", "-p", "--from"})
                return (_classify_tokens(inner, depth + 1) or "run") if inner else "run"
            return "read" if s2 in ("list", "dir") else "build"
        if sub == "pip" and _sub(rest)[0] in ("list", "show", "freeze", "tree"):
            return "read"
        if sub in ("pip", "sync", "lock", "add", "remove", "build", "venv", "export", "tree",
                   "init", "python", "self", "publish", "cache"):
            return "remote" if sub == "publish" else "build"
        return "run"
    if base in ("uvx", "npx", "bunx") or (base == "pipx" and args[:1] == ["run"]):
        rest = args[1:] if base == "pipx" else args
        inner = _after_flags(rest, {"-p", "--package", "--from", "--with", "--python", "-c"})
        if not inner:
            return "run"
        return _classify_tokens(inner, depth + 1) or "run"
    if base in _RUN_PREFIX:
        sub, rest = _sub(args)
        if sub == "test":
            return "test"
        if sub == "run":
            inner = _after_flags(rest, {"-n", "--name", "-p", "--prefix", "-e", "--environment"})
            return (_classify_tokens(inner, depth + 1) or "run") if inner else "run"
        return "build"
    if base == "bundle":
        sub, rest = _sub(args)
        if sub == "exec":
            return _classify_tokens(rest, depth + 1) or "run"
        return "build"
    if base in ("npm", "pnpm", "yarn", "bun"):
        return _js_pm(base, args, depth)
    if base in ("make", "gmake"):
        targets, k = [], 0
        while k < len(args):
            a = args[k]
            if a in ("-C", "-f", "-j", "-I", "-o", "-W", "--directory", "--file"):
                k += 2
                continue
            if not a.startswith("-") and not _ASSIGN.match(a):
                targets.append(a)
            k += 1
        if any("test" in t or t in ("check", "spec", "ci") for t in targets):
            return "test"
        return "build"
    if base == "cargo":
        sub, _ = _sub([a for a in args if not a.startswith("+")])
        if sub in ("test", "nextest", "t"):
            return "test"
        if sub in ("run", "r", "bench"):
            return "run"
        if sub in ("publish", "login"):
            return "remote"
        return "build"
    if base == "go":
        sub, _ = _sub(args)
        if sub == "test":
            return "test"
        if sub == "run":
            return "run"
        return "build"
    if base in ("rails", "rake"):
        sub, _ = _sub(args)
        return "test" if sub and ("test" in sub or "spec" in sub) else "run"
    if base in ("dotnet", "mvn", "mvnw", "gradle", "gradlew", "swift", "xcodebuild", "bazel",
                "bazelisk", "buck", "buck2", "sbt", "lein", "mix", "deno", "flutter", "dart",
                "zig", "ant"):
        if any(a == "test" or a.startswith("test:") for a in args):
            return "test"
        if base in ("deno", "swift", "dart", "flutter", "zig", "mix"):
            sub, _ = _sub(args)
            return "build" if sub in ("build", "compile", "check", "lint", "fmt", "format",
                                      "pub", "deps.get") else "run"
        return "build"
    if base == "claude":
        sub, rest = _sub(args)
        if sub == "plugin" and _sub(rest)[0] == "test":
            return "test"
        return "run"
    if base == "docker" or base == "podman":
        sub, rest = _sub(args)
        if sub == "compose":
            sub, rest = _sub(rest)
        if sub == "build":
            return "build"
        if sub in ("push", "pull", "login"):
            return "remote"
        return "run"
    if base == "vite":
        return "build" if "build" in args else "run"
    if base in VCS_BINS:
        if base == "git":
            sub, _ = _sub(_after_flags(args, {"-C", "-c", "--git-dir", "--work-tree"}))
            if sub == "grep":
                return "search"
        return "vcs"
    if base == "openssl":
        # a TLS handshake goes over the network; x509/req/dgst on a local file is a read
        return "remote" if any(a in ("s_client", "s_server", "ocsp") for a in args) else "read"
    if base == "rsync":
        return "remote" if any(":" in a and not a.startswith("-") for a in args) else "write"
    if base in REMOTE_BINS:
        return "remote"
    if base in BUILD_BINS:
        return "build"
    if base == "sed" and any(a.startswith("-i") or a == "--in-place" or a.startswith("--in-place=")
                             or (a.startswith("-") and not a.startswith("--") and "i" in a[1:])
                             for a in args):
        return "edit"
    if base == "perl" and any(a.startswith("-") and "i" in a[1:] and not a.startswith("--")
                              for a in args):
        return "edit"
    if base in EDIT_BINS:
        return "edit"
    if base == "tee":
        return "write" if any(not a.startswith("-") for a in args) else None
    if base == "tar":
        return "write" if args and re.search(r"x|c", args[0].lstrip("-")) else "read"
    if base in WRITE_BINS:
        return "write"
    if base in SEARCH_BINS:
        return "search"
    if base in READ_BINS:
        if base == "cat" and not _sub(args)[0]:
            return None
        return "read"
    return "run"


def _expand(tok: str, env: dict) -> str:
    """``$P`` / ``${P}`` set earlier in the same command -> its value (leading space marks a
    multi-word value to re-split)."""
    m = _VARREF.match(tok)
    if not m:
        return tok
    v = env.get(m.group(1) or m.group(2))
    if v is None:
        return tok
    return " " + v if " " in v.strip() else v


def classify_bash(cmd: str, depth: int = 0, default: str | None = "other") -> str | None:
    """Class of one bash command string (never stored)."""
    if not isinstance(cmd, str) or not cmd.strip():
        return default
    try:
        segs, subs = _split(_strip_heredocs(cmd))
    except Exception:  # noqa: BLE001 — a command that cannot be parsed is "run"
        return "run"
    found = []
    env: dict = {}                      # P=.venv/bin/pytest; $P -q  -> the head is pytest
    for seg in segs:
        toks, writes = _strip_redirs(_tokens(seg))
        for t in (toks[1:] if toks[:1] == ["export"] else toks):
            m = _ASSIGN.match(t)
            if not m:
                break
            env[t[:m.end()].rstrip("+=")] = t[m.end():]
        toks = [_expand(t, env) for t in toks]
        toks = [w for t in toks for w in (t.split() if t.startswith(" ") else [t])]
        c = _classify_tokens(toks, depth)
        if writes:
            c = _best([c, "write"])
        found.append(c)
    for s in subs:
        found.append(classify_bash(s, depth + 1, default=None))
    return _best(found) or default


def classify(tool: str | None, inp=None) -> str:
    """Action class of one tool call: bash by command, everything else by tool name."""
    tool = tool or ""
    if tool in BASH_TOOLS:
        cmd = inp.get("command") if isinstance(inp, dict) else inp if isinstance(inp, str) else ""
        if isinstance(cmd, list):
            cmd = " ".join(str(x) for x in cmd)
        return classify_bash(cmd if isinstance(cmd, str) else "") or "other"
    if tool in TOOL_CLASS:
        return TOOL_CLASS[tool]
    if tool.startswith("mcp__claude-in-chrome__") or tool.startswith("mcp__playwright"):
        return "remote"
    return "other"


def is_spawn(tool: str | None) -> int:
    return int(tool in SPAWN_TOOLS)


# ---- test output ----------------------------------------------------------------------------

_PYTEST_LINE = re.compile(r"\b\d+ (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b"
                          r".*\bin \d+(\.\d+)?s\b|\bno tests ran in \d")
_COUNT = re.compile(r"(\d+) (passed|failed|errors?|error)\b")
_JEST = re.compile(r"^\s*Tests:\s+(.*?)\btotal\b", re.MULTILINE)
_VITEST = re.compile(r"^\s*Tests\s+(.*?)\(\d+\)\s*$", re.MULTILINE)
_CARGO = re.compile(r"test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed")
_RSPEC = re.compile(r"(\d+) examples?, (\d+) failures?")
_UNITTEST_RAN = re.compile(r"\bRan (\d+) tests? in \d")
_UNITTEST_FAIL = re.compile(r"^\s*(?:\d+\s+)?FAILED \(((?:failures|errors)=[^)]*)\)", re.MULTILINE)
_BUN_PASS = re.compile(r"^\s*(\d+) pass\s*$", re.MULTILINE)
_BUN_FAIL = re.compile(r"^\s*(\d+) fail\s*$", re.MULTILINE)
_MOCHA_PASS = re.compile(r"^\s*(\d+) passing\b", re.MULTILINE)
_MOCHA_FAIL = re.compile(r"^\s*(\d+) failing\b", re.MULTILINE)
_GO_PASS = re.compile(r"^\s*--- PASS:", re.MULTILINE)
_GO_FAIL = re.compile(r"^\s*--- FAIL:", re.MULTILINE)
_GO_OK = re.compile(r"^ok\s+\S+\s", re.MULTILINE)
_GO_FAILPKG = re.compile(r"^FAIL\s+\S+\s+[\d.]+s|^FAIL\s+\S+\s+\[", re.MULTILINE)


def parse_tests(text) -> tuple[int, int | None] | None:
    """(failed, passed) from a test runner's output, or None if no known summary is present.
    pytest (``N failed, M passed in Xs``; errors count as failed), jest, vitest, cargo test
    (summed over binaries), rspec, unittest (``FAILED (errors=N)`` alone gives failed with passed
    None), bun / ``claude plugin test`` (`` N pass`` / `` N fail``), mocha, go test (test-level ``--- PASS/FAIL`` lines
    when verbose, else package-level ``ok`` / ``FAIL`` lines)."""
    if not isinstance(text, str) or not text:
        return None
    m = _CARGO.findall(text)
    if m:
        return sum(int(f) for _, f in m), sum(int(p) for p, _ in m)
    for rx in (_JEST, _VITEST):
        hits = rx.findall(text)
        if hits:
            s = hits[-1]
            f = re.search(r"(\d+) failed", s)
            p = re.search(r"(\d+) passed", s)
            if f or p:
                return int(f.group(1)) if f else 0, int(p.group(1)) if p else 0
    lines = [ln for ln in text.splitlines() if _PYTEST_LINE.search(ln)]
    if lines:
        counts = {"passed": 0, "failed": 0}
        for n, kind in _COUNT.findall(lines[-1]):
            counts["passed" if kind == "passed" else "failed"] += int(n)
        return counts["failed"], counts["passed"]
    m = _RSPEC.findall(text)
    if m:
        n, f = (int(x) for x in m[-1])
        return f, max(0, n - f)
    ran = _UNITTEST_RAN.findall(text)
    fm = _UNITTEST_FAIL.findall(text)
    if ran or fm:
        f = sum(int(x) for x in re.findall(r"(?:failures|errors)=(\d+)", fm[-1])) if fm else 0
        if not ran:                     # summary cut off above the FAILED line: total unknown
            return f, None
        return f, max(0, int(ran[-1]) - f)
    bp, bf = _BUN_PASS.findall(text), _BUN_FAIL.findall(text)
    if bp or bf:                        # bun test / `claude plugin test`: " N pass" / " N fail"
        return int(bf[-1]) if bf else 0, int(bp[-1]) if bp else None
    mp, mf = _MOCHA_PASS.findall(text), _MOCHA_FAIL.findall(text)
    if mp or mf:
        return int(mf[-1]) if mf else 0, int(mp[-1]) if mp else 0
    gp, gf = len(_GO_PASS.findall(text)), len(_GO_FAIL.findall(text))
    if gp or gf:
        return gf, gp
    ok, fp = len(_GO_OK.findall(text)), len(_GO_FAILPKG.findall(text))
    if ok or fp:
        return fp, ok
    return None


def tests_field(act: str, output) -> dict:
    """The step's ``tests`` record: ran=1 for a ``test`` step, counts when the output parses."""
    if act != "test":
        return {"ran": 0, "failed": None, "passed": None}
    r = parse_tests(output)
    return {"ran": 1, "failed": r[0] if r else None, "passed": r[1] if r else None}


# ---- buckets + phases -----------------------------------------------------------------------

def size_bucket(n: int) -> int:
    return 0 if n < 1_000 else 1 if n < 10_000 else 2 if n < 100_000 else 3


def phase_of(act: str, edited: bool) -> str:
    """Phase of one step given only whether an edit/write happened earlier in its stream."""
    if act in ("edit", "write"):
        return "edit"
    if act in ("search", "read"):
        return "other" if edited else "explore"
    if act in ("test", "build", "run"):
        return "verify" if edited else "other"
    if act in ("vcs", "ask"):
        return "deliver"
    return "other"


def phases(acts: list[str]) -> list[str]:
    """Phase of each step of one stream (a task's main thread, or one subagent's steps).

    CAUSAL (DESIGN-worldmodel-P6.md §2 as amended): a step's phase depends only on itself and the
    steps before it, never on what comes later — appending steps never changes an earlier phase.
    explore: search/read with no edit so far · edit: edit/write · verify: test/build/run after an
    edit so far · deliver: vcs/ask · other: the rest."""
    out, edited = [], False
    for a in acts:
        out.append(phase_of(a, edited))
        edited = edited or a in ("edit", "write")
    return out
