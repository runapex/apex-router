from pathlib import Path

from apex_router.telemetry_path import telemetry_path


def test_explicit_file_wins():
    assert telemetry_path({"APEX_TELEMETRY": "/x/t.jsonl", "APEX_HOME": "/y"}) == Path("/x/t.jsonl")


def test_apex_home_matches_proxy_writer():
    assert telemetry_path({"APEX_HOME": "/y"}) == Path("/y/telemetry.jsonl")


def test_default_is_dot_apex():
    assert telemetry_path({}) == Path.home() / ".apex" / "telemetry.jsonl"
