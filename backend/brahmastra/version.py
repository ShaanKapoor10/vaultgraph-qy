"""
Which code is this process actually running?

A question that should never need asking, and cost real time twice in one day
because nothing could answer it.

  THE STALE PROCESS. The MCP server loads its modules at startup and keeps them.
  After the resolver was fixed on disk, `brahmastra_run_pipeline` -- which runs
  inside that server -- reported 837 clusters while a fresh process on the same
  data reported 852. Both answers looked equally authoritative.

  THE STALE DEPLOYMENT. The whole Docker stack ran an image built three days
  earlier, and its scheduler re-ran the pipeline with that image's resolver
  every few minutes, overwriting every fix written from a fresh process. The
  graph kept serving a 20-file mega-cluster the code no longer produced.

CLAUDE.md documented the first trap. Documentation did not catch either. So:

  fingerprint   a digest of every source file of this package, taken when the
                package was IMPORTED -- what this process is running
  on_disk       the same digest, taken now -- what it would run if restarted
  stale         the two differ: this process is behind the files beside it

A process can therefore tell on its own that it is stale; that covers the MCP
server. A deployment cannot -- the image's files ARE what it loaded -- so it is
compared from outside, against the working tree:

    python -m brahmastra.version                       this checkout
    python -m brahmastra.version --against http://localhost:8001

Line endings are normalised before hashing. A Windows checkout and a Linux
container built from it must agree on what "the same code" means, or every
comparison between them would report a difference that is not one.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

PACKAGE = Path(__file__).resolve().parent


def _source_files() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def fingerprint_of_disk() -> str:
    """A digest of this package's source as it is on disk right now."""
    h = hashlib.sha256()
    for path in _source_files():
        h.update(path.relative_to(PACKAGE).as_posix().encode("utf-8"))
        h.update(bytes([0]))
        try:
            h.update(path.read_bytes().replace(b"\r\n", b"\n"))
        except OSError:
            h.update(b"<unreadable>")
        h.update(bytes([0]))
    return h.hexdigest()[:12]


# What this process loaded. Computed once, at import -- which is the moment the
# code it is running was read.
LOADED = fingerprint_of_disk()


def _git_commit() -> str | None:
    """The checkout's commit, when there is a checkout. A container has none."""
    configured = (os.environ.get("BRAHMASTRA_GIT_COMMIT") or "").strip()
    if configured:
        return configured
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=PACKAGE, capture_output=True, text=True,
                             timeout=5)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def status() -> dict[str, Any]:
    """This process's code, and whether it is behind the files beside it."""
    on_disk = fingerprint_of_disk()
    out: dict[str, Any] = {"fingerprint": LOADED, "on_disk": on_disk,
                           "stale": LOADED != on_disk}
    commit = _git_commit()
    if commit:
        out["commit"] = commit
    if out["stale"]:
        out["warning"] = ("this process is running older code than is on disk "
                          "-- restart it to pick up the changes")
    return out


def compare(url: str) -> dict[str, Any]:
    """This checkout against a running server, by fingerprint."""
    import json
    import urllib.request

    with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=20) as resp:
        remote = json.load(resp).get("code") or {}
    here = fingerprint_of_disk()
    return {"here": here, "there": remote.get("fingerprint"),
            "behind": remote.get("fingerprint") not in (None, here)}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m brahmastra.version")
    parser.add_argument("--against", metavar="URL",
                        help="compare this checkout with a running server")
    args = parser.parse_args(argv)

    if args.against:
        try:
            result = compare(args.against)
        except Exception as exc:
            print(f"could not reach {args.against}: {exc}")
            return 2
        if result["there"] is None:
            print(f"{args.against} does not report a code fingerprint -- it "
                  f"predates brahmastra/version.py, so it is behind by definition")
            return 1
        verdict = "BEHIND this checkout" if result["behind"] else "current"
        print(f"this checkout {result['here']}  server {result['there']}  -> {verdict}")
        return 1 if result["behind"] else 0

    s = status()
    print(f"loaded {s['fingerprint']}  on disk {s['on_disk']}  "
          f"{'STALE' if s['stale'] else 'current'}"
          + (f"  commit {s['commit']}" if s.get("commit") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
