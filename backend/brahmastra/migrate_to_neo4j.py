"""
Copy everything from the SQLite store into the Neo4j store.

    python -m brahmastra.migrate_to_neo4j            # dry run: counts only
    python -m brahmastra.migrate_to_neo4j --apply    # actually write
    python -m brahmastra.migrate_to_neo4j --apply --wipe   # clear target first

Reads through the GraphStore contract rather than raw SQL, so it stays correct
if either backend changes.

Re-runnable: the target ends up mirroring the source. Notes and mentions are
MERGEd, and each note's triples are deleted before re-insert, so a second run
neither duplicates facts nor leaves behind ones the source has since dropped.
--wipe additionally clears entities and the cached graph, for a cold rebuild.

The source is always SQLite and the target always Neo4j; GRAPH_BACKEND is
ignored here so running this cannot accidentally copy a store onto itself.
"""

from __future__ import annotations

import argparse
import sys

from brahmastra.stores.neo4j_store import Neo4jStore
from brahmastra.stores.sqlite_store import SQLiteStore


def migrate(apply: bool = False, wipe: bool = False) -> dict[str, int]:
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

    print("  notes ...")
    for n in notes:
        dst.upsert_note(
            id=n["id"],
            title=n["title"],
            content=n["content"],
            last_edited=n.get("last_edited"),
            # Preserve status verbatim: re-marking everything pending would
            # trigger a full re-extraction and a large LLM bill.
            mark_pending=(n.get("extraction_status") == "pending"),
        )
        if n.get("extraction_status") in ("done", "error"):
            dst.set_note_status(n["id"], n["extraction_status"])

    # Mirror, do not union. Without the delete this is additive: re-running it
    # once turned 633 triples in SQLite into 1028 in Neo4j — 312 duplicates
    # plus 83 facts from earlier extractions that SQLite had already dropped
    # when those notes were re-extracted. Deleting per note is what the
    # extraction path already does; it touches only triples, leaving notes,
    # entities and the cached graph intact.
    print("  triples (replacing per note) ...")
    for n in notes:
        dst.delete_triples_for_note(n["id"])
    dst.insert_triples(triples)

    print("  canonical map ...")
    dst.replace_canonical_map(clusters)

    if cached:
        print("  graph ...")
        dst.save_graph(cached["graph"], cached.get("stats") or {})

    print("\ntarget stats:", dst.stats())
    dst.close()
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="Migrate SQLite -> Neo4j")
    ap.add_argument("--apply", action="store_true", help="write (default is dry run)")
    ap.add_argument("--wipe", action="store_true", help="clear the target first")
    args = ap.parse_args()
    migrate(apply=args.apply, wipe=args.wipe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
