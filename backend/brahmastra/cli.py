#!/usr/bin/env python3
"""
brahmastra — Concept Graph Engine CLI.

Commands:
  brahmastra run         Run the full pipeline (incremental by default).
  brahmastra run --full  Re-extract and reprocess everything.
  brahmastra add-note    Add a single note from a text file.
  brahmastra show graph  Print graph stats.
  brahmastra show nodes  Print ranked entity list.
  brahmastra show contradictions  Print detected contradictions.
  brahmastra show clusters  Print concept clusters.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

# Load .env from backend directory
_HERE = Path(__file__).resolve().parent
_ENV_FILE = _HERE / ".env"
if _ENV_FILE.exists():
    from dotenv import load_dotenv
    load_dotenv(_ENV_FILE)

app = typer.Typer(
    name="brahmastra",
    help="Concept Graph Engine — turn notes into a queryable knowledge graph.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True, style="bold red")

show_app = typer.Typer(help="Show graph insights.", no_args_is_help=True)
app.add_typer(show_app, name="show")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init():
    """Initialise DB on every command."""
    from brahmastra.db import init_db
    init_db()


# ---------------------------------------------------------------------------
# brahmastra run
# ---------------------------------------------------------------------------

@app.command("run")
def run(
    full: bool = typer.Option(False, "--full", help="Re-extract all notes (ignore previous results)."),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON."),
):
    """Run the full pipeline: extract → resolve → build-graph."""
    _init()
    from brahmastra.pipeline import run_pipeline

    console.print(Panel.fit(
        "[bold amber]Brahmastra[/] — running pipeline…",
        border_style="dim",
    ))

    with console.status("[dim]Running pipeline…[/dim]"):
        result = run_pipeline(full=full)

    if json_output:
        print(json.dumps(result, indent=2))
        return

    stages = result.get("stages", {})

    # Extract
    ext = stages.get("extract", {})
    console.print(f"[bold]extract[/]  notes={ext.get('extracted',0)}  "
                  f"triples_added={ext.get('triples_added',0)}  "
                  f"errors={len(ext.get('errors', []))}")
    if ext.get("errors"):
        for e in ext["errors"]:
            err_console.print(f"  note {e['note_id']}: {e['error']}")

    # Resolve
    res = stages.get("resolve", {})
    console.print(f"[bold]resolve[/]  clusters={res.get('clusters',0)}  "
                  f"mentions={res.get('mentions',0)}  "
                  f"merges={res.get('merge_edges',0)}  "
                  f"embeddings={'yes' if res.get('embedding_used') else 'no (heuristics only)'}")

    # Graph
    g = stages.get("graph", {})
    console.print(f"[bold]graph[/]    nodes={g.get('nodes',0)}  "
                  f"edges={g.get('edges',0)}  "
                  f"clusters={g.get('clusters',0)}  "
                  f"contradictions={g.get('contradictions',0)}  "
                  f"predicted_links={g.get('predicted_links',0)}")

    console.print(f"\n[green]Done.[/] started={result['started_at']}  finished={result['finished_at']}")


# ---------------------------------------------------------------------------
# brahmastra add-note
# ---------------------------------------------------------------------------

@app.command("add-note")
def add_note(
    title: str = typer.Argument(..., help="Note title."),
    file: Optional[Path] = typer.Option(None, "--file", "-f", help="Path to text file containing note content."),
    content: Optional[str] = typer.Option(None, "--content", "-c", help="Note content as inline string."),
    note_id: Optional[str] = typer.Option(None, "--id", help="Note ID (auto-generated if omitted)."),
):
    """Add a note to the database (marks it pending for extraction)."""
    _init()
    import uuid
    from brahmastra.db import upsert_note

    if file:
        body = Path(file).read_text(encoding="utf-8")
    elif content:
        body = content
    else:
        err_console.print("Provide either --file or --content.")
        raise typer.Exit(1)

    nid = note_id or str(uuid.uuid4())[:8]
    upsert_note(nid, title, body, mark_pending=True)
    console.print(f"[green]Added[/] note id=[cyan]{nid}[/] title=[bold]{title}[/] — status=pending")
    console.print("Run [bold]brahmastra run[/] to extract triples.")


# ---------------------------------------------------------------------------
# brahmastra show graph
# ---------------------------------------------------------------------------

@show_app.command("graph")
def show_graph(
    json_output: bool = typer.Option(False, "--json"),
):
    """Print graph stats summary."""
    _init()
    from brahmastra.db import get_cached_graph, get_db_stats

    cached = get_cached_graph()
    stats = get_db_stats()

    if json_output:
        print(json.dumps({"db": stats, "graph": cached}, indent=2))
        return

    t = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold")
    t.add_column("Metric", style="dim")
    t.add_column("Value", justify="right")

    t.add_row("Notes (total)",   str(stats["notes_total"]))
    t.add_row("Notes (pending)", str(stats["notes_pending"]))
    t.add_row("Raw triples",     str(stats["triples_total"]))
    t.add_row("Entity clusters", str(stats["entity_clusters"]))
    t.add_row("Graph cached",    "yes" if stats["graph_cached"] else "no")

    if cached:
        s = cached["stats"]
        t.add_row("Graph nodes",      str(s.get("nodes", 0)))
        t.add_row("Graph edges",      str(s.get("edges", 0)))
        t.add_row("Clusters",         str(len(s.get("concept_clusters", []))))
        t.add_row("Contradictions",   str(len(s.get("contradictions", []))))
        t.add_row("Predicted links",  str(len(s.get("predicted_links", []))))
        t.add_row("Built at",         cached.get("built_at", "—"))

    console.print(Panel(t, title="[bold]Brahmastra — Graph Stats[/]", border_style="dim"))


# ---------------------------------------------------------------------------
# brahmastra show nodes
# ---------------------------------------------------------------------------

@show_app.command("nodes")
def show_nodes(
    top: int = typer.Option(20, "--top", "-n", help="Number of top entities to show."),
    json_output: bool = typer.Option(False, "--json"),
):
    """Print top entities ranked by PageRank centrality."""
    _init()
    from brahmastra.db import get_cached_graph

    cached = get_cached_graph()
    if not cached:
        err_console.print("No graph cached yet. Run [bold]brahmastra run[/] first.")
        raise typer.Exit(1)

    nodes = cached["graph"].get("nodes", [])
    if json_output:
        print(json.dumps(sorted(nodes, key=lambda n: n["pagerank"], reverse=True)[:top], indent=2))
        return

    ranked = sorted(nodes, key=lambda n: n.get("pagerank", 0), reverse=True)[:top]

    t = Table(box=box.SIMPLE_HEAD, header_style="bold")
    t.add_column("#",       style="dim", justify="right", width=4)
    t.add_column("Entity",  min_width=24)
    t.add_column("Type",    style="dim", width=14)
    t.add_column("Cluster", justify="right", width=9)
    t.add_column("PageRank",justify="right", width=12)

    for i, n in enumerate(ranked, 1):
        t.add_row(
            str(i),
            n["label"],
            n.get("type", "—"),
            str(n.get("cluster", "—")),
            f"{n.get('pagerank', 0):.6f}",
        )

    console.print(Panel(t, title="[bold]Top Entities by PageRank[/]", border_style="dim"))


# ---------------------------------------------------------------------------
# brahmastra show contradictions
# ---------------------------------------------------------------------------

@show_app.command("contradictions")
def show_contradictions(
    json_output: bool = typer.Option(False, "--json"),
):
    """Print detected contradictions."""
    _init()
    from brahmastra.db import get_cached_graph

    cached = get_cached_graph()
    if not cached:
        err_console.print("No graph cached. Run [bold]brahmastra run[/] first.")
        raise typer.Exit(1)

    contradictions = cached["stats"].get("contradictions", [])
    if json_output:
        print(json.dumps(contradictions, indent=2))
        return

    if not contradictions:
        console.print("[dim]No contradictions detected.[/]")
        return

    for c in contradictions:
        console.print(Panel(
            f"[bold]{c['subject']}[/]  [dim]{c['relation']}[/]  ?\n\n"
            + "\n".join(
                f"  [{'green' if i == 0 else 'red'}]{'LATEST' if i == 0 else 'OLDER '}[/] "
                f"[italic]{e['object']}[/]  [dim]{e.get('source_quote', '')[:80]}[/]"
                for i, e in enumerate(c["evidence"])
            ),
            title=f"[bold red]Contradiction[/]  {c['subject']} / {c['relation']}",
            border_style="red",
        ))


# ---------------------------------------------------------------------------
# brahmastra show clusters
# ---------------------------------------------------------------------------

@show_app.command("clusters")
def show_clusters(
    json_output: bool = typer.Option(False, "--json"),
):
    """Print Louvain concept clusters."""
    _init()
    from brahmastra.db import get_cached_graph

    cached = get_cached_graph()
    if not cached:
        err_console.print("No graph cached. Run [bold]brahmastra run[/] first.")
        raise typer.Exit(1)

    clusters = cached["stats"].get("concept_clusters", [])
    if json_output:
        print(json.dumps(clusters, indent=2))
        return

    if not clusters:
        console.print("[dim]No clusters yet.[/]")
        return

    t = Table(box=box.SIMPLE_HEAD, header_style="bold")
    t.add_column("Cluster", width=9, justify="right")
    t.add_column("Size",    width=6, justify="right")
    t.add_column("Members", min_width=40)

    for c in clusters:
        members_str = ", ".join(c["members"][:8])
        if len(c["members"]) > 8:
            members_str += f" … (+{len(c['members']) - 8})"
        t.add_row(str(c["id"]), str(c["size"]), members_str)

    console.print(Panel(t, title="[bold]Concept Clusters (Louvain)[/]", border_style="dim"))


# ---------------------------------------------------------------------------
# brahmastra show predicted-links
# ---------------------------------------------------------------------------

@show_app.command("predicted-links")
def show_predicted_links(
    json_output: bool = typer.Option(False, "--json"),
):
    """Print link predictions."""
    _init()
    from brahmastra.db import get_cached_graph

    cached = get_cached_graph()
    if not cached:
        err_console.print("No graph cached. Run [bold]brahmastra run[/] first.")
        raise typer.Exit(1)

    links = cached["stats"].get("predicted_links", [])
    if json_output:
        print(json.dumps(links, indent=2))
        return

    if not links:
        console.print("[dim]No predicted links.[/]")
        return

    t = Table(box=box.SIMPLE_HEAD, header_style="bold")
    t.add_column("Source",          min_width=20)
    t.add_column("Target",          min_width=20)
    t.add_column("Jaccard",         justify="right", width=10)
    t.add_column("Common Nbrs",     justify="right", width=13)

    for lnk in links:
        t.add_row(
            lnk["source"],
            lnk["target"],
            f"{lnk.get('jaccard', 0):.4f}",
            str(lnk.get("common_neighbors", 0)),
        )

    console.print(Panel(t, title="[bold]Predicted Links[/]", border_style="dim"))


# ---------------------------------------------------------------------------
# brahmastra show notes
# ---------------------------------------------------------------------------

@show_app.command("notes")
def show_notes(
    json_output: bool = typer.Option(False, "--json"),
):
    """List all notes and their extraction status."""
    _init()
    from brahmastra.db import get_notes

    notes = get_notes()
    if json_output:
        print(json.dumps(notes, indent=2))
        return

    if not notes:
        console.print("[dim]No notes in database. Use [bold]brahmastra add-note[/] to add one.[/]")
        return

    t = Table(box=box.SIMPLE_HEAD, header_style="bold")
    t.add_column("ID",      width=12, style="dim")
    t.add_column("Title",   min_width=24)
    t.add_column("Status",  width=10)
    t.add_column("Last edited", width=22, style="dim")

    status_colors = {"done": "green", "pending": "yellow", "error": "red"}
    for n in notes:
        st = n.get("extraction_status", "—")
        color = status_colors.get(st, "white")
        t.add_row(
            n["id"],
            n["title"],
            f"[{color}]{st}[/]",
            (n.get("last_edited") or "—")[:19],
        )

    console.print(Panel(t, title="[bold]Notes[/]", border_style="dim"))


@app.command("sync")
def sync_notion(
    json_output: bool = typer.Option(False, "--json"),
):
    """Sync pages from a Notion database into SQLite (requires NOTION_TOKEN + NOTION_DATABASE_ID)."""
    _init()
    try:
        from brahmastra.sync import run_sync
    except ImportError as e:
        err_console.print(str(e))
        raise typer.Exit(1)

    with console.status("[dim]Syncing from Notion…[/dim]"):
        try:
            result = run_sync()
        except RuntimeError as e:
            err_console.print(str(e))
            raise typer.Exit(1)

    if json_output:
        print(json.dumps(result, indent=2))
        return

    console.print(
        f"[green]Synced[/] {result['synced']} pages  "
        f"[dim]unchanged={result['unchanged']}  errors={len(result['errors'])}[/]"
    )
    if result["errors"]:
        for e in result["errors"]:
            err_console.print(f"  {e['page_id']} ({e['title']}): {e['error']}")
    console.print("Run [bold]brahmastra run[/] to extract new notes.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
