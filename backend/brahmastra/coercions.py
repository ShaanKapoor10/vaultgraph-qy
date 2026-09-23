"""
What the ontology could not hold -- kept, so the vocabulary can grow from it.

`docs/ONTOLOGY_DESIGN.md` sets one rule for adding a relation:

    Do not add relations in anticipation. Add a relation when it keeps
    appearing as `unmapped_relation:` -- that is data telling you.

That rule had no data. `extract_note()` computed a coercion for every fact the
ontology had to bend, returned the list, and `run_extraction` never read it --
so on every run, for every note, the evidence the rule depends on was built and
thrown away. The vocabulary could only ever grow by anticipation, which is the
one way the design doc says it must not.

WHAT IS RECORDED. One row per coerced fact, carrying enough to judge it:

    kind              unmapped_relation   the model said a relation we lack
                      alias               it said one we know by another name
                      domain_range        right relation, wrong argument types
                      (and the four drops: missing_fields, empty_endpoint,
                       placeholder_entity, low_confidence)
    raw_relation      exactly what the model wrote -- "works closely with"
    stored_relation   what the graph holds instead -- usually related_to
    subject/object    the types, and the text as an example

The example text matters. "decided" appearing forty times is a signal;
"decided" appearing forty times between a person and a date is a relation
with a domain and a range, and that is what an ontology entry needs.

LIFECYCLE. Exactly the note's triples' lifecycle, deliberately. Extraction
deletes a note's triples and re-inserts them, and these rows are produced by
the same call from the same reply -- so they are replaced at the same moment
and die with the note. A coercion that outlived its note would be evidence for
a relation from a sentence nobody can find any more, which is the orphan
problem `ownership.py` exists for, in miniature.

DERIVED. Recomputable by re-extracting, and cheaply so: extraction is memoised
on the raw reply and coercion re-runs on every hit, so re-extracting an
unchanged note re-derives its coercions without a model call.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from brahmastra.sidecar import SidecarStore

# What each coercion kind means, for a reader of the report.
KINDS = {
    "unmapped_relation": "the model used a relation the ontology does not have",
    "alias": "a relation the ontology knows by another name",
    "domain_range": "a known relation between the wrong kinds of thing",
    "missing_fields": "dropped: the triple was malformed",
    "empty_endpoint": "dropped: a subject or object was blank",
    "placeholder_entity": "dropped: 'Unknown', 'N/A' and similar",
    "low_confidence": "dropped: the model was not sure",
    "malformed": "dropped: an array element that was not a triple at all",
}

# The kinds that are evidence about the VOCABULARY. The drops are evidence
# about the model or the prompt; they are recorded, but a report about which
# relations to add must not be swamped by them.
VOCABULARY_KINDS = ("unmapped_relation", "domain_range", "alias")


class CoercionStore(SidecarStore):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS extraction_coercions (
        workspace_id     TEXT NOT NULL DEFAULT 'default',
        note_id          TEXT NOT NULL,
        idx              INTEGER NOT NULL,
        kind             TEXT NOT NULL,
        raw_relation     TEXT,
        stored_relation  TEXT,
        subject_type     TEXT,
        object_type      TEXT,
        subject_text     TEXT,
        object_text      TEXT,
        created_at       TEXT NOT NULL,
        PRIMARY KEY (workspace_id, note_id, idx)
    );
    CREATE INDEX IF NOT EXISTS idx_coercions_kind
        ON extraction_coercions (workspace_id, kind, raw_relation);
    """

    def replace(self, note_id: str, rows: list[dict[str, Any]]) -> None:
        """
        This note's coercions, and only these. The previous set goes.

        Delete-then-insert in one transaction, because the triples beside
        these are replaced the same way and the two must never disagree about
        which extraction they came from.
        """
        self.init_schema()
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "DELETE FROM extraction_coercions "
                "WHERE workspace_id = ? AND note_id = ?"),
                (self.workspace, note_id))
            if rows:
                cur.executemany(self._ph(
                    "INSERT INTO extraction_coercions (workspace_id, note_id, "
                    "idx, kind, raw_relation, stored_relation, subject_type, "
                    "object_type, subject_text, object_text, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"),
                    [(self.workspace, note_id, i, r["kind"],
                      r.get("raw_relation"), r.get("stored_relation"),
                      r.get("subject_type"), r.get("object_type"),
                      r.get("subject_text"), r.get("object_text"), now)
                     for i, r in enumerate(rows)])

    def forget(self, note_id: str) -> None:
        self.replace(note_id, [])

    def all(self, kinds: Iterable[str] | None = None) -> list[dict[str, Any]]:
        self.init_schema()
        sql = "SELECT * FROM extraction_coercions WHERE workspace_id = ?"
        params: list[Any] = [self.workspace]
        wanted = list(kinds or [])
        if wanted:
            sql += " AND kind IN (" + ", ".join("?" for _ in wanted) + ")"
            params.extend(wanted)
        sql += " ORDER BY note_id, idx"
        with self._cursor() as cur:
            cur.execute(self._ph(sql), tuple(params))
            return [self._dict(r) for r in cur.fetchall()]


# -- turning a coerced triple into a row -------------------------------------


def describe(raw: dict[str, Any], stored: dict[str, Any] | None,
             reason: str) -> dict[str, Any]:
    """
    One coercion, as a row. `raw` is what the model produced; `stored` is what
    the graph got instead, or None if the fact was dropped.

    Built from the triples themselves rather than by parsing `reason`, which is
    a display string. Only its prefix is read -- the part before the first
    colon, which `_coerce_triple` uses as the kind.
    """
    kind = reason.split(":", 1)[0]
    if not isinstance(raw, dict):
        # Not a triple at all -- an empty string in the array, a bare number.
        # Kept, with what it actually was, because "the model is emitting
        # junk elements" is itself evidence, about the prompt if not the
        # vocabulary.
        return {"kind": kind, "raw_relation": None, "stored_relation": None,
                "subject_type": None, "object_type": None,
                "subject_text": repr(raw)[:200], "object_text": None}
    source = stored if stored is not None else raw

    def text(key: str) -> str | None:
        value = raw.get(key)
        return str(value).strip()[:200] if value is not None else None

    return {
        "kind": kind,
        "raw_relation": (str(raw.get("relation")).strip().lower()
                         if raw.get("relation") is not None else None),
        "stored_relation": stored.get("relation") if stored else None,
        # Types from the STORED triple where there is one: an alias can invert
        # a relation, and the stored direction is the one the graph holds.
        "subject_type": source.get("subject_type"),
        "object_type": source.get("object_type"),
        "subject_text": text("subject_text"),
        "object_text": text("object_text"),
    }


# -- reaching the store -------------------------------------------------------


def _store(workspace: str | None = None) -> CoercionStore:
    return CoercionStore(workspace)


def record(note_id: str, rows: list[dict[str, Any]],
           workspace: str | None = None) -> None:
    """Store a note's coercions. Never raises: failing to record is not failing
    to extract, and the triples have already been written by the time this
    runs."""
    try:
        _store(workspace).replace(note_id, rows)
    except Exception:
        pass


def forget(note_id: str, workspace: str | None = None) -> None:
    """The note is gone, so its evidence goes too. Never raises."""
    try:
        _store(workspace).forget(note_id)
    except Exception:
        pass


# -- the report ---------------------------------------------------------------


def summarise(rows: list[dict[str, Any]], examples: int = 3) -> list[dict[str, Any]]:
    """
    Group coercions into candidates for the vocabulary.

    One entry per (kind, raw_relation), ranked by how many NOTES produced it
    rather than how many times -- a relation one long note used twenty times
    is weaker evidence than one that twenty notes each used once, because the
    design doc's test is whether the data keeps asking, not whether one
    passage did.

    PURE, so the ranking can be tested without a database.
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["kind"], row.get("raw_relation") or "")
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "kind": row["kind"],
                "raw_relation": row.get("raw_relation") or "",
                "stored_relation": row.get("stored_relation"),
                "occurrences": 0,
                "notes": set(),
                "type_pairs": Counter(),
                "examples": [],
            }
        group["occurrences"] += 1
        group["notes"].add(row["note_id"])
        group["type_pairs"][(row.get("subject_type") or "unknown",
                             row.get("object_type") or "unknown")] += 1
        if len(group["examples"]) < examples:
            example = (row.get("subject_text"), row.get("raw_relation"),
                       row.get("object_text"))
            if example not in group["examples"]:
                group["examples"].append(example)

    out = []
    for group in groups.values():
        pairs = group["type_pairs"].most_common()
        out.append({
            "kind": group["kind"],
            "raw_relation": group["raw_relation"],
            "stored_relation": group["stored_relation"],
            "notes": len(group["notes"]),
            "occurrences": group["occurrences"],
            # The dominant (subject_type, object_type), and how dominant. A
            # relation whose uses agree on their types has a domain and a
            # range; one whose uses are scattered is probably several things
            # wearing one verb.
            "type_pairs": [{"subject": s, "object": o, "count": n}
                           for (s, o), n in pairs],
            "type_agreement": round(pairs[0][1] / group["occurrences"], 2)
                              if pairs else 0.0,
            "examples": [list(e) for e in group["examples"]],
        })
    out.sort(key=lambda g: (-g["notes"], -g["occurrences"], g["kind"],
                            g["raw_relation"]))
    return out


def report(kinds: Iterable[str] = VOCABULARY_KINDS,
           workspace: str | None = None) -> dict[str, Any]:
    """Everything the ontology had to bend, grouped and ranked."""
    rows = _store(workspace).all(kinds)
    by_kind = Counter(r["kind"] for r in rows)
    return {
        "total": len(rows),
        "by_kind": dict(sorted(by_kind.items())),
        "notes_affected": len({r["note_id"] for r in rows}),
        "candidates": summarise(rows),
    }


def main(argv: list[str] | None = None) -> int:
    """
    python -m brahmastra.coercions              vocabulary evidence only
    python -m brahmastra.coercions --all        the drops too
    python -m brahmastra.coercions --min 3      only relations 3+ notes used
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m brahmastra.coercions")
    parser.add_argument("--all", action="store_true",
                        help="include dropped facts, not only vocabulary evidence")
    parser.add_argument("--min", type=int, default=1,
                        help="only show relations at least this many notes used")
    parser.add_argument("--workspace", default=None)
    args = parser.parse_args(argv)

    out = report(kinds=list(KINDS) if args.all else VOCABULARY_KINDS,
                 workspace=args.workspace)

    def say(line: str = "") -> None:
        sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))

    say(f"{out['total']} coercions across {out['notes_affected']} notes")
    for kind, n in out["by_kind"].items():
        say(f"  {n:>5}  {kind:<20} {KINDS.get(kind, '')}")
    say()
    shown = [c for c in out["candidates"] if c["notes"] >= args.min]
    say(f"{'notes':>5} {'uses':>5}  {'agree':>5}  kind / relation as the model wrote it")
    for c in shown:
        top = c["type_pairs"][0] if c["type_pairs"] else None
        types = f"  [{top['subject']} -> {top['object']}]" if top else ""
        say(f"{c['notes']:>5} {c['occurrences']:>5}  {c['type_agreement']:>5.0%}  "
            f"{c['kind']}: {c['raw_relation']!r}{types}")
        for s, r, o in c["examples"]:
            say(f"{'':>20}e.g. {s!r} {r} {o!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
