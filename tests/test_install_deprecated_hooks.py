"""install.sh 0.4.0: the two hook flags still wire their (deprecated) hook and say so; the default
marketplace is this install and the default plugin is datapce."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = (ROOT / "install.sh").read_text()
STUBS = ('warn(){ printf "WARN %s\\n" "$*"; }\nok(){ printf "OK %s\\n" "$*"; }\nsay(){ printf "SAY %s\\n" "$*"; }\n'
         '_wire_stop_hook(){ printf "WIRED %s\\n" "$1"; }\n_wire_hook(){ printf "WIRED %s\\n" "$3"; }\n')


def _fn(name: str) -> str:
    start = INSTALL.index(f"\n{name}() {{") + 1
    return INSTALL[start:INSTALL.index("\n}\n", start) + 3]


def _run(script: str, home: Path) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=10)


def test_install_sh_parses():
    assert subprocess.run(["bash", "-n", str(ROOT / "install.sh")]).returncode == 0


def test_deprecated_flags_still_wire_and_warn(tmp_path):
    for fn, flag, script in (("install_cache_handoff_hook", "DO_CACHE_HANDOFF", "cache-handoff-nudge.sh"),
                             ("install_agent_route_log_hook", "DO_AGENT_ROUTE_LOG", "agent-route-log.sh")):
        on = _run(STUBS + _fn(fn) + f"\nINSTALL_DIR=/opt/apex\n{flag}=1\n{fn}\n", tmp_path)
        assert on.returncode == 0, on.stderr
        assert f"WIRED /opt/apex/hooks/{script}" in on.stdout, on.stdout
        warns = [l for l in on.stdout.splitlines() if l.startswith("WARN ")]
        assert len(warns) == 1 and "deprecated" in warns[0] and "0.4.2" in warns[0] and "datapce" in warns[0], warns
        off = _run(STUBS + _fn(fn) + f"\nINSTALL_DIR=/opt/apex\n{flag}=0\n{fn}\n", tmp_path)
        assert "WIRED" not in off.stdout and "WARN" not in off.stdout, off.stdout


def test_default_marketplace_is_this_install_and_the_plugin_is_datapce(tmp_path):
    assert 'APEX_PUBLIC_MARKETPLACE=""' in INSTALL
    assert 'APEX_DEFAULT_PLUGIN="datapce@datapce"' in INSTALL
    script = (STUBS + "have(){ return 1; }\n" + _fn("_marketplace_add") + _fn("install_skills_marketplaces")
              + '\nDO_SKILLS=1; INSTALL_DIR=/opt/apex; APEX_PUBLIC_MARKETPLACE=""; '
                'APEX_DEFAULT_PLUGIN="datapce@datapce"; SKILLS_MARKETPLACES=""\ninstall_skills_marketplaces\n')
    out = _run(script, tmp_path).stdout
    assert "/plugin marketplace add /opt/apex" in out
    assert "/plugin install datapce@datapce" in out
