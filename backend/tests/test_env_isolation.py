"""
backend/.env must not reach the test suite.

Three times now, tests have talked to live infrastructure through
configuration. The most recent route: ten modules each called load_dotenv at
import, and while dotenv leaves an already-set variable alone, it fills in one
that is absent — so DELETING a variable armed the leak instead of preventing
it, and any importlib.reload() pulled the developer's real config back in.

These tests pin the loader itself, so a future module that reintroduces a
direct load_dotenv is caught here rather than by a test that mysteriously talks
to production months later.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from brahmastra import env as env_mod

BACKEND = Path(__file__).resolve().parent.parent


def test_the_flag_stops_the_file_being_read(monkeypatch, tmp_path):
    envfile = tmp_path / ".env"
    envfile.write_text("LEAK_CANARY=from-dotenv\n", encoding="utf-8")

    monkeypatch.setenv("BRAHMASTRA_NO_DOTENV", "1")
    monkeypatch.delenv("LEAK_CANARY", raising=False)
    assert env_mod.load_env(envfile) is False
    assert os.environ.get("LEAK_CANARY") is None, "the file was read despite the flag"


def test_without_the_flag_it_does_load(monkeypatch, tmp_path):
    """The guard must not have broken the thing it guards."""
    envfile = tmp_path / ".env"
    envfile.write_text("LEAK_CANARY=from-dotenv\n", encoding="utf-8")

    monkeypatch.delenv("BRAHMASTRA_NO_DOTENV", raising=False)
    monkeypatch.delenv("LEAK_CANARY", raising=False)
    assert env_mod.load_env(envfile) is True
    assert os.environ["LEAK_CANARY"] == "from-dotenv"


def test_an_explicit_value_still_wins(monkeypatch, tmp_path):
    """A real environment must override the file, as it always did."""
    envfile = tmp_path / ".env"
    envfile.write_text("LEAK_CANARY=from-dotenv\n", encoding="utf-8")

    monkeypatch.delenv("BRAHMASTRA_NO_DOTENV", raising=False)
    monkeypatch.setenv("LEAK_CANARY", "from-environment")
    env_mod.load_env(envfile)
    assert os.environ["LEAK_CANARY"] == "from-environment"


def test_reloading_a_module_cannot_reintroduce_the_real_config():
    """
    The exact failure: reload re-ran a module-level load_dotenv, dotenv filled
    in the deleted NOTE_BACKEND, and the test went to the production Postgres.
    conftest sets the flag, so a reload now changes nothing.
    """
    import brahmastra.extraction  # noqa: F401
    import brahmastra.stores

    for module in ("brahmastra.extraction", "brahmastra.llm", "brahmastra.stores"):
        importlib.reload(importlib.import_module(module))

    assert brahmastra.stores.note_backend_name() == "", (
        "a module reload pulled NOTE_BACKEND back in from backend/.env"
    )
    assert brahmastra.stores.backend_name() == "sqlite"


def test_no_module_loads_dotenv_directly():
    """
    Every read must go through brahmastra.env, or the flag stops covering it.
    A new module calling load_dotenv itself is the way this comes back.
    """
    offenders = []
    for path in list((BACKEND / "brahmastra").rglob("*.py")) + [BACKEND / "main.py"]:
        if path.name == "env.py":
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "load_dotenv" in stripped:
                offenders.append(f"{path.relative_to(BACKEND)}: {stripped}")
    assert not offenders, (
        "these bypass brahmastra.env.load_env(), so BRAHMASTRA_NO_DOTENV does "
        f"not cover them:\n  " + "\n  ".join(offenders)
    )
