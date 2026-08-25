"""
Run throwaway code against a throwaway database.

The test suite has three defences against reaching real infrastructure. Nothing
else has any. A one-off `python -c` typed to check a hypothesis reads
backend/.env like every other process, resolves the deployed arrangement, and
talks to the real Postgres and the real Aura — which is how two debug probes
came to write notes into the production graph and need deleting afterwards.

Telling the next person to remember four environment variables does not fix
that; it is the same "careful enough" that failed three times in the test
suite. So this is a RUNNER rather than a helper:

    python -m brahmastra.scratch -c "from brahmastra import db; print(db.describe())"
    python -m brahmastra.scratch -m brahmastra.keepalive --status
    python -m brahmastra.scratch probe.py

The environment is set before your code is imported, which is the part a
helper function cannot promise. `brahmastra.stores` loads .env at import and
answers "which store?" for the whole process, so anything that imports it
before the guard is applied has already resolved the wrong one — an ordering
subtlety that no amount of care removes and this entrypoint makes impossible.

Nothing here is a sandbox. It redirects storage and disarms the Notion
write-back; it does not stop code that names a production host explicitly.
"""

from __future__ import annotations

import argparse
import os
import runpy
import sys
import tempfile
from pathlib import Path
from typing import Any

# The database the developer actually uses, resolved the same way the test
# suite resolves it so the two cannot drift apart.
PRODUCTION_DB = Path(__file__).resolve().parent.parent / "data" / "concept_graph.db"

# What a scratch process must not inherit. Set to empty rather than deleted,
# deliberately: python-dotenv leaves a variable that is PRESENT alone but
# happily fills in one that is ABSENT, so deleting these would ARM the leak
# instead of disarming it. That is the exact mistake that sent the suite to the
# production Postgres, and it is worth making only once.
_EMPTY = (
    "NOTE_BACKEND",
    "POSTGRES_DSN",
    "DATABASE_URL",
    "POSTGRES_HOST",
    # Notion is the outward-facing half: the pipeline's write-back stage edits
    # real pages, so a probe that happens to run the pipeline would publish.
    "NOTION_TOKEN",
    "NOTION_DATABASE_ID",
)


class UnsafeScratch(RuntimeError):
    """The scratch environment resolved to something real."""


def scratch_env(db_path: str | Path | None = None) -> dict[str, str]:
    """
    The environment a throwaway process should have. Computed, not applied.

    Separate from `activate` so it can be inspected, logged, and asserted on in
    a test without a process having to adopt it.
    """
    if db_path is None:
        db_path = Path(tempfile.gettempdir()) / f"brahmastra-scratch-{os.getpid()}.db"
    resolved = Path(db_path).resolve()
    if resolved == PRODUCTION_DB.resolve():
        raise UnsafeScratch(
            f"refusing to use the production database at {PRODUCTION_DB} as scratch"
        )

    env = {
        # The one switch that covers every variable, present or future. Without
        # it the four below are a list that rots the moment a fifth is added.
        "BRAHMASTRA_NO_DOTENV": "1",
        "GRAPH_BACKEND": "sqlite",
        "BRAHMASTRA_DB": str(resolved),
        # A remote engine has nothing to stay awake for here, and a scratch run
        # vouching for the real one would be a lie the keepalive then believes.
        "GRAPH_KEEPALIVE": "0",
    }
    env.update({name: "" for name in _EMPTY})
    return env


def activate(db_path: str | Path | None = None) -> dict[str, str]:
    """
    Apply the scratch environment to THIS process.

    Only sound before anything has resolved a store; the cached ones are
    dropped so a later call rebuilds, but a caller already holding a store
    reference keeps talking to whatever it holds. Prefer the `-m` runner, which
    has no such window.
    """
    env = scratch_env(db_path)
    os.environ.update(env)
    if "brahmastra.stores" in sys.modules:
        sys.modules["brahmastra.stores"].reset_store()   # type: ignore[attr-defined]
    return env


def verify() -> dict[str, Any]:
    """
    Resolve a store and confirm it is not a real one.

    Checks the OUTCOME rather than the configuration, which is the only check
    that survives a route in that nobody predicted — the same reason the test
    suite has a third defence that does not depend on knowing how a leak works.
    """
    from brahmastra import db
    from brahmastra.stores import backend_name, note_backend_name

    target = db.describe()
    if backend_name() != "sqlite" or note_backend_name():
        raise UnsafeScratch(f"scratch resolved a networked store: {target}")

    configured = os.environ.get("BRAHMASTRA_DB", "")
    if configured and Path(configured).resolve() == PRODUCTION_DB.resolve():
        raise UnsafeScratch(f"scratch resolved the production database: {target}")
    return {"backend": backend_name(), "target": target}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m brahmastra.scratch",
        description="Run throwaway code against a throwaway database.",
    )
    parser.add_argument("-c", dest="code", help="a statement to run")
    parser.add_argument("-m", dest="module", help="a module to run as __main__")
    parser.add_argument("--db", default=None, help="scratch database path (default: a temp file)")
    parser.add_argument("--quiet", action="store_true", help="do not print where it pointed")
    parser.add_argument("script", nargs="?", help="a script to run")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="arguments for it")
    # Known-args, because the thing being run has flags of its own and they
    # arrive looking like ours: `-m brahmastra.keepalive --status` would
    # otherwise fail on an unrecognised --status before running anything.
    ns, extra = parser.parse_known_args(argv)
    ns.args = [*ns.args, *extra]

    chosen = [x for x in (ns.code, ns.module, ns.script) if x]
    if len(chosen) != 1:
        parser.error("give exactly one of -c CODE, -m MODULE, or a script path")

    activate(ns.db)
    where = verify()
    if not ns.quiet:
        # Say it out loud. A probe that silently pointed somewhere safe teaches
        # nobody where it was pointing when it does not.
        print(f"[scratch] {where['target']}", file=sys.stderr)

    if ns.code:
        sys.argv = ["-c", *ns.args]
        exec(compile(ns.code, "<scratch>", "exec"), {"__name__": "__main__"})
    elif ns.module:
        sys.argv = [ns.module, *ns.args]
        runpy.run_module(ns.module, run_name="__main__", alter_sys=True)
    else:
        sys.argv = [ns.script, *ns.args]
        runpy.run_path(ns.script, run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
