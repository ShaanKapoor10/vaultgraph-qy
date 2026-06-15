"""
test_pipeline.py — End-to-end pipeline orchestration tests.
"""

from __future__ import annotations

import tempfile
import os
import sqlite3
from unittest.mock import patch
from brahmastra.pipeline import run_pipeline
from brahmastra import db


def test_pipeline_incremental_mode(monkeypatch):
    """Test that incremental mode only processes notes with status='pending'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        monkeypatch.setenv("BRAHMASTRA_DB", db_path)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        # Initialize DB
        db.init_db()

        # Add two notes: one done, one pending
        db.upsert_note("n1", "Title 1", "Content 1", mark_pending=False)
        db.upsert_note("n2", "Title 2", "Content 2", mark_pending=True)

        # Mark n1 as extraction_status='done' to simulate it's already processed
        db.mark_note_done("n1")

        with patch("brahmastra.extraction._extract_with_llm", return_value=[]):
            result = run_pipeline(full=False)

        # In incremental mode, only n2 should be extracted (it's pending)
        assert result["stages"]["extract"]["total_pending"] == 1
        assert result["stages"]["extract"]["extracted"] == 1


def test_pipeline_with_notion_sync_skip(monkeypatch):
    """Test that sync stage is skipped when NOTION_TOKEN is not set."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        monkeypatch.setenv("BRAHMASTRA_DB", db_path)
        # Make sure NOTION_TOKEN is not set
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        monkeypatch.delenv("NOTION_DATABASE_ID", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        # Initialize DB
        db.init_db()

        db.upsert_note("n1", "T", "C", mark_pending=True)

        with patch("brahmastra.extraction._extract_with_llm", return_value=[]):
            result = run_pipeline(full=False)

        # Sync stage should be skipped
        assert "skipped" in result["stages"]["sync"]

