"""
Keep a pausable graph engine awake.

Neo4j Aura Free suspends an instance after roughly three days without a query.
A suspended instance does not merely refuse connections -- its hostname stops
resolving, so the failure surfaces as a DNS error that reads like a typo in
NEO4J_URI rather than an instance that needs resuming. That misreading costs
more time than the outage does.

Nothing already running prevents it, and the reason is worth stating plainly
because it is the whole point of this module. The scheduler's idle tick calls
``db.get_notes(status="pending")``, which under the deployed arrangement
(NOTE_BACKEND=postgres + GRAPH_BACKEND=neo4j) is answered ENTIRELY by Postgres.
The loop can tick every fifteen minutes for a week and never send Neo4j a
single query. A keepalive built on the db facade would inherit exactly that
blind spot, so this one reaches past it to the graph half deliberately.

    python -m brahmastra.keepalive              # touch it if it has gone idle
    python -m brahmastra.keepalive --force      # touch it regardless
    python -m brahmastra.keepalive --status     # say when it was last touched
    python -m brahmastra.keepalive --loop       # stay running and keep touching

Configuration:
    GRAPH_KEEPALIVE=0           switch it off
    GRAPH_KEEPALIVE_HOURS=12    touch after this much idleness (default 12)
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from brahmastra.env import data_dir, load_env

load_env()

from brahmastra.stores.base import GraphStore

# Aura Free's limit is about 72 hours. Twelve leaves five further attempts of
# margin, so the instance survives a keepalive that fails, a scheduler that is
# restarted, and a machine that is off overnight -- without becoming a process
# that queries a remote database every few minutes for no reason.
DEFAULT_IDLE_HOURS = 12.0

# Backends that can go to sleep while still being configured correctly. SQLite
# is a file on this disk; it has no notion of idleness, and pinging it would
# just be an odd way to stat a file.
_PAUSABLE = {"neo4j", "postgres"}


class KeepaliveUnavailable(RuntimeError):
    """The engine did not answer. Usually means suspended, not misconfigured."""


# ---------------------------------------------------------------------------
# Reaching the engine specifically
# ---------------------------------------------------------------------------

def graph_half(store: GraphStore) -> GraphStore:
    """
    The store that actually holds the graph.

    Under a split arrangement the composite routes by method name, and the
    methods a naive keepalive would reach for -- get_notes, search_notes,
    get_db_stats -- are either SOURCE methods or are overridden to answer from
    the note half. Asking the composite to prove the engine is awake would
    therefore prove that POSTGRES is awake, which was never in doubt.
    """
    half = getattr(store, "graph_store", None)
    return half if half is not None else store


def _digest(store: GraphStore) -> str:
    return hashlib.sha1(graph_half(store).describe().encode("utf-8")).hexdigest()[:12]


def _state_path(store: GraphStore) -> Path:
    """One stamp per engine, so two graphs cannot vouch for each other."""
    return data_dir() / f".graph-touch-{_digest(store)}"


def _resolve(store: GraphStore | None) -> GraphStore:
    if store is not None:
        return store
    from brahmastra.stores import get_store
    return get_store()


# ---------------------------------------------------------------------------
# When was it last hit
# ---------------------------------------------------------------------------

def record_contact(store: GraphStore | None = None, when: float | None = None) -> None:
    """
    Note that something reached the engine just now.

    Called by the pipeline as well as by this module, so real work counts as a
    keepalive and the instance is not queried twice for the same purpose. Never
    raises: failing to record a touch must degrade into one extra ping later,
    not into a failed pipeline run.
    """
    try:
        target = _resolve(store)
        path = _state_path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(when if when is not None else time.time()), encoding="utf-8")
    except Exception:
        pass


def last_contact(store: GraphStore | None = None) -> float | None:
    """Epoch seconds of the last recorded touch, or None if there is no record."""
    try:
        raw = _state_path(_resolve(store)).read_text(encoding="utf-8").strip()
        return float(raw)
    except (OSError, ValueError):
        return None


def idle_seconds(store: GraphStore | None = None) -> float | None:
    last = last_contact(store)
    return None if last is None else max(0.0, time.time() - last)


def idle_limit() -> float:
    """Seconds of idleness that earn a ping. Read per call, never cached."""
    raw = os.environ.get("GRAPH_KEEPALIVE_HOURS", "").strip()
    try:
        hours = float(raw) if raw else DEFAULT_IDLE_HOURS
    except ValueError:
        hours = DEFAULT_IDLE_HOURS
    # A floor rather than a free choice: a limit of zero would turn every
    # scheduler tick into a remote query, which is the failure this avoids.
    return max(60.0, hours * 3600.0)


def enabled() -> bool:
    return os.environ.get("GRAPH_KEEPALIVE", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


# ---------------------------------------------------------------------------
# The touch itself
# ---------------------------------------------------------------------------

def ping(store: GraphStore | None = None) -> dict[str, Any]:
    """
    Make the engine execute a query, and say how it went.

    `stats()` rather than a bare round trip on purpose: it is cheap at any size
    this project reaches, it is on the contract for every backend, and it comes
    back with counts -- so the log line says what the graph held rather than
    just "ok", which is the difference between a heartbeat someone reads and
    one they learn to skip over.
    """
    target = _resolve(store)
    engine = graph_half(target)
    t0 = time.monotonic()
    try:
        stats = engine.stats()
    except Exception as exc:
        raise KeepaliveUnavailable(
            f"{engine.describe()} did not answer: {type(exc).__name__}: {exc}"
        ) from exc
    elapsed = (time.monotonic() - t0) * 1000.0
    record_contact(target)
    # Derived counts only. The engine's own `notes_total` is its provenance
    # stubs, not the system of record -- reporting it would read as notes
    # having gone missing whenever Postgres holds more of them, which is
    # precisely why the composite strips that key from the graph half.
    return {
        "ok": True,
        "target": engine.describe(),
        "latency_ms": round(elapsed, 1),
        "entities": stats.get("entity_clusters"),
        "triples": stats.get("triples_total"),
    }


def touch_if_idle(
    store: GraphStore | None = None,
    force: bool = False,
    max_idle: float | None = None,
) -> dict[str, Any]:
    """
    Ping the engine, but only when nothing else has.

    Always returns a dict carrying `pinged` and `reason`, and never raises: a
    keepalive that can take down its caller is worse than one that misses a
    beat, because the caller is a loop whose whole job is to still be running
    in three days.
    """
    from brahmastra.stores import backend_name

    if not force and not enabled():
        return {"pinged": False, "reason": "GRAPH_KEEPALIVE is off"}

    backend = backend_name()
    if not force and backend not in _PAUSABLE:
        return {"pinged": False, "reason": f"{backend} does not idle out"}

    limit = idle_limit() if max_idle is None else max_idle
    idle = idle_seconds(store)

    # No record at all means nothing has vouched for this engine on this
    # machine, so touch it rather than assume it is fine.
    if not force and idle is not None and idle < limit:
        return {
            "pinged": False,
            "reason": "touched {:.1f}h ago, under the {:.1f}h limit".format(
                idle / 3600, limit / 3600
            ),
            "idle_hours": round(idle / 3600, 2),
        }

    try:
        result = ping(store)
    except KeepaliveUnavailable as exc:
        return {"pinged": False, "ok": False, "reason": str(exc), "error": str(exc)}

    if force:
        why = "forced"
    elif idle is None:
        why = "no record of a previous touch"
    else:
        why = "idle for {:.1f}h".format(idle / 3600)
    result["pinged"] = True
    result["reason"] = why
    return result


# ---------------------------------------------------------------------------
# Standalone
# ---------------------------------------------------------------------------

def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def loop(interval: float | None = None) -> None:
    """
    Stay running and keep the engine awake.

    For when the scheduler is not running. The scheduler calls touch_if_idle
    itself, so running both is harmless rather than duplicative: whichever gets
    there first records the contact and the other one skips.
    """
    if interval is None:
        # Check several times per limit, so one failed check is not the
        # difference between awake and suspended.
        interval = min(idle_limit() / 4, 3600.0)
    print(
        "[{}] graph keepalive started (check every {:.0f}m, touch after {:.1f}h idle)".format(
            _stamp(), interval / 60, idle_limit() / 3600
        ),
        flush=True,
    )
    while True:
        try:
            res = touch_if_idle()
        except Exception as exc:            # belt and braces; touch_if_idle swallows
            print(f"[{_stamp()}] keepalive error: {type(exc).__name__}: {exc}", flush=True)
        else:
            if res.get("pinged"):
                print(f"[{_stamp()}] touched {res.get('target')} in "
                      f"{res.get('latency_ms')}ms ({res.get('reason')})", flush=True)
            elif res.get("error"):
                print(f"[{_stamp()}] FAILED: {res['error']}", flush=True)
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Keep the graph engine from idling out.",
    )
    parser.add_argument("--force", action="store_true", help="ping regardless of idleness")
    parser.add_argument("--status", action="store_true", help="report only, do not ping")
    parser.add_argument("--loop", action="store_true", help="stay running and keep touching")
    parser.add_argument("--interval", type=float, default=None,
                        help="seconds between checks, with --loop")
    args = parser.parse_args(argv)

    if args.loop:
        loop(args.interval)
        return 0

    from brahmastra.stores import backend_name

    if args.status:
        store = _resolve(None)
        engine = graph_half(store)
        idle = idle_seconds(store)
        print(f"backend      {backend_name()}")
        print(f"engine       {engine.describe()}")
        print(f"pauses       {'yes' if backend_name() in _PAUSABLE else 'no'}")
        print(f"limit        {idle_limit() / 3600:.1f}h")
        print("last touch   " + ("never recorded" if idle is None
                                 else f"{idle / 3600:.2f}h ago"))
        print(f"state file   {_state_path(store)}")
        return 0

    res = touch_if_idle(force=args.force)
    if res.get("pinged"):
        # ASCII on purpose: the Windows console is cp1252, and an em dash here
        # printed as a replacement character.
        print(f"touched {res['target']} in {res['latency_ms']}ms - "
              f"{res.get('entities')} entities, {res.get('triples')} triples "
              f"({res['reason']})")
        return 0
    if res.get("error"):
        print(f"could not reach the engine: {res['error']}", file=sys.stderr)
        return 1
    print(f"skipped: {res['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
