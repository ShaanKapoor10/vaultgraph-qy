"""
Tests for the MCP server's tool surface.

These exist because of a bug that no import-based test could have caught: the
`if __name__ == "__main__": mcp.run()` block sat in the MIDDLE of the module,
above three @mcp.tool definitions. mcp.run() blocks forever, so running the
server registered only the tools declared above it, while
`from brahmastra.mcp_server import mcp` registered all ten — an import never
executes __main__. The code had the tools; the running server did not serve
them, and the two views disagreed for days.
"""
from __future__ import annotations

import asyncio
import runpy
from unittest.mock import patch

import pytest

# The MCP SDK is installed only in backend/.venv, which is what actually runs
# the server; the global interpreter runs the suite. Skip rather than fail
# there, and run these explicitly with:
#     .venv/Scripts/python.exe -m pytest tests/test_mcp_server.py -q
pytest.importorskip("mcp", reason="mcp SDK is installed in backend/.venv only")

EXPECTED = {
    "brahmastra_run_pipeline",
    "brahmastra_get_graph_stats",
    "brahmastra_search_entities",
    "brahmastra_search_notes",
    "brahmastra_get_entity_details",
    "brahmastra_get_contradictions",
    "brahmastra_add_note",
    "brahmastra_list_workspaces",
    "brahmastra_create_workspace",
    "brahmastra_search_all_workspaces",
}


def _tools_registered_as_main() -> set[str]:
    """Register tools the way the server does — as __main__, without blocking."""
    with patch("mcp.server.fastmcp.FastMCP.run", lambda self, **kw: None):
        module = runpy.run_module("brahmastra.mcp_server", run_name="__main__")
    return {t.name for t in asyncio.run(module["mcp"].list_tools())}


def test_every_tool_is_registered_when_run_as_the_server_runs_it():
    registered = _tools_registered_as_main()
    missing = EXPECTED - registered
    assert not missing, (
        f"declared but never registered: {sorted(missing)}. A tool defined below "
        "mcp.run() is invisible to clients — keep the entrypoint last."
    )
    assert registered == EXPECTED


def test_the_entrypoint_is_the_last_thing_in_the_file():
    """
    The structural guarantee behind the test above. Asserting position as well
    as behaviour means a tool added below the entrypoint fails loudly here,
    naming the cause, rather than only showing up as a missing tool.
    """
    import inspect
    from brahmastra import mcp_server

    source = inspect.getsource(mcp_server)
    run_at = source.index("mcp.run(transport=")
    assert "@mcp.tool" not in source[run_at:], (
        "a tool is defined after mcp.run(); it will never be registered"
    )
