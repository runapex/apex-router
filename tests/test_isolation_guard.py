"""Unit tests for the conftest isolation guard's ~/.claude watch list (fake home only)."""
from conftest import _claude_changed, _claude_watch_files, _sha


def _tree(root):
    for rel, txt in {
        "settings.json": "{}", "plugins/installed_plugins.json": "{}",
        "plugins/x/.in_use/123": "a", "plugins/.last_inuse_sweep": "t",
        "plugins/data/store/datapce_1.json": "{}", "projects/t.jsonl": "x",
    }.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(txt)


def test_watch_list_excludes_churn(tmp_path):
    _tree(tmp_path)
    got = {p.relative_to(tmp_path).as_posix() for p in _claude_watch_files(tmp_path)}
    assert got == {"settings.json", "plugins/installed_plugins.json"}


def test_config_change_fails_churn_passes(tmp_path):
    _tree(tmp_path)
    snap = {p: _sha(p) for p in _claude_watch_files(tmp_path)}
    (tmp_path / "plugins/x/.in_use/123").write_text("b")
    (tmp_path / "plugins/x/.in_use/999").write_text("new")
    (tmp_path / "plugins/.last_inuse_sweep").write_text("u")
    (tmp_path / "plugins/data/store/datapce_1.json").write_text('{"a":1}')
    assert _claude_changed(snap, tmp_path) == []
    (tmp_path / "settings.json").write_text('{"k":1}')
    assert _claude_changed(snap, tmp_path) == [tmp_path / "settings.json"]
    (tmp_path / "plugins/installed_plugins.json").unlink()
    assert len(_claude_changed(snap, tmp_path)) == 2
