"""
Which code is this process running -- and is it behind the files beside it?

Both failures this exists for happened on one day. The MCP server ran the old
resolver and reported 837 clusters where fresh code said 852; the Docker stack
ran a three-day-old image whose scheduler reverted every fix. Each produced
numbers that looked exactly as authoritative as the right ones.
"""
from __future__ import annotations

from brahmastra import version


def test_a_fresh_process_is_current():
    s = version.status()
    assert s["fingerprint"] == s["on_disk"]
    assert s["stale"] is False
    assert "warning" not in s


def test_a_process_behind_its_files_says_so(monkeypatch):
    """The MCP-server case: loaded at startup, files changed since."""
    monkeypatch.setattr(version, "LOADED", "000000000000")
    s = version.status()
    assert s["stale"] is True
    assert "restart" in s["warning"]


def test_the_fingerprint_changes_when_the_code_does(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")
    monkeypatch.setattr(version, "PACKAGE", tmp_path)
    before = version.fingerprint_of_disk()
    (tmp_path / "a.py").write_text("x = 2\n")
    assert version.fingerprint_of_disk() != before


def test_line_endings_do_not_count_as_a_change(tmp_path, monkeypatch):
    """
    A Windows checkout and the Linux container built from it must agree on
    what "the same code" means, or every comparison between them would report
    a difference that is not one.
    """
    monkeypatch.setattr(version, "PACKAGE", tmp_path)
    (tmp_path / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    crlf = version.fingerprint_of_disk()
    (tmp_path / "a.py").write_bytes(b"x = 1\ny = 2\n")
    assert version.fingerprint_of_disk() == crlf


def test_a_renamed_file_is_a_change(tmp_path, monkeypatch):
    """Same bytes under another name is different code: imports resolve by name."""
    monkeypatch.setattr(version, "PACKAGE", tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    before = version.fingerprint_of_disk()
    (tmp_path / "a.py").rename(tmp_path / "b.py")
    assert version.fingerprint_of_disk() != before


def test_health_reports_the_code_without_touching_a_database():
    """Liveness must stay dependency-free; the fingerprint is a file hash."""
    import asyncio

    import main

    body = asyncio.run(main.health())
    assert body["status"] == "ok"
    assert body["code"]["fingerprint"] == version.LOADED


def test_a_server_that_predates_the_fingerprint_is_behind(monkeypatch, capsys):
    """No `code` in /health means older than this module -- behind by definition."""
    monkeypatch.setattr(version, "compare",
                        lambda url: {"here": "abc", "there": None, "behind": True})
    assert version.main(["--against", "http://example"]) == 1
    assert "predates" in capsys.readouterr().out


def test_a_matching_server_is_current(monkeypatch, capsys):
    monkeypatch.setattr(version, "compare",
                        lambda url: {"here": "abc", "there": "abc", "behind": False})
    assert version.main(["--against", "http://example"]) == 0
    assert "current" in capsys.readouterr().out
