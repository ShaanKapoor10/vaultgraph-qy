"""
Move the system of record into Neo4j, and seed its cache.

    python -m brahmastra.migrate_to_neo4j                  # dry run: counts only
    python -m brahmastra.migrate_to_neo4j --apply          # move notes, copy cache
    python -m brahmastra.migrate_to_neo4j --apply --rebuild  # move notes, recompute
    python -m brahmastra.migrate_to_neo4j --apply --wipe   # clear target first

Two phases, deliberately unequal, because what they risk is unequal
(see SOURCE_DATA / DERIVED_DATA in stores/base.py):

  1. NOTES AND WORKSPACES -- the system of record. Cannot be recomputed, so
     this phase verifies what it wrote and refuses to report success on a
     short count. A note that does not arrive is lost information.

  2. TRIPLES, CANONICAL MAP AND THE CACHED GRAPH -- a cache. Every row is a
     function of the notes. Losing it costs LLM calls and time, never
     information, so this phase is allowed to fail loudly and be re-run.

By default phase 2 COPIES the existing cache rather than recomputing it: a
rebuild re-extracts every note through the LLM, which on the free tier is both
slow and rate-limited. --rebuild opts into recomputation, which is the honest
choice when the source cache is stale or you have changed the ontology.

Re-runnable either way. Notes and mentions are MERGEd, and each note's triples
are deleted before re-insert, so a second run neither duplicates facts nor
leaves behind ones the source has since dropped. --wipe additionally clears
entities and the cached graph, for a cold rebuild.

The source is always SQLite and the target always Neo4j; GRAPH_BACKEND is
ignored here so running this cannot accidentally copy a store onto itself.
"""

from __future__ import annotations

import argparse
import sys

from brahmastra.stores.neo4j_store import Neo4jStore
from brahmastra.stores.sqlite_store import SQLiteStore


def _rebuild_cache_in_target() -> dict:
    """
    Recompute the derived cache inside Neo4j, from the notes now living there.

    Repoints the process at the target and runs the full pipeline. The store is
    cached per (backend, workspace), so the reset is what makes the switch take
    effect -- without it the rebuild would quietly recompute SQLite's cache and
    report success.

    Restores the previous backend afterwards, so a caller that imported this
    module is not left pointing somewhere it never asked for.
    """
    import os

    from brahmastra import db

    previous = os.environ.get("GRAPH_BACKEND")
    os.environ["GRAPH_BACKEND"] = "neo4j"
    db.reset_store()
    try:
        from brahmastra.pipeline import run_pipeline
        return run_pipeline(full=True)
    finally:
        if previous is None:
            os.environ.pop("GRAPH_BACKEND", None)
        else:
            os.environ["GRAPH_BACKEND"] = previous
        db.reset_store()


class MigrationIncomplete(RuntimeError):
    """
    Raised when the system of record did not arrive intact.

    Only phase 1 raises this. A short cache is a re-run; a short set of notes
    is missing information, and reporting success on that is how a migration
    silently becomes data loss.
    """


def migrate(apply: bool = False, wipe: bool = False,
            rebuild: bool = False) -> dict[str, int]:
    src = SQLiteStore()
    notes = src.get_notes()
    triples = src.get_all_triples()
    clusters = src.get_entity_clusters()
    cached = src.load_graph()

    counts = {
        "notes": len(notes),
        "triples": len(triples),
        "clusters": len(clusters),
        "graph_nodes": len(((cached or {}).get("graph") or {}).get("nodes") or []),
        "graph_edges": len(((cached or {}).get("graph") or {}).get("edges") or []),
    }

    print(f"source: {src.describe()}")
    for k, v in counts.items():
        print(f"  {k:12} {v}")

    if not apply:
        print("\nDry run. Re-run with --apply to write.")
        return counts

    dst = Neo4jStore()
    print(f"target: {dst.describe()}")
    dst.init_schema()

    if wipe:
        print("  wiping target ...")
        # Batched so a large graph cannot exhaust the transaction.
        while True:
            done = dst._run(
                "MATCH (n) WITH n LIMIT 5000 DETACH DELETE n RETURN count(n) AS c"
            )[0]["c"]
            if not done:
                break

    # ---- phase 1: the system of record --------------------------------
    print("  [1/2] notes and workspaces (system of record) ...")
    for n in notes:
        dst.upsert_note(
            id=n["id"],
            title=n["title"],
            content=n["content"],
            last_edited=n.get("last_edited"),
            # Preserve status verbatim: re-marking everything pending would
            # trigger a full re-extraction and a large LLM bill.
            mark_pending=(n.get("extraction_status") == "pending"),
            # Carry the origin across rather than relabelling everything
            # 'migration' — the point of the column is where a note came FROM.
            source=n.get("source") or "unknown",
        )
        if n.get("extraction_status") in ("done", "error"):
            dst.set_note_status(n["id"], n["extraction_status"])

    # Verify before touching the cache. If notes did not arrive, a rebuilt
    # graph would be a confident graph of the wrong corpus.
    landed = len(dst.get_notes())
    counts["notes_landed"] = landed
    if landed < len(notes):
        dst.close()
        raise MigrationIncomplete(
            f"system of record incomplete: {landed} of {len(notes)} notes reached "
            f"{dst.describe()}. Nothing derived was written. Re-run --apply; the "
            "note copy is idempotent."
        )
    print(f"        {landed} notes verified in target")

    # ---- phase 2: the rebuildable cache -------------------------------
    if rebuild:
        # Recompute rather than copy. Correct when the source cache is stale or
        # the ontology changed, and expensive: extraction re-runs the LLM over
        # every note, which the free tier rate-limits.
        print("  [2/2] rebuilding cache from notes (re-extracts every note) ...")
        result = _rebuild_cache_in_target()
        counts["rebuild_status"] = result.get("status")
        print(f"        rebuild: {result.get('status')} "
              f"failed={result.get('failed_stages')}")
    else:
        # Mirror, do not union. Without the delete this is additive: re-running
        # it once turned 633 triples in SQLite into 1028 in Neo4j - 312
        # duplicates plus 83 facts from earlier extractions that SQLite had
        # already dropped when those notes were re-extracted. Deleting per note
        # is what the extraction path already does; it touches only triples,
        # leaving notes, entities and the cached graph intact.
        print("  [2/2] copying cache (triples, replacing per note) ...")
        for n in notes:
            dst.delete_triples_for_note(n["id"])
        dst.insert_triples(triples)

        print("        canonical map ...")
        dst.replace_canonical_map(clusters)

        if cached:
            print("        graph ...")
            dst.save_graph(cached["graph"], cached.get("stats") or {})

    print("\ntarget stats:", dst.stats())
    dst.close()
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="Migrate SQLite -> Neo4j")
    ap.add_argument("--apply", action="store_true", help="write (default is dry run)")
    ap.add_argument("--wipe", action="store_true", help="clear the target first")
    ap.add_argument("--rebuild", action="store_true",
                    help="recompute the cache from notes instead of copying it "
                         "(re-extracts every note through the LLM)")
    args = ap.parse_args()
    try:
        migrate(apply=args.apply, wipe=args.wipe, rebuild=args.rebuild)
    except MigrationIncomplete as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
