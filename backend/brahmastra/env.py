"""
One place that loads backend/.env, and one switch that turns it off.

Ten modules used to call `load_dotenv` themselves at import. Each carried a
comment saying it "never overrides an already-set var, so tests keep control",
which is true and was not enough: dotenv leaves a variable that is PRESENT
alone, but happily fills in one that is ABSENT. So the suite stayed isolated
only for as long as every test remembered to SET every storage variable rather
than delete it — across ten import sites and a list of variables that grows.

It failed exactly there. `conftest` deleted NOTE_BACKEND, an
`importlib.reload()` re-ran one of those ten loads, dotenv filled the gap from
the developer's real configuration, and the test went to the production
Postgres. It passed anyway for a while, because that database happened to have
nothing pending; it began failing the moment it did. That is the third time
this repository's tests have reached live infrastructure through config.

The fix is not another careful variable list. It is that under test, the file
is not read at all — one flag, checked in one function, covering every variable
that exists now or later.
"""

from __future__ import annotations

import os
from pathlib import Path

# backend/.env, resolved from this file so it does not depend on the working
# directory of whatever entrypoint imported us.
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

DISABLE_VAR = "BRAHMASTRA_NO_DOTENV"


# Runtime state that belongs to this deployment rather than to the code: the
# pipeline lock and the keepalive's touch stamp.
#
# Overridable, and in a container it MUST be overridden. The package lives on
# the image under /app, which is owned by root while the process runs as an
# unprivileged user, so the default is not merely a poor choice there -- it is
# unwritable. That is not theoretical: every pipeline run inside Docker failed
# with "[Errno 13] Permission denied: '/app/data'" before the lock looked here,
# which broke the dashboard's run button and the scheduler at once while
# everything else about both looked healthy.
#
# One function for both callers on purpose. They have to agree: the lock and
# the stamp describe the same run, and a container that wrote one to the volume
# and the other to the image would be half-configured in a way nobody notices.
_DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

DATA_DIR_VAR = "BRAHMASTRA_DATA_DIR"


def data_dir() -> Path:
    """Where runtime state is written. Read per call, never cached."""
    override = os.environ.get(DATA_DIR_VAR, "").strip()
    return Path(override) if override else _DEFAULT_DATA_DIR


def dotenv_disabled() -> bool:
    """Whether loading is switched off. Read per call, never cached."""
    return os.environ.get(DISABLE_VAR, "").strip().lower() in {"1", "true", "yes", "on"}


def load_env(path: Path | None = None) -> bool:
    """
    Load backend/.env into the environment. Returns True if it was read.

    Never overrides a variable that is already set, so an explicit environment
    still wins — that part of the old behaviour was right and is kept. What is
    new is that a single flag suppresses the read entirely, which is what makes
    test isolation a property of the suite rather than of every caller.

    Safe to call repeatedly and from anywhere; a missing file or a missing
    python-dotenv is not an error, since both are normal in a container where
    configuration arrives as real environment variables.
    """
    if dotenv_disabled():
        return False
    target = path or ENV_PATH
    if not target.exists():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    load_dotenv(target)
    return True
