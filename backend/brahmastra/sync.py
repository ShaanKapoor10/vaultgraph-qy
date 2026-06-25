"""
Stage 7 — Notion sync.

Pulls pages from a Notion database, converts rich-text blocks to plain text,
and upserts them into the local SQLite notes table with extraction_status='pending'
if their last_edited time has changed since the last sync.

Requirements:
  NOTION_TOKEN        — Notion integration token (secret_...)
  NOTION_DATABASE_ID  — The database / page ID to pull from

Install: uv pip install notion-client

Usage (programmatic):  from brahmastra.sync import run_sync
Usage (CLI):           brahmastra sync
Usage (FastAPI):       POST /pipeline/sync
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from brahmastra import db

# The Notion SDK logs (and retries) a WARNING when we probe whether the configured
# ID is a database — for a page that probe is expected to fail and we handle it by
# falling back to page mode. Silence that expected noise.
logging.getLogger("notion_client").setLevel(logging.ERROR)

# Cache the database-vs-page detection per process so we probe only once, not on
# every pipeline run (the watcher + backend run this constantly).
_SOURCE_IS_DB: dict[str, bool] = {}

# ---------------------------------------------------------------------------
# Block-to-text conversion
# ---------------------------------------------------------------------------

def _rich_text_to_str(rich_text: list[dict]) -> str:
    """Flatten a Notion rich_text array to a plain string."""
    return "".join(rt.get("plain_text", "") for rt in rich_text)


def _block_to_text(block: dict) -> str:
    """Convert a single Notion block to a plain text line."""
    btype = block.get("type", "")
    data = block.get(btype, {})
    rich = data.get("rich_text", [])

    if btype in (
        "paragraph", "heading_1", "heading_2", "heading_3",
        "bulleted_list_item", "numbered_list_item", "quote", "callout",
        "toggle", "to_do",
    ):
        text = _rich_text_to_str(rich)
        # Add markdown-ish headings for context
        if btype == "heading_1":
            return f"# {text}"
        if btype == "heading_2":
            return f"## {text}"
        if btype == "heading_3":
            return f"### {text}"
        if btype == "to_do":
            checked = data.get("checked", False)
            return f"- [{'x' if checked else ' '}] {text}"
        return text
    if btype == "code":
        code = _rich_text_to_str(rich)
        lang = data.get("language", "")
        return f"```{lang}\n{code}\n```"
    if btype == "divider":
        return "---"
    return ""


# Marker for Brahmastra's own write-back block — must match notion_writeback.INSIGHT_MARKER.
# We skip it on read so the engine never re-ingests its own generated insights
# (otherwise: write-back → re-extract → re-write → infinite feedback loop).
_INSIGHT_MARKER = "🧠 Brahmastra Insights"


def _is_insight_block(block: dict) -> bool:
    if block.get("type") != "toggle":
        return False
    rt = block.get("toggle", {}).get("rich_text", [])
    text = "".join(t.get("plain_text", "") for t in rt)
    return text.strip().startswith(_INSIGHT_MARKER)


def _page_to_plain_text(client: Any, page_id: str) -> str:
    """Fetch all blocks for a page and return as plain text (excluding our insight block)."""
    lines = []
    cursor = None
    while True:
        kwargs: dict[str, Any] = {"block_id": page_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = client.blocks.children.list(**kwargs)
        for block in resp.get("results", []):
            if _is_insight_block(block):
                continue  # never ingest Brahmastra's own annotations
            text = _block_to_text(block)
            if text:
                lines.append(text)
            # Recurse into toggle/callout children (one level deep)
            if block.get("has_children"):
                try:
                    child_resp = client.blocks.children.list(block_id=block["id"], page_size=100)
                    for child in child_resp.get("results", []):
                        child_text = _block_to_text(child)
                        if child_text:
                            lines.append("  " + child_text)
                except Exception:
                    pass
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return "\n".join(lines)


def _get_page_title(page: dict) -> str:
    """Extract the title property from a Notion page object."""
    props = page.get("properties", {})
    # Try common title property names
    for key in ("Name", "Title", "title", "name"):
        prop = props.get(key, {})
        ptype = prop.get("type", "")
        if ptype == "title":
            return _rich_text_to_str(prop.get("title", []))
    # Fallback: first title-type property found
    for prop in props.values():
        if prop.get("type") == "title":
            return _rich_text_to_str(prop.get("title", []))
    return page.get("id", "untitled")


# ---------------------------------------------------------------------------
# Title for a bare page (child page) vs a database row
# ---------------------------------------------------------------------------

def _page_title_any(page: dict) -> str:
    """Title for any page object — child pages expose it under a 'title' prop."""
    title = _get_page_title(page)
    if title and title != page.get("id"):
        return title
    # Child pages: properties.title.title = [...]
    props = page.get("properties", {})
    t = props.get("title", {})
    if t.get("type") == "title":
        return _rich_text_to_str(t.get("title", []))
    return "Untitled"


# ---------------------------------------------------------------------------
# Process a single page object → upsert as a note
# ---------------------------------------------------------------------------

def _process_page(
    client: Any,
    page: dict,
    existing: dict[str, dict],
    counters: dict[str, int],
    errors: list[dict[str, str]],
) -> None:
    page_id = page["id"]
    if page.get("archived") or page.get("in_trash"):
        return

    last_edited = page.get("last_edited_time", "")
    title = _page_title_any(page)

    try:
        content = _page_to_plain_text(client, page_id)
    except Exception as e:
        errors.append({"page_id": page_id, "title": title, "error": str(e)})
        return

    # Skip pages with no body text — a title alone has no relationships to graph
    # (keeps empty placeholder pages out of the knowledge graph).
    if not content.strip():
        counters["unchanged"] += 1
        return

    # Include the title in the content so the extractor has full context
    full = f"{title}\n\n{content}".strip()

    # CONTENT-based change detection (not last_edited_time). Our own write-back
    # bumps last_edited but leaves the real body unchanged — comparing the
    # extracted content (which already excludes our insight block) prevents the
    # write-back → re-extract feedback loop.
    existing_note = existing.get(page_id)
    if (
        existing_note
        and existing_note.get("content") == full
        and existing_note.get("extraction_status") == "done"
    ):
        counters["unchanged"] += 1
        return

    db.upsert_note(
        id=page_id,
        title=title,
        content=full,
        last_edited=last_edited,
        mark_pending=True,
    )
    counters["synced"] += 1


# ---------------------------------------------------------------------------
# Main sync function — auto-detects database vs page mode
# ---------------------------------------------------------------------------

def run_sync() -> dict[str, Any]:
    """
    Sync notes from Notion into SQLite.

    Auto-detects the configured NOTION_DATABASE_ID:
      - If it's a database  → query its rows (each row = a note).
      - If it's a page (or unset) → pull every page the integration can access
        via search (each page = a note). This is the friendlier default since
        many users organise notes as plain pages, not databases.

    Returns: {"synced": N, "unchanged": M, "errors": [...], "mode": ...}
    """
    try:
        from notion_client import Client
    except ImportError as e:
        raise RuntimeError(
            "notion-client not installed. Run: pip install notion-client"
        ) from e

    token = os.environ.get("NOTION_TOKEN")
    target_id = os.environ.get("NOTION_DATABASE_ID")

    if not token:
        raise RuntimeError("NOTION_TOKEN env var not set")

    client = Client(auth=token)
    db.init_db()

    existing = {n["id"]: n for n in db.get_notes()}
    counters = {"synced": 0, "unchanged": 0}
    errors: list[dict[str, str]] = []

    # Decide mode: is target_id a database? Probe once per process, then cache.
    # The probe intentionally fails for page IDs; silence ALL logging around it
    # (logging.disable is global+temporary) so the SDK's expected WARNING/retries
    # don't spam the console.
    is_database = False
    if target_id:
        if target_id in _SOURCE_IS_DB:
            is_database = _SOURCE_IS_DB[target_id]
        else:
            _prev_disable = logging.root.manager.disable
            logging.disable(logging.WARNING)
            try:
                client.databases.retrieve(target_id)
                is_database = True
            except Exception:
                is_database = False
            finally:
                logging.disable(_prev_disable)
            _SOURCE_IS_DB[target_id] = is_database

    if is_database:
        mode = "database"
        cursor = None
        while True:
            kwargs: dict[str, Any] = {"database_id": target_id, "page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            try:
                resp = client.databases.query(**kwargs)
            except Exception as e:
                raise RuntimeError(f"Notion API error: {e}") from e
            for page in resp.get("results", []):
                _process_page(client, page, existing, counters, errors)
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
    else:
        # Page mode: pull every page the integration can access via search.
        mode = "pages"
        cursor = None
        while True:
            kwargs = {
                "filter": {"property": "object", "value": "page"},
                "page_size": 100,
            }
            if cursor:
                kwargs["start_cursor"] = cursor
            try:
                resp = client.search(**kwargs)
            except Exception as e:
                raise RuntimeError(f"Notion search error: {e}") from e
            for page in resp.get("results", []):
                if page.get("object") != "page":
                    continue
                _process_page(client, page, existing, counters, errors)
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")

    return {
        "mode": mode,
        "synced": counters["synced"],
        "unchanged": counters["unchanged"],
        "errors": errors,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }
