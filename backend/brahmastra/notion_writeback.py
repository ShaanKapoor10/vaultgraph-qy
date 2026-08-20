"""
Stage 8 — Notion write-back (the bidirectional brain).

Pushes Brahmastra's discovered intelligence BACK into each Notion page:
  • Relationships found in the note (auto-extracted triples)
  • ⚠️ Contradictions involving the note's entities
  • 🔗 Suggested connections (predicted links) touching the note's entities

All insights live inside a single collapsible toggle block titled
"🧠 Brahmastra Insights" at the bottom of the page. The push is IDEMPOTENT:
on each run the old toggle is deleted and a fresh one appended, so insights
stay current without piling up.

This is what makes Brahmastra better than Obsidian: you never hand-link —
the graph discovers the connections and writes them into your Notion pages.

Usage:  from brahmastra.notion_writeback import push_insights
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Ensure NOTION_TOKEN is available regardless of entrypoint
from brahmastra.env import load_env

load_env()

from brahmastra import db

INSIGHT_MARKER = "🧠 Brahmastra Insights"


# ---------------------------------------------------------------------------
# Block builders
# ---------------------------------------------------------------------------

def _text(content: str, bold: bool = False) -> dict[str, Any]:
    return {
        "type": "text",
        "text": {"content": content},
        "annotations": {"bold": bold},
    }


def _heading(content: str) -> dict[str, Any]:
    return {"type": "paragraph", "paragraph": {"rich_text": [_text(content, bold=True)]}}


def _bullet(content: str) -> dict[str, Any]:
    return {
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [_text(content)]},
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_notion_page_id(note_id: str) -> bool:
    """Notion page IDs are UUIDs (5 dash-separated groups)."""
    return note_id.count("-") == 4


def _canon(cmap: dict[str, str], name: str) -> str:
    return cmap.get(name, name)


def _delete_existing_insights(client: Any, page_id: str) -> None:
    """Remove any prior '🧠 Brahmastra Insights' toggle (idempotency)."""
    try:
        resp = client.blocks.children.list(block_id=page_id, page_size=100)
    except Exception:
        return
    for block in resp.get("results", []):
        if block.get("type") != "toggle":
            continue
        rt = block.get("toggle", {}).get("rich_text", [])
        text = "".join(t.get("plain_text", t.get("text", {}).get("content", "")) for t in rt)
        if text.strip().startswith(INSIGHT_MARKER):
            try:
                client.blocks.delete(block_id=block["id"])
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Publishing Brahmastra-born notes INTO Notion
# ---------------------------------------------------------------------------
#
# Notion is the human surface; Neo4j is the system of record. So publishing is
# opt-in per note (`publish`), not automatic: a session checkpoint is working
# memory and belongs in the graph, while a design decision is prose somebody
# will want to re-read, and belongs in Notion.
#
# The page id returned by Notion MUST be persisted. Without it every run
# creates the page again, and the workspace fills with duplicates of the same
# note — the one failure mode that makes this feature worse than not having it.

# Notion rejects a rich_text run longer than this, and more than 100 children
# in one request.
_MAX_TEXT = 1900
_MAX_CHILDREN = 100


def _paragraphs(content: str) -> list[dict[str, Any]]:
    """Note body -> paragraph blocks, split to stay under Notion's text cap."""
    blocks: list[dict[str, Any]] = []
    for line in content.splitlines():
        line = line.rstrip()
        if not line:
            continue
        for i in range(0, len(line), _MAX_TEXT):
            blocks.append({
                "type": "paragraph",
                "paragraph": {"rich_text": [_text(line[i:i + _MAX_TEXT])]},
            })
    return blocks


def _title_property(client: Any, target: str) -> tuple[str, dict[str, Any]]:
    """
    Return (title property name, parent spec) for the configured database.

    Branches on capability, not version: the 2025-09-03 API parents a page on a
    data source, older clients on the database itself. Same reasoning as
    sync._iter_database_rows — both SDK generations are installed here.
    """
    meta = client.databases.retrieve(target)
    if hasattr(client, "data_sources") and meta.get("data_sources"):
        source_id = meta["data_sources"][0]["id"]
        parent = {"type": "data_source_id", "data_source_id": source_id}
        schema = client.data_sources.retrieve(source_id).get("properties", {})
    else:
        parent = {"type": "database_id", "database_id": target}
        schema = meta.get("properties", {})

    for name, prop in schema.items():
        if prop.get("type") == "title":
            return name, parent
    return "Name", parent


def publish_notes(client: Any, target: str) -> dict[str, Any]:
    """
    Create a Notion page for every note marked `publish` that has none yet.

    Only creates. Updating an existing published page is deliberately left
    alone for now: the note body lives in the graph, and rewriting a page a
    human may have edited is a merge problem, not a write.
    """
    created: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    candidates = [
        n for n in db.get_notes()
        if n.get("publish")
        and not n.get("notion_page_id")
        and not _is_notion_page_id(n["id"])  # already lives in Notion
    ]
    if not candidates:
        return {"created": 0, "pages": [], "errors": []}

    title_prop, parent = _title_property(client, target)

    for note in candidates:
        blocks = _paragraphs(note["content"])
        try:
            page = client.pages.create(
                parent=parent,
                properties={title_prop: {"title": [{"text": {"content": note["title"][:2000]}}]}},
                children=blocks[:_MAX_CHILDREN],
            )
            # Persist FIRST: a crash after this point costs an incomplete body,
            # which the next run can finish. Losing the id costs a duplicate
            # page every run, forever.
            db.set_notion_page_id(note["id"], page["id"])

            for i in range(_MAX_CHILDREN, len(blocks), _MAX_CHILDREN):
                client.blocks.children.append(
                    block_id=page["id"], children=blocks[i:i + _MAX_CHILDREN]
                )
            created.append({"note_id": note["id"], "page_id": page["id"],
                            "title": note["title"]})
        except Exception as e:
            errors.append({"note_id": note["id"], "error": str(e)[:200]})

    return {"created": len(created), "pages": created, "errors": errors}


def _notion_target(note: dict[str, Any]) -> str | None:
    """The Notion page this note's insights belong on, if any."""
    if _is_notion_page_id(note["id"]):
        return note["id"]                      # pulled FROM Notion
    return note.get("notion_page_id") or None  # published TO Notion


def push_insights() -> dict[str, Any]:
    """
    Write Brahmastra insights back into every Notion-sourced page.
    Returns {"pushed": N, "pages": [...], "skipped": M}.
    """
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise RuntimeError("NOTION_TOKEN not set — cannot write back to Notion")

    try:
        from notion_client import Client
    except ImportError as e:
        raise RuntimeError("notion-client not installed") from e

    client = Client(auth=token)

    # Publish first, so a note created in this run also gets its insights in
    # the same run rather than waiting for the next one.
    published = {"created": 0, "pages": [], "errors": []}
    # One resolver for the workspace's Notion source, shared with sync, so
    # pull and publish can never disagree about which database is meant.
    from brahmastra.sync import _notion_target_for_current_workspace
    target = _notion_target_for_current_workspace()
    if target:
        try:
            published = publish_notes(client, target)
        except Exception as e:
            published = {"created": 0, "pages": [], "errors": [{"error": str(e)[:200]}]}

    cached = db.get_cached_graph() or {}
    stats = cached.get("stats", {})
    contradictions = stats.get("contradictions", []) or []
    predicted = stats.get("predicted_links", []) or []
    cmap = db.get_canonical_map()
    all_triples = db.get_all_triples()

    pushed: list[dict[str, Any]] = []
    skipped = 0

    for note in db.get_notes():
        # Triples are keyed by the note's OWN id; the blocks go to its Notion
        # page, which is a different id once a note was published rather than
        # pulled. Conflating the two silently annotates nothing.
        nid = note["id"]
        page_id = _notion_target(note)
        if not page_id:
            continue

        page_triples = [t for t in all_triples if t["source_note_id"] == nid]
        if not page_triples:
            skipped += 1
            continue

        # Entities this page talks about (canonicalised)
        ents: set[str] = set()
        for t in page_triples:
            ents.add(_canon(cmap, t["subject_text"]))
            ents.add(_canon(cmap, t["object_text"]))

        # 1. Relationships found in this note
        rel_lines = []
        seen_rel = set()
        for t in page_triples:
            s = _canon(cmap, t["subject_text"])
            o = _canon(cmap, t["object_text"])
            key = (s, t["relation"], o)
            if key in seen_rel:
                continue
            seen_rel.add(key)
            rel_lines.append(f"{s}  —{t['relation']}→  {o}")

        # 2. Contradictions touching this page's entities
        contra_lines = []
        for ctr in contradictions:
            subj = _canon(cmap, ctr["subject"])
            if subj in ents or ctr["subject"] in ents:
                vals = '"  vs  "'.join(ctr["conflicting_values"])
                contra_lines.append(f'{ctr["subject"]} ({ctr["relation"]}): "{vals}"')

        # 3. Predicted links touching this page's entities
        pred_lines = []
        for pl in predicted:
            a, b = pl.get("source"), pl.get("target")
            if a in ents or b in ents:
                pred_lines.append(f"{a}  ↔  {b}")

        # Assemble toggle children
        children: list[dict[str, Any]] = []
        if rel_lines:
            children.append(_heading("Relationships found:"))
            children.extend(_bullet(l) for l in rel_lines)
        if contra_lines:
            children.append(_heading("⚠️ Contradictions:"))
            children.extend(_bullet(l) for l in contra_lines)
        if pred_lines:
            children.append(_heading("🔗 Suggested connections:"))
            children.extend(_bullet(l) for l in pred_lines[:5])

        if not children:
            skipped += 1
            continue

        # Idempotent replace
        _delete_existing_insights(client, page_id)
        try:
            client.blocks.children.append(
                block_id=page_id,
                children=[{
                    "type": "toggle",
                    "toggle": {
                        "rich_text": [_text(INSIGHT_MARKER, bold=True)],
                        "children": children,
                    },
                }],
            )
            pushed.append({
                "title": note["title"],
                "relationships": len(rel_lines),
                "contradictions": len(contra_lines),
                "suggested": len(pred_lines),
            })
        except Exception as e:
            skipped += 1
            pushed.append({"title": note["title"], "error": str(e)})

    return {"pushed": len([p for p in pushed if "error" not in p]),
            "skipped": skipped, "pages": pushed,
            "published": published["created"], "published_pages": published["pages"],
            "publish_errors": published["errors"]}
