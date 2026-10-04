"""The committed TS parity fixtures are exactly what the Python oracle generates now."""
from apex_router.core import fixtures


def test_core_fixture_is_current():
    expected = fixtures.render_ts("CORE", fixtures.build_core())
    actual = (fixtures.FIXTURE_DIR / "core.ts").read_text()
    assert actual == expected, "stale: run `python -m apex_router.core.fixtures --write`"


def test_classify_fixture_is_current():
    expected = fixtures.render_ts("CLASSIFY", fixtures.build_classify())
    actual = (fixtures.FIXTURE_DIR / "classify.ts").read_text()
    assert actual == expected, "stale: run `python -m apex_router.core.fixtures --write`"
