"""
Groq keys a person adds and removes from the dashboard, on top of .env.

WHY A FILE AND NOT .env. In the compose deployment the backend never sees a
.env: compose reads the one at the repo root and passes the values in as
environment. A key added from the UI therefore has nowhere to go in .env, and
editing the host's file from inside a container would need a mount this
deployment deliberately does not have. So UI-managed keys live in a small file
in the data directory (the shared volume under compose, backend/data locally),
never in the repo, and the pool reads it beside the environment:

    effective keys = (keys from the environment - disabled) + keys added here

A key that came from .env cannot be deleted from here, only DISABLED, and the
list says which is which -- pretending to delete it would bring it back on the
next restart.

WHAT IT NEVER DOES: return a key. Every response carries a masked form and an
id (a hash prefix), and every action takes the id. The file itself holds keys in
plain text, exactly as .env does, and is written readable by its owner only.

SWITCHED OFF BY DEFAULT: writing secrets from an HTTP route is only reasonable
on a machine you control. BRAHMASTRA_KEY_ADMIN=1 turns the routes on.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

FILE_NAME = "llm-keys.json"
ADMIN_VAR = "BRAHMASTRA_KEY_ADMIN"
# Groq keys: "gsk_" and a long run of letters and digits. Checked before a key
# is stored, so a pasted sentence or a truncated copy is refused with a reason.
_GROQ_KEY = re.compile(r"^gsk_[A-Za-z0-9]{40,}$")

_lock = threading.Lock()


class KeyError_(ValueError):
    """A key that cannot be added or found. Its message is safe to show."""


def admin_enabled() -> bool:
    return os.environ.get(ADMIN_VAR, "").strip().lower() in {"1", "true", "yes", "on"}


def key_id(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def mask(key: str) -> str:
    return f"{key[:8]}…{key[-4:]}" if len(key) > 16 else "(short)"


def _path() -> Path:
    from brahmastra.env import data_dir

    return data_dir() / FILE_NAME


def _read() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        data = {}
    groq = data.get("groq") if isinstance(data.get("groq"), dict) else {}
    return {"groq": {"added": [k for k in groq.get("added", []) if isinstance(k, str)],
                     "disabled": [k for k in groq.get("disabled", []) if isinstance(k, str)]}}


def _write(data: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass                                   # Windows: ACLs, not mode bits
    os.replace(tmp, path)


def env_groq_keys() -> list[str]:
    """GROQ_API_KEYS, then GROQ_API_KEY, de-duplicated, order kept."""
    listed = [k.strip() for k in (os.environ.get("GROQ_API_KEYS") or "").split(",")]
    single = (os.environ.get("GROQ_API_KEY") or "").strip()
    keys: list[str] = []
    for key in listed + [single]:
        if key and key not in keys:
            keys.append(key)
    return keys


def effective_groq_keys() -> list[str]:
    """What the pool uses: the environment's keys minus disabled, plus added."""
    stored = _read()["groq"]
    disabled = set(stored["disabled"])
    keys = [k for k in env_groq_keys() if key_id(k) not in disabled]
    for key in stored["added"]:
        if key not in keys and key_id(key) not in disabled:
            keys.append(key)
    return keys


def list_groq_keys() -> list[dict[str, Any]]:
    """Every known key, masked, with where it came from and the pool's view of it."""
    from brahmastra import groq_pool

    stored = _read()["groq"]
    disabled = set(stored["disabled"])
    state = {row["key"]: row for row in groq_pool.status()}
    rows = []
    for source, keys in (("env", env_groq_keys()), ("dashboard", stored["added"])):
        for key in keys:
            if any(r["id"] == key_id(key) for r in rows):
                continue
            pool_row = state.get(groq_pool.mask(key), {})
            rows.append({
                "id": key_id(key), "key": mask(key), "source": source,
                "disabled": key_id(key) in disabled,
                "state": "disabled" if key_id(key) in disabled else pool_row.get("state", "unused"),
                "reason": pool_row.get("reason", ""), "organization": pool_row.get("organization", ""),
                "calls": pool_row.get("calls", 0), "failures": pool_row.get("failures", 0),
            })
    return rows


def test_groq_key(key: str) -> dict[str, Any]:
    """Can this key reach Groq? Lists models, which costs no tokens."""
    try:
        from brahmastra.groq_pool import make_client

        models = make_client(key).models.list()
        names = [m.id for m in getattr(models, "data", [])]
        return {"ok": True, "models": len(names)}
    except Exception as exc:                                  # noqa: BLE001
        text = str(exc)
        reason = ("invalid key" if "401" in text or "invalid" in text.lower()
                  else f"{type(exc).__name__}: {text[:160]}")
        # Never echo a key back, even inside an error from the SDK.
        return {"ok": False, "error": re.sub(r"gsk_[A-Za-z0-9]+", "gsk_…", reason)}


def _find(kid: str) -> tuple[str, str]:
    stored = _read()["groq"]
    for source, keys in (("env", env_groq_keys()), ("dashboard", stored["added"])):
        for key in keys:
            if key_id(key) == kid:
                return key, source
    raise KeyError_(f"no key with id {kid!r}")


def add_groq_key(key: str, test: bool = True) -> dict[str, Any]:
    key = (key or "").strip()
    if not _GROQ_KEY.match(key):
        raise KeyError_("that does not look like a Groq key (gsk_ followed by 40+ letters and digits)")
    if test:
        result = test_groq_key(key)
        if not result["ok"]:
            raise KeyError_(f"Groq refused the key: {result['error']}")
    with _lock:
        data = _read()
        groq = data["groq"]
        groq["disabled"] = [d for d in groq["disabled"] if d != key_id(key)]
        if key not in env_groq_keys() and key not in groq["added"]:
            groq["added"].append(key)
        _write(data)
    return {"id": key_id(key), "key": mask(key)}


def remove_groq_key(kid: str) -> dict[str, Any]:
    """Delete a dashboard key; DISABLE an .env key, which cannot be deleted from here."""
    key, source = _find(kid)
    with _lock:
        data = _read()
        groq = data["groq"]
        if source == "dashboard":
            groq["added"] = [k for k in groq["added"] if k != key]
            action = "deleted"
        else:
            if kid not in groq["disabled"]:
                groq["disabled"].append(kid)
            action = "disabled (it is set in .env; remove it there to delete it)"
        _write(data)
    return {"id": kid, "key": mask(key), "action": action}


def enable_groq_key(kid: str) -> dict[str, Any]:
    key, _ = _find(kid)
    with _lock:
        data = _read()
        data["groq"]["disabled"] = [d for d in data["groq"]["disabled"] if d != kid]
        _write(data)
    return {"id": kid, "key": mask(key), "action": "enabled"}
