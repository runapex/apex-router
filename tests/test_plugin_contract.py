"""Static guards on the hooks module (spec §10 privacy, §17.5 advise-only, §17.6 measurable-or-omitted)."""
import re
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / "plugin" / "hooks"


def _files() -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(HOOKS.rglob("*")) if p.suffix in (".ts", ".tsx")}


def _sources() -> str:
    return "\n".join(_files().values())


def _code(src: str) -> str:
    """Source without // comments (guards must not trip on prose)."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|(?<=[\s;,)])//[^\n'\"`]*$", "", src)


def _calls(code: str, names: tuple[str, ...]) -> list[tuple[str, str]]:
    """(name, argument text) of every call `name(` with balanced parentheses, across newlines."""
    found = []
    for m in re.finditer(r"(?<!function )\b(" + "|".join(names) + r")\(", code):
        depth, i = 1, m.end()
        while i < len(code) and depth:
            depth += {"(": 1, ")": -1}.get(code[i], 0)
            i += 1
        found.append((m.group(1), code[m.end() : i - 1]))
    return found


def _blocks(code: str, opener: str) -> list[str]:
    """Source of each `on('<event>' ...)` registration, by balanced parentheses (indent-independent)."""
    out = []
    for m in re.finditer(r"\bon\(\s*'" + re.escape(opener) + r"'", code):
        start, depth, i = m.start(), 1, m.end()
        while i < len(code) and depth:
            depth += {"(": 1, ")": -1}.get(code[i], 0)
            i += 1
        out.append(code[start:i])
    return out


def test_no_network_calls():
    src = _code(_sources())
    assert "$.http" not in src
    assert "fetch(" not in src
    for token in ("XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon"):
        assert token not in src
    assert not re.search(r"\b(curl|wget|nc)\b", src)
    assert not re.search(r"https?://", src), "no URL literal in code"


def test_prompt_compose_is_hooked_once_and_only_in_register():
    files = _files()
    assert len(re.findall(r"on\(\s*'prompt\.compose'", files["register.ts"])) == 1
    assert sum(len(re.findall(r"'prompt\.compose'", s)) for s in files.values()) == 1


def test_tool_call_hooks_never_deny_or_rewrite():
    hooks = [(n, b) for n, src in _files().items() for b in _blocks(_code(src), "tool.call")]
    assert len(hooks) == 2, [n for n, _ in hooks]  # observe.ts Bash, router.ts Workflow
    for name, body in hooks:
        assert "deny" not in body, name
        # the result is the engine's own: `r` is assigned only from `await next(e)` and returned as is,
        # or the hook returns `next(e)` directly
        assigns = re.findall(r"\b(?:const|let|var)\s+r\s*=\s*([^\n]+)|\br\s*=\s*([^\n=][^\n]*)", body)
        for a1, a2 in assigns:
            assert (a1 or a2).strip() == "await next(e)", (name, a1 or a2)
        returns = re.findall(r"\breturn\s+([^\n]+)", body)
        assert returns and all(x.strip() in ("r", "next(e)") for x in returns), (name, returns)
        if "return r" in body:
            assert assigns, name


def test_no_code_path_rewrites_the_spawn_model():
    files = _files()
    spawns = _blocks(_code(files["register.ts"]), "agent.spawn")
    assert len(spawns) == 1
    body = spawns[0]
    assert body.count("next(") == 1 and "next(e)" in body, "agent.spawn passes the event through untouched"
    for name, src in files.items():
        code = _code(src)
        for _, args in _calls(code, ("next",)):
            assert not re.search(r"\bmodel\b", args), (name, args)  # any object argument, nested or not
        assert not re.search(r"\be\.model\s*=(?!=)|\be\[\s*['\"]model['\"]\s*\]\s*=(?!=)", code), name
        assert "Object.assign(e" not in code and "structuredClone(e" not in code, name
        assert not re.search(r"\.\.\.e,\s*model\b", code), name
        assert not re.search(r"\benforce(d|ment)?\b", code, re.I), name
    # a handler for agent.spawn returns the engine's result object, never a built {model}
    assert not re.search(r"return\s*\{[^}]*\bmodel\s*:", body)


def test_privacy_writes_only_under_backend_dir_and_store():
    files = _files()
    for name, src in files.items():
        code = _code(src)
        if name not in ("host.ts", "register.ts"):
            # the only direct file write is ensureDir's marker, under the dir it is handed
            for m in re.finditer(r"host\.write\(\s*([^,]+),", code):
                assert m.group(1).strip() == "`${dir}/.keep`", (name, m.group(1))
        for m in re.finditer(r"appendLines\(\s*host,\s*([^,]+),", code):
            if name != "io.ts":
                assert re.search(r"observePath\(rt\.backendDir|routeLogPath\(rt\.backendDir", m.group(1)), (name, m.group(1))
        for m in re.finditer(r"ensureDir\(\s*host,\s*([^)]+)\)", code):
            if name != "io.ts":
                assert m.group(1).strip() == "dir", (name, m.group(1))
    obs = _code(files["observe.ts"])
    assert "const dir = observeDir(rt.backendDir)" in obs
    # processes: only tee/tail/rm helpers (appends, tails, retention) and the backend CLI reads
    argvs = set(re.findall(r"host\.run\(\[\s*'([^']+)'", _code(_sources())))
    assert argvs <= {"/usr/bin/tee", "/usr/bin/tail", "/bin/rm"}, argvs
    for name, src in files.items():
        assert not re.search(r"'/(etc|usr/local|var|tmp)\b|homedir|process\.env", _code(src)), name


def test_privacy_no_prompt_command_or_file_text_is_persisted():
    # rows and $.store values are built from labels and numbers; the raw prompt, Bash command, file
    # text or tool output never reaches a record(...) / storeSet(...) argument.
    banned = re.compile(
        r"\be\.(prompt|command|text|input|script|answer|content|output|stdout|stderr)\b"
        r"|\b(prompt|command|answer|content|output|stdout|stderr|text)\b(?!\s*:)"
    )
    matched = 0
    for name, src in _files().items():
        for fn, args in _calls(_code(src), ("record", "storeSet")):
            matched += 1
            bare = re.sub(r"'[^']*'|\"[^\"]*\"", "''", args)  # string literals are labels, not data
            assert not banned.search(bare), (name, fn, args)
    assert matched >= 12, matched


LISTINGS = (
    Path(__file__).resolve().parents[1] / "plugin" / ".claude-plugin" / "plugin.json",
    Path(__file__).resolve().parents[1] / ".claude-plugin" / "marketplace.json",
)


def test_listings_describe_advise_only_behaviour():
    """§10: the listing states behaviour accurately — v1 is advise-only and never enforces."""
    import json

    for p in LISTINGS:
        text = p.read_text()
        json.loads(text)
        assert not re.search(r"enforc", text, re.I), f"{p.name} promises enforcement"
        assert "advise-only" in text, f"{p.name} does not say advise-only"
