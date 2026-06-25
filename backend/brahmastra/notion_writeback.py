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
_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ENV.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV)
    except ImportError:
        pass

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

    cached = db.get_cached_graph() or {}
    stats = cached.get("stats", {})
    contradictions = stats.get("contradictions", []) or []
    predicted = stats.get("predicted_links", []) or []
    cmap = db.get_canonical_map()
    all_triples = db.get_all_triples()

    pushed: list[dict[str, Any]] = []
    skipped = 0

    for note in db.get_notes():
        nid = note["id"]
        if not _is_notion_page_id(nid):
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
        _delete_existing_insights(client, nid)
        try:
            client.blocks.children.append(
                block_id=nid,
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
            "skipped": skipped, "pages": pushed}
