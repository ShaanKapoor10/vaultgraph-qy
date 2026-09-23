"""
Code symbols get a type of their own, assigned by SPELLING.

Found while working out why schema-enforced extraction did not beat JSON mode:
38 of 129 domain_range degradations had a function, constant or class as an
endpoint, and the ontology had no type for one. Replayed over all 94 cached
extractions, this rescued 20 triples and degraded none that were kept before.
"""
from __future__ import annotations

import pytest

from brahmastra.extraction import SYSTEM_PROMPT, _coerce_triple
from brahmastra.ontology import (ALL_ENTITY_TYPES, ENTITY_TYPES, code_symbol_type,
                                 is_valid_triple)


def _t(s, st, r, o, ot):
    return {"subject_text": s, "subject_type": st, "relation": r,
            "object_text": o, "object_type": ot, "confidence": 0.9,
            "source_quote": "q"}


@pytest.mark.parametrize("name", [
    "_groq_chat", "run_extraction", "BLOCKING_MIN_MENTIONS", "NOTION_TOKEN",
    "clear_derived()", "db.get_notes()", "brahmastra_add_note",
])
def test_identifiers_are_code_symbols_by_spelling(name):
    assert code_symbol_type(name, "concept") == "code_symbol"


@pytest.mark.parametrize("name,model_type", [
    ("in_progress", "status"),        # a snake_case STATUS is a status value
    ("ShaanKapoor10", "person"),
    ("vaultgraph_qy", "project"),
    ("Q3 release", "event"),
    ("knowledge graph", "concept"),   # plain words are never code
    ("extraction.py", "file"),        # files keep their own type
])
def test_spelling_never_overrides_what_a_thing_plainly_is(name, model_type):
    assert code_symbol_type(name, model_type) is None


@pytest.mark.parametrize("name,model_type", [
    ("CocoIndex", "project"), ("TypeScript", "tool"), ("PageRank", "concept"),
    ("MiniCheck", "tool"),
])
def test_camel_case_is_a_product_name_as_often_as_a_class(name, model_type):
    """Seen in the corpus; every one of these was a product or an algorithm."""
    assert code_symbol_type(name, model_type) is None


def test_camel_case_is_a_class_when_the_model_had_nothing_better():
    assert code_symbol_type("CompositeStore", "unknown") == "code_symbol"


@pytest.mark.parametrize("written", ["function", "module", "hook", "variable",
                                     "environment variable", "Function"])
def test_the_types_the_model_invents_for_code_are_understood(written):
    """In JSON mode the model steps outside the list to write these."""
    triple, _ = _coerce_triple(_t("the watcher loop", written, "provides",
                                  "liveness", "concept"))
    assert triple["subject_type"] == "code_symbol"


def test_the_corpus_example_is_no_longer_degraded():
    triple, reason = _coerce_triple(_t("_groq_chat", "feature", "integrates_with",
                                       "extraction.py", "file"))
    assert triple["relation"] == "integrates_with"
    assert reason is None


def test_a_symbol_can_implement_and_provide():
    assert is_valid_triple("code_symbol", "implements", "concept")
    assert is_valid_triple("code_symbol", "provides", "feature")
    assert is_valid_triple("tool", "provides", "code_symbol")


def test_located_in_is_still_not_widened():
    """Functional: NOTION_TOKEN lives in two .env files, and saying so would
    read as a contradiction."""
    assert not is_valid_triple("code_symbol", "located_in", "file")


def test_the_model_is_never_offered_the_type():
    """Kept out of the prompt: the prompt is part of every memo key, and
    offering it would invalidate every cached extraction."""
    assert "code_symbol" not in ENTITY_TYPES
    assert "code_symbol" not in SYSTEM_PROMPT
    assert "code_symbol" in ALL_ENTITY_TYPES
