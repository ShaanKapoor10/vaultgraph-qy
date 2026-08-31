"""
Runtime state must land somewhere the process can actually write.

The pipeline lock and the keepalive's touch stamp both defaulted to a directory
beside the package. That is fine on a developer's machine and wrong in the
container, where the package lives under a root-owned /app and the process runs
as an unprivileged user.

The lock is taken before any work, so this was not a degraded run -- it was no
run at all. `POST /pipeline/run` returned
"[Errno 13] Permission denied: '/app/data'" instantly, and the scheduler
raised the same on every tick that found something to do. Both looked healthy
from outside: the container was up, the API answered, the scheduler logged
heartbeats.
"""
from __future__ import annotations

from pathlib import Path

from brahmastra import keepalive, pipeline
from brahmastra.env import data_dir


def test_the_lock_follows_the_data_dir(monkeypatch, tmp_path):
    """The claim that makes a containerised run possible at all."""
    monkeypatch.setenv("BRAHMASTRA_DATA_DIR", str(tmp_path))
    assert pipeline._lock_path().parent == tmp_path


def test_the_touch_stamp_follows_the_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DATA_DIR", str(tmp_path))

    class Fake:
        workspace = "default"
        def describe(self) -> str:
            return "fake:engine"

    assert keepalive._state_path(Fake()).parent == tmp_path


def test_the_lock_and_the_stamp_agree(monkeypatch, tmp_path):
    """
    They describe the same run. A container that wrote one to the volume and
    the other onto the image would be half-configured in a way nobody notices
    until the keepalive forgets a touch it actually made.
    """
    monkeypatch.setenv("BRAHMASTRA_DATA_DIR", str(tmp_path))

    class Fake:
        workspace = "default"
        def describe(self) -> str:
            return "fake:engine"

    assert pipeline._lock_path().parent == keepalive._state_path(Fake()).parent


def test_a_run_survives_an_unwritable_package_directory(monkeypatch, tmp_path):
    """
    Simulates the container: the package directory cannot be created in, and
    the run must still acquire its lock from the configured data dir.
    """
    monkeypatch.setenv("BRAHMASTRA_DATA_DIR", str(tmp_path / "volume"))

    real_mkdir = Path.mkdir

    def refuse_outside_the_volume(self, *args, **kwargs):
        if "volume" not in str(self):
            raise PermissionError(f"[Errno 13] Permission denied: '{self}'")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", refuse_outside_the_volume)

    assert pipeline._acquire_lock() is True
    pipeline._release_lock()


def test_the_default_is_still_beside_the_package(monkeypatch):
    """Unset means the local arrangement, which must not change."""
    monkeypatch.delenv("BRAHMASTRA_DATA_DIR", raising=False)
    assert data_dir().name == "data"
    assert data_dir().parent.name == "backend"


# ---------------------------------------------------------------------------
# Present-but-empty is not absent
# ---------------------------------------------------------------------------

def test_an_empty_model_variable_falls_back_to_the_default(monkeypatch):
    """
    docker compose writes `GROQ_MODEL: ${GROQ_MODEL:-}` so an operator can
    override it -- present, empty. os.environ.get(name, default) returns the
    default only when the variable is ABSENT, so the container asked Groq for a
    model named "" and got `404 The model `` does not exist`, which reads like
    a RETIRED model rather than an unset one.
    """
    from brahmastra import llm

    monkeypatch.setenv("GROQ_MODEL", "")
    assert llm.groq_model() == llm.GROQ_DEFAULT_MODEL

    monkeypatch.setenv("GROQ_MODEL", "   ")
    assert llm.groq_model() == llm.GROQ_DEFAULT_MODEL

    monkeypatch.setenv("GROQ_MODEL", "some/other-model")
    assert llm.groq_model() == "some/other-model"


def test_an_empty_anthropic_model_also_falls_back(monkeypatch):
    from brahmastra import llm

    monkeypatch.setenv("ANTHROPIC_MODEL", "")
    assert llm.anthropic_model() == llm.ANTHROPIC_DEFAULT_MODEL
