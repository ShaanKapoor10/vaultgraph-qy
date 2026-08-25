"""
Throwaway code must not be able to reach real infrastructure.

The test suite has three defences against that. Nothing else had any: a one-off
`python -c` reads backend/.env like every other process, resolves the deployed
arrangement, and talks to the real Postgres and the real Aura. That is not
hypothetical — two debug probes wrote notes into the production graph and had
to be deleted afterwards.

The fix has to be a runner rather than a reminder, because the trap is import
ORDER: `brahmastra.stores` loads .env at import and answers "which store?" for
the whole process, so a helper called after that import is already too late.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from brahmastra import scratch

BACKEND = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# What the environment says
# ---------------------------------------------------------------------------

def test_dotenv_is_switched_off_entirely():
    """
    The one switch that covers variables nobody has added yet. Naming four is
    a list that rots; this is what makes the guarantee hold as config grows.
    """
    assert scratch.scratch_env()["BRAHMASTRA_NO_DOTENV"] == "1"


def test_the_networked_backends_are_set_empty_not_deleted():
    """
    Deleting ARMS the leak rather than disarming it: python-dotenv leaves a
    variable that is PRESENT alone but fills in one that is ABSENT. That exact
    inversion sent the test suite to the production Postgres.
    """
    env = scratch.scratch_env()
    for name in ("NOTE_BACKEND", "POSTGRES_HOST", "DATABASE_URL"):
        assert name in env, f"{name} must be present-but-empty, not absent"
        assert env[name] == ""


def test_notion_is_disarmed():
    """
    The outward-facing half. A probe that happens to run the pipeline reaches
    the write-back stage, which edits real Notion pages.
    """
    env = scratch.scratch_env()
    assert env["NOTION_TOKEN"] == ""
    assert env["NOTION_DATABASE_ID"] == ""


def test_it_does_not_vouch_for_the_real_engine():
    """
    A scratch run recording a keepalive touch would tell the keepalive that
    Aura was contacted when it was not — a lie the keepalive then believes for
    twelve hours.
    """
    assert scratch.scratch_env()["GRAPH_KEEPALIVE"] == "0"


def test_the_scratch_database_is_not_the_real_one():
    env = scratch.scratch_env()
    assert env["GRAPH_BACKEND"] == "sqlite"
    assert Path(env["BRAHMASTRA_DB"]).resolve() != scratch.PRODUCTION_DB.resolve()


def test_naming_the_production_database_is_refused():
    """--db is a convenience, not a way round the point of the module."""
    with pytest.raises(scratch.UnsafeScratch):
        scratch.scratch_env(scratch.PRODUCTION_DB)


def test_two_runs_do_not_share_a_database():
    """A throwaway database that persists between probes is not throwaway."""
    assert scratch.scratch_env("a.db")["BRAHMASTRA_DB"] != \
        scratch.scratch_env("b.db")["BRAHMASTRA_DB"]


# ---------------------------------------------------------------------------
# What actually happens when it runs
# ---------------------------------------------------------------------------

def _run(*args: str) -> subprocess.CompletedProcess:
    """
    A real subprocess on purpose. The claim under test is about what a FRESH
    interpreter resolves, and this test process has already imported half the
    package — so anything done in-process would prove the wrong thing.
    """
    return subprocess.run(
        [sys.executable, "-m", "brahmastra.scratch", *args],
        cwd=BACKEND, capture_output=True, text=True, timeout=120,
    )


def test_a_probe_resolves_a_scratch_store_even_with_a_deployed_dotenv(tmp_path):
    """
    The end-to-end claim: whatever backend/.env says, this points at sqlite in
    a temp file. Run without the suite's own isolation, since a guard that only
    works inside pytest is not a guard.
    """
    proc = _run("--db", str(tmp_path / "probe.db"),
                "-c", "from brahmastra import db; print(db.describe())")
    assert proc.returncode == 0, proc.stderr
    assert "sqlite" in proc.stdout
    assert "neo4j" not in proc.stdout and "postgres" not in proc.stdout
    assert str(tmp_path) in proc.stdout


def test_it_says_where_it_pointed(tmp_path):
    """A probe that silently pointed somewhere safe teaches nobody where it was
    pointing on the day it does not."""
    proc = _run("--db", str(tmp_path / "probe.db"), "-c", "pass")
    assert "[scratch]" in proc.stderr


def test_the_flags_of_the_thing_being_run_are_passed_through(tmp_path):
    """`-m brahmastra.keepalive --status` must not fail on an unrecognised --status."""
    proc = _run("--db", str(tmp_path / "probe.db"), "-m", "brahmastra.keepalive", "--status")
    assert proc.returncode == 0, proc.stderr
    assert "backend      sqlite" in proc.stdout


def test_exactly_one_thing_to_run_is_required():
    proc = _run()
    assert proc.returncode != 0
    assert "exactly one" in proc.stderr
