"""
The source/derived split is only useful if it stays true as the contract grows.

A new method added to GraphStore without being classified is exactly how this
distinction rots into a comment nobody trusts.
"""
from __future__ import annotations

import inspect

from brahmastra.stores.base import (
    DERIVED_DATA,
    SOURCE_DATA,
    SOURCE_METHODS,
    GraphStore,
)

# Methods that are neither source nor derived: lifecycle and introspection.
# `capabilities` reports what a backend can do, not what it holds, so it has no
# data to lose either way.
INFRA = {"init_schema", "describe", "stats", "close", "capabilities"}

# Everything that reads or writes the rebuildable cache. Listed explicitly so
# adding a contract method forces a decision here rather than defaulting into
# whichever set happens to be checked first.
DERIVED_METHODS = {
    "insert_triples", "get_all_triples", "delete_triples_for_note",
    "replace_canonical_map", "get_canonical_map", "get_entity_clusters",
    "save_graph", "load_graph", "get_entities", "search_entities",
    "find_path", "neighbourhood",
}


def _contract_methods() -> set[str]:
    return {
        name for name, _ in inspect.getmembers(GraphStore, inspect.isfunction)
        if not name.startswith("_")
    }


def test_every_contract_method_is_classified():
    """
    A method that is neither source nor derived has no defined blast radius:
    a composite store cannot route it, and nobody can say whether losing its
    data costs time or costs information.
    """
    unclassified = _contract_methods() - SOURCE_METHODS - DERIVED_METHODS - INFRA
    assert not unclassified, (
        f"new GraphStore method(s) {sorted(unclassified)} are unclassified — "
        "add each to SOURCE_METHODS in stores/base.py (cannot be recomputed) "
        "or to DERIVED_METHODS in this test (rebuildable from notes)"
    )


def test_the_classification_names_real_methods():
    """A rename would otherwise leave a stale name routing nothing."""
    contract = _contract_methods()
    for name in sorted(SOURCE_METHODS):
        assert name in contract, f"SOURCE_METHODS names {name!r}, not on GraphStore"
    for name in sorted(DERIVED_METHODS):
        assert name in contract, f"DERIVED_METHODS names {name!r}, not on GraphStore"


def test_source_and_derived_do_not_overlap():
    """A method cannot be both authoritative and disposable."""
    assert not (SOURCE_METHODS & DERIVED_METHODS)
    assert not (set(SOURCE_DATA) & set(DERIVED_DATA))
