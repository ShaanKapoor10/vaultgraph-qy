"""
Tests for the ontology module.
No DB or LLM calls — purely functional.
"""
from __future__ import annotations

import pytest
from brahmastra.ontology import is_valid_triple, is_functional, ENTITY_TYPES, RELATION_NAMES


def test_entity_types_non_empty():
    assert len(ENTITY_TYPES) > 0
    assert "person" in ENTITY_TYPES
    assert "concept" in ENTITY_TYPES


def test_relation_names_non_empty():
    assert len(RELATION_NAMES) > 0
    assert "related_to" in RELATION_NAMES


def test_valid_triples():
    assert is_valid_triple("person", "reports_to", "person")
    assert is_valid_triple("project", "depends_on", "concept")
    assert is_valid_triple("person", "owns", "project")
    assert is_valid_triple("concept", "related_to", "concept")
    assert is_valid_triple("person", "related_to", "tool")


def test_invalid_relation():
    assert not is_valid_triple("person", "nonexistent_relation", "person")


def test_domain_constraint_violated():
    # "owns" domain = ["person"] — project is not in domain
    assert not is_valid_triple("project", "owns", "project")


def test_range_constraint_violated():
    # "reports_to" range = ["person"] — concept is not in range
    assert not is_valid_triple("person", "reports_to", "concept")


def test_functional_relations():
    assert is_functional("reports_to")
    assert is_functional("scheduled_for")
    assert not is_functional("related_to")
    assert not is_functional("depends_on")


def test_unknown_relation_not_functional():
    assert not is_functional("nonexistent")


def test_wildcard_domain_range():
    # "part_of" has domain=["*"]
    assert is_valid_triple("person", "part_of", "project")
    assert is_valid_triple("tool", "part_of", "organisation")


def test_related_to_universal():
    # "related_to" has domain=["*"] range=["*"]
    for etype in ENTITY_TYPES:
        assert is_valid_triple(etype, "related_to", etype)


# -- domains, widened from evidence rather than anticipation -----------------
#
# The first evidence brahmastra.coercions collected did not say what
# ONTOLOGY_DESIGN.md expected. Zero `unmapped_relation` across 39 notes of the
# real corpus -- the model never leaves the listed vocabulary. The signal was
# `domain_range`: known relations refused because `file` and `feature` joined
# ENTITY_TYPES ten days after the domains were written, and nobody went back.
# Widening them took the related_to catch-all on those notes from 31.3% to
# 16.7%, and handed 59 triples their real relation back.

from brahmastra.ontology import is_valid_triple


def test_the_sentence_claude_md_tells_you_to_write_is_now_legal():
    """
    CLAUDE.md's own recommended note style: "The file extraction.py implements
    retry logic." It extracted as file -> concept, which `implements` refused,
    so the protocol's own example was degraded to `related_to`.
    """
    assert is_valid_triple("file", "implements", "concept")


def test_ontology_yaml_s_own_example_is_now_legal():
    """'entity_resolution.py implements Union-Find' -- the documented example
    for `implements`, which its own domain forbade."""
    assert is_valid_triple("file", "implements", "concept")


def test_a_file_contains_and_provides_things():
    assert is_valid_triple("file", "has_component", "feature")     # 10 notes
    assert is_valid_triple("file", "provides", "concept")          # 7 notes


def test_a_feature_contains_and_provides_things():
    assert is_valid_triple("feature", "provides", "feature")       # 6 notes
    assert is_valid_triple("feature", "has_component", "file")     # 4 notes


def test_concept_was_deliberately_not_admitted():
    """
    Three relations cleared the same evidence bar for `concept`, and it was
    refused on reading the sentences: "len(chunks) provides call count",
    "groq=True provides live Groq key". `concept` is where the model puts what
    it cannot type. Pinned so a later widening is a decision, not a drift.
    """
    assert not is_valid_triple("concept", "provides", "feature")
    assert not is_valid_triple("concept", "integrates_with", "tool")


def test_the_widening_did_not_touch_the_prompt():
    """
    The domains are not in SYSTEM_PROMPT, which is why widening them cost no
    model calls: the memo key holds, and coercion re-runs on cached replies.
    If this ever fails, an ontology change has started invalidating every
    cached extraction and the re-extraction bill needs budgeting.
    """
    from brahmastra.extraction import SYSTEM_PROMPT

    assert "domain" not in SYSTEM_PROMPT.lower()
    assert "project, concept, tool, organisation, file" not in SYSTEM_PROMPT
