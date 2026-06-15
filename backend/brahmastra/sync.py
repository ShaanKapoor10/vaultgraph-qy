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

import os
from datetime import datetime, timezone
from typing import Any

from brahmastra import db

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


def _page_to_plain_text(client: Any, page_id: str) -> str:
    """Fetch all blocks for a page and return as plain text."""
    lines = []
    cursor = None
    while True:
        kwargs: dict[str, Any] = {"block_id": page_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = client.blocks.children.list(**kwargs)
        for block in resp.get("results", []):
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
# Main sync function
# ---------------------------------------------------------------------------

def run_sync() -> dict[str, Any]:
    """
    Sync pages from the configured Notion database into SQLite.

    Returns a summary dict:
      {
        "synced": N,       # pages upserted
        "unchanged": M,    # pages whose last_edited matched — skipped
        "errors": [...],   # per-page errors
      }
    """
    try:
        from notion_client import Client
    except ImportError as e:
        raise RuntimeError(
            "notion-client not installed. Run: uv pip install notion-client"
        ) from e

    token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DATABASE_ID")

    if not token:
        raise RuntimeError("NOTION_TOKEN env var not set")
    if not database_id:
        raise RuntimeError("NOTION_DATABASE_ID env var not set")

    client = Client(auth=token)
    db.init_db()

    # Fetch existing note metadata to detect changes
    existing = {n["id"]: n for n in db.get_notes()}

    synced = 0
    unchanged = 0
    errors: list[dict[str, str]] = []

    # Paginate through the Notion database
    cursor = None
    while True:
        kwargs: dict[str, Any] = {"database_id": database_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor

        try:
            resp = client.databases.query(**kwargs)
        except Exception as e:
            raise RuntimeError(f"Notion API error: {e}") from e

        for page in resp.get("results", []):
            page_id = page["id"]
            last_edited = page.get("last_edited_time", "")
            title = _get_page_title(page)

            # Skip archived pages
            if page.get("archived"):
                continue

            # Check if unchanged since last sync
            existing_note = existing.get(page_id)
            if (
                existing_note
                and existing_note.get("last_edited") == last_edited
                and existing_note.get("extraction_status") == "done"
            ):
                unchanged += 1
                continue

            # Fetch full block content
            try:
                content = _page_to_plain_text(client, page_id)
            except Exception as e:
                errors.append({"page_id": page_id, "title": title, "error": str(e)})
                continue

            if not content.strip():
                unchanged += 1
                continue

            db.upsert_note(
                id=page_id,
                title=title,
                content=content,
                last_edited=last_edited,
                mark_pending=True,
            )
            synced += 1

        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")

    return {
        "synced": synced,
        "unchanged": unchanged,
        "errors": errors,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }
