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
