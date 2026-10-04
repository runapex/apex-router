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
    for name, src in _files().items():
        for m in re.finditer(r"on\(\s*'tool\.call'.*?\n  \}\)", _code(src), re.S):
            body = m.group(0)
            assert "deny" not in body, name
            assert re.search(r"return next\(e\)|return r\b", body), name
    observe = (HOOKS / "observe.ts").read_text()
    assert "deny:" not in observe and "{ deny" not in observe


def test_no_code_path_rewrites_the_spawn_model():
    files = _files()
    spawn = re.search(r"on\('agent\.spawn'.*?\n  \}\)", _code(files["register.ts"]), re.S)
    assert spawn is not None
    body = spawn.group(0)
    assert body.count("next(") == 1 and "next(e)" in body, "agent.spawn passes the event through untouched"
    for name, src in files.items():
        code = _code(src)
        assert not re.search(r"next\(\s*\{[^}]*\bmodel\b", code), name
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
    banned = re.compile(r"\be\.prompt\b|\be\.command\b|\be\.text\b|\be\.input\b|\be\.script\b|\be\.answer\b|\.stdout\b")
    for name, src in _files().items():
        code = _code(src)
        for m in re.finditer(r"\b(record|storeSet)\(([^\n]*)\)", code):
            assert not banned.search(m.group(2)), (name, m.group(0))
