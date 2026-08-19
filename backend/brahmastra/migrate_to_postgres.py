"""
Move the system of record from SQLite into Postgres.

    python -m brahmastra.migrate_to_postgres            # dry run: counts only
    python -m brahmastra.migrate_to_postgres --apply    # actually write
    python -m brahmastra.migrate_to_postgres --apply --workspace office

Only notes and workspaces move, because only they are the system of record.
Triples, the canonical map, clusters and the cached graph stay where the engine
keeps them -- they are a function of the notes, and copying them here would
create a second copy of data that is meant to have exactly one home.

Every note is verified present in the target before the run reports success. A
short copy raises rather than returning, because the whole reason to separate
the system of record is that losing a row of it loses information.

Re-runnable. Notes upsert by (workspace, id), and the upsert preserves the
target's publish flag and origin, so a second run cannot relabel provenance or
silently unpublish something.

Embeddings are recomputed on write rather than copied: SQLite never had any,
and a note that arrives without one is invisible to the semantic half of
hybrid search while still looking perfectly healthy in a row count.
"""

from __future__ import annotations

import argparse
import sys

from brahmastra.stores.postgres_store import PostgresStore
from brahmastra.stores.sqlite_store import SQLiteStore


class MigrationIncomplete(RuntimeError):
    """The system of record did not arrive intact."""


def migrate(apply: bool = False, workspace: str = "default") -> dict[str, int]:
    src = SQLiteStore(workspace=workspace)
    notes = src.get_notes()

    counts = {"notes": len(notes)}
    print(f"source: {src.describe()}")
    print(f"  notes  {len(notes)}")

    if not apply:
        print("\nDry run. Re-run with --apply to write.")
        return counts

    dst = PostgresStore(workspace=workspace)
    print(f"target: {dst.describe()}")
    dst.init_schema()

    if not dst.has_vector():
        # Not fatal -- the notes still move -- but it means the semantic half of
        # hybrid search will be empty, and the composite will refuse this store
        # afterwards. Better said now than discovered when search gets worse.
        print("  WARNING: pgvector is not installed on this server.")
        print("           Notes will migrate, but semantic search will be dead")
        print("           and CompositeStore will refuse this as a note store.")
        print("           Use the pgvector/pgvector image (docker compose does).")

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
            publish=bool(n.get("publish")) or None,
            source=n.get("source") or "unknown",
        )
        status = n.get("extraction_status")
        if status in ("done", "error"):
            dst.set_note_status(n["id"], status, n.get("extraction_error"))
        if n.get("notion_page_id"):
            dst.set_notion_page_id(n["id"], n["notion_page_id"])

    landed = {row["id"] for row in dst.get_notes()}
    missing = [n["id"] for n in notes if n["id"] not in landed]
    counts["notes_landed"] = len(landed)
    if missing:
        dst.close()
        raise MigrationIncomplete(
            f"{len(missing)} of {len(notes)} notes did not reach "
            f"{dst.describe()}: {missing[:5]}{' ...' if len(missing) > 5 else ''}. "
            f"The source is untouched; re-run --apply, the copy is idempotent."
        )

    print(f"        {len(landed)} notes verified in target")
    print("\ntarget stats:", dst.stats())
    print(f"capabilities: {sorted(dst.capabilities())}")
    dst.close()
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="Migrate the system of record: SQLite -> Postgres")
    ap.add_argument("--apply", action="store_true", help="write (default is dry run)")
    ap.add_argument("--workspace", default="default", help="workspace to move")
    args = ap.parse_args()
    try:
        migrate(apply=args.apply, workspace=args.workspace)
    except MigrationIncomplete as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
