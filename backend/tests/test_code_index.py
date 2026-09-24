"""
Source code as a searchable part of the brain (ROADMAP item 4).

Chunks follow the language's own structure and keep their line numbers; a
chunk's identity is its content, so code that only moved is re-lined, not
re-embedded; only git-tracked files are read, and what is read is redacted.
"""
from __future__ import annotations

import pytest

from brahmastra import code_index as ci
from brahmastra import hybrid

PY = '''"""Module doc."""
import os


def parse_wait(message):
    """How long Groq asked us to wait."""
    return 7.5


class GroqKeyPool:
    def call(self, fn):
        return fn()
'''


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "code.db"))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("NOTE_BACKEND", "")
    import numpy as np
    from brahmastra import embeddings

    def fake(texts):
        out = []
        for t in texts:
            v = np.zeros(64, dtype="float32")
            for w in hybrid.tokens(t):
                v[hash(w) % 64] += 1
            out.append(v.tolist())
        return out

    monkeypatch.setattr(embeddings, "embed", fake)
    monkeypatch.setattr(embeddings, "embed_one", lambda t: fake([t])[0])
    return ci.CodeIndex(workspace="default")


# -- cutting -----------------------------------------------------------------

def test_python_is_cut_at_definitions_with_exact_lines(monkeypatch):
    monkeypatch.setattr(ci, "MERGE_LINES", 0)          # see the raw cut
    chunks = ci.chunk_file("backend/brahmastra/groq_pool.py", PY)
    by_symbol = {tuple(c.symbols): (c.start_line, c.end_line) for c in chunks}
    assert by_symbol[("parse_wait",)] == (5, 7)
    assert by_symbol[("GroqKeyPool",)] == (10, 12)


def test_small_neighbours_are_merged_keeping_every_name():
    chunks = ci.chunk_file("backend/brahmastra/groq_pool.py", PY)
    assert len(chunks) == 1
    assert {"parse_wait", "GroqKeyPool"} <= set(chunks[0].symbols)


def test_a_large_class_is_cut_per_method(monkeypatch):
    monkeypatch.setattr(ci, "MERGE_LINES", 0)
    body = "\n".join(f"    def m{i}(self):\n" + "        x = 1\n" * 12 for i in range(6))
    chunks = ci.chunk_file("a.py", f"class Big:\n{body}")
    assert "Big.m3" in {s for c in chunks for s in c.symbols}


def test_typescript_is_cut_at_top_level_declarations(monkeypatch):
    monkeypatch.setattr(ci, "MERGE_LINES", 0)
    src = "import x from 'y'\n\nexport function GraphView() {\n  return 1\n}\n\nexport const ONTOLOGY = {}\n"
    names = [c.symbols for c in ci.chunk_file("frontend/lib/graph.tsx", src)]
    assert ["GraphView"] in names and ["ONTOLOGY"] in names


def test_markdown_is_cut_at_headings(monkeypatch):
    monkeypatch.setattr(ci, "MERGE_LINES", 0)
    chunks = ci.chunk_file("docs/X.md", "# Title\nintro\n\n## Storage\nnotes live in postgres\n")
    assert [c.symbols for c in chunks] == [["Title"], ["Storage"]]


def test_an_overlong_unit_is_windowed_and_keeps_line_numbers():
    src = "def f():\n" + "".join(f"    x{i} = {i}\n" for i in range(150))
    chunks = ci.chunk_file("a.py", src)
    assert len(chunks) > 1
    assert chunks[0].start_line == 1
    assert all(b.start_line == a.end_line + 1 for a, b in zip(chunks, chunks[1:]))


def test_a_key_in_a_tracked_file_is_redacted():
    chunks = ci.chunk_file("a.py", 'KEY = "gsk_' + "A1b2" * 12 + '"\n')
    assert "A1b2A1b2" not in chunks[0].text


# -- the index ---------------------------------------------------------------

def test_code_below_an_edit_is_relined_not_re_embedded(store, monkeypatch):
    monkeypatch.setattr(ci, "MERGE_LINES", 0)
    first = ci.index_repo(store=store, files={"a.py": PY})
    assert first["embedded"] == first["chunks"]

    edited = PY.replace("    return 7.5\n", "    x = 1\n    y = 2\n    return 7.5\n")
    report = ci.index_repo(store=store, files={"a.py": edited})
    assert report["embedded"] == 1            # parse_wait changed
    assert report["relined"] >= 1             # GroqKeyPool only moved
    pool = next(r for r in store.all() if "GroqKeyPool" in r["symbols"])
    assert (pool["start_line"], pool["end_line"]) == (12, 14)


def test_unchanged_code_costs_nothing(store):
    ci.index_repo(store=store, files={"a.py": PY})
    again = ci.index_repo(store=store, files={"a.py": PY})
    assert again == {"files": 1, "chunks": again["chunks"], "embedded": 0,
                     "relined": 0, "removed": 0}


def test_a_deleted_file_leaves_the_index(store):
    ci.index_repo(store=store, files={"a.py": PY, "b.py": "def g():\n    return 2\n"})
    report = ci.index_repo(store=store, files={"a.py": PY})
    assert report["removed"] == 1
    assert {r["path"] for r in store.all()} == {"a.py"}


def test_only_git_tracked_files_are_read(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "kept.py").write_text("x = 1\n")
    (tmp_path / ".env").write_text("GROQ_API_KEY=secret\n")
    (tmp_path / "untracked.py").write_text("y = 2\n")
    subprocess.run(["git", "add", "kept.py", ".env"], cwd=tmp_path, check=True)
    files = ci.tracked_files(tmp_path)
    assert files == ["kept.py"]          # .env is tracked here, but not code


# -- search ------------------------------------------------------------------

def test_search_defaults_to_source_not_the_test_that_describes_it(store):
    ci.index_repo(store=store, files={
        "backend/brahmastra/groq_pool.py": PY,
        "backend/tests/test_groq_pool.py": "def test_parse_wait():\n    assert parse_wait('wait 7.5s') == 7.5\n",
    })
    paths = {h["path"] for h in ci.search("parse_wait", store=store)}
    assert paths == {"backend/brahmastra/groq_pool.py"}
    assert "backend/tests/test_groq_pool.py" in {
        h["path"] for h in ci.search("parse_wait", store=store, scope="all")}


def test_where_defined_reports_one_range_for_a_windowed_function(store):
    src = "def long_one():\n" + "".join(f"    x{i} = {i}\n" for i in range(150))
    ci.index_repo(store=store, files={"backend/brahmastra/llm.py": src})
    assert ci.where_defined(["long_one"], store=store) == ["backend/brahmastra/llm.py:1-151"]


def test_where_defined_ignores_tests(store):
    ci.index_repo(store=store, files={"backend/tests/test_x.py": "def helper():\n    pass\n"})
    assert ci.where_defined(["helper"], store=store) == []


@pytest.mark.parametrize("path,kind", [
    ("backend/brahmastra/llm.py", "source"), ("backend/tests/test_llm.py", "tests"),
    ("frontend/lib/graph.test.ts", "tests"), ("docs/ROADMAP.md", "docs"),
    ("frontend/components/graph-view.tsx", "source"),
])
def test_kind_of(path, kind):
    assert ci.kind_of(path) == kind
