"""`mesh units`: systemd --user unit files for `mesh up` and `mesh worker` (Linux nodes)."""
from __future__ import annotations

import pytest

from apex_router.mesh import cli, units


def test_up_unit_pins_interpreter_and_args():
    body = units.render_up(["--peer", "10.0.0.158", "--role", "data"], py="/home/u/venv/bin/python",
                           env={})
    assert "ExecStart=/home/u/venv/bin/python -m apex_router.mesh.cli up --peer 10.0.0.158 " \
           "--role data\n" in body
    assert "Restart=always" in body and "WantedBy=default.target" in body


def test_worker_unit_runs_worker_loop():
    body = units.render_worker(py="/p/python", env={})
    assert "ExecStart=/p/python -m apex_router.mesh.cli worker\n" in body


def test_env_carries_mesh_home_and_path_with_percent_escaped():
    body = units.render_up([], py="/p/python",
                           env={"APEX_MESH_HOME": "/srv/mesh", "PATH": "/usr/bin:/a%b", "HOME": "/x"})
    assert "Environment=APEX_MESH_HOME=/srv/mesh\n" in body
    assert "Environment=PATH=/usr/bin:/a%%b\n" in body             # % is a systemd specifier
    assert "HOME" not in body.replace("APEX_MESH_HOME", "")       # only the carried keys


@pytest.mark.parametrize("bad", ["a b", "x;y", "$HOME", "'q'", '"q"', "a\\b", "a\nb", "%n", "`id`"])
def test_unsafe_up_args_are_refused(bad):
    with pytest.raises(ValueError):
        units.render_up(["--role", bad], py="/p/python", env={})


def test_unsafe_env_value_is_refused():
    with pytest.raises(ValueError):
        units.render_up([], py="/p/python", env={"PATH": "/usr/bin\nExecStart=/bin/sh"})


def test_up_args_are_validated_by_the_up_parser():
    with pytest.raises(ValueError):
        units.check_up_args(["--no-such-flag"])
    units.check_up_args(["--peer", "10.0.0.158", "--serve-model", "qwen3.8", "--no-auto-update"])


def test_cli_writes_units_and_runs_no_systemctl(tmp_path, monkeypatch, capsys):
    import subprocess

    def no_run(*a, **kw):
        raise AssertionError("mesh units must not run any command")

    monkeypatch.setattr(subprocess, "run", no_run)
    rc = cli.main(["units", "--dir", str(tmp_path), "--worker", "--",
                   "--peer", "10.0.0.158", "--role", "data"])
    assert rc == 0
    up = (tmp_path / "apex-router-mesh.service").read_text()
    assert " up --peer 10.0.0.158 --role data\n" in up
    assert (tmp_path / "apex-router-mesh-worker.service").exists()
    out = capsys.readouterr().out
    assert "systemctl --user daemon-reload" in out
    assert "systemctl --user enable --now apex-router-mesh.service" in out
    assert "apex-router-mesh-worker.service" in out
    assert "loginctl enable-linger" in out


def test_cli_without_worker_writes_only_up(tmp_path):
    assert cli.main(["units", "--dir", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["apex-router-mesh.service"]


def test_cli_print_writes_nothing(tmp_path, capsys):
    assert cli.main(["units", "--dir", str(tmp_path), "--print", "--", "--role", "data"]) == 0
    assert list(tmp_path.iterdir()) == []
    assert "ExecStart=" in capsys.readouterr().out


def test_cli_bad_up_arg_exits_2_and_writes_nothing(tmp_path, capsys):
    assert cli.main(["units", "--dir", str(tmp_path), "--", "--bogus"]) == 2
    assert list(tmp_path.iterdir()) == []
    assert "mesh units:" in capsys.readouterr().err
