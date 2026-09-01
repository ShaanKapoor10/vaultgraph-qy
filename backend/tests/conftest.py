"""
Suite-wide isolation.

The tests point BRAHMASTRA_DB at a temp file and clear the Notion and LLM
credentials, because they once ran against the production database and pushed
to the real Notion workspace. Session checkpointing added a third piece of real
state they could reach: the capture queue on disk.

That one is worse than it looks. `run_pipeline` drains the queue, so a test
calling it distils a genuine conversation into a throwaway database and then
DELETES the capture — the queue file is removed once its note is stored, and
the note lives in a temp file that vanishes at teardown. It only failed loudly
here because the suite clears the API keys, leaving no LLM to distil with.

The fourth was backend/.env itself, and it is the reason for the belt AND
braces below. Ten modules used to call load_dotenv at import; each was correct
that dotenv "never overrides an already-set var", and each was undone by the
half nobody wrote down: dotenv DOES fill in a variable that is absent. Deleting
a variable therefore armed it rather than disarming it, and any
`importlib.reload()` — which many tests do — pulled the developer's real
configuration back in and sent the test to the production Postgres. It passed
for as long as that database happened to have nothing pending.

So there are now three independent defences, because this failure mode has
recurred three times and each previous fix was also "careful enough":

  1. BRAHMASTRA_NO_DOTENV stops the file being read AT ALL, in one place,
     covering every variable that exists now or is added later.
  2. The storage variables are SET to empty rather than deleted, so even a
     module that somehow loads .env finds them already present.
  3. `_refuse_real_infrastructure` fails the test outright if the resolved
     database is the production one — catching any future route in, including
     ones nobody has thought of.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# The database the developer actually uses. Nothing in the suite may touch it.
PRODUCTION_DB = (
    Path(__file__).resolve().parent.parent / "data" / "concept_graph.db"
)


@pytest.fixture(autouse=True)
def _isolate_storage_choice(monkeypatch):
    """
    Decide configuration here, not in backend/.env.

    Tests that want another backend set it themselves; monkeypatch restores
    all of this afterwards either way.
    """
    # Defence 1: the file is not read at all.
    monkeypatch.setenv("BRAHMASTRA_NO_DOTENV", "1")

    # Defence 2: present-but-empty, so a stray load finds nothing to fill in.
    # Empty is falsy everywhere these are read, and means "single store, sqlite".
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("NOTE_BACKEND", "")
    monkeypatch.setenv("POSTGRES_DSN", "")
    monkeypatch.setenv("DATABASE_URL", "")

    # A LOCAL model is reachable without any credential, so clearing API keys
    # is not enough to keep the suite off an LLM. The moment `ollama serve` is
    # running -- which it now is, as the quota-free provider -- resolve_provider
    # picks it up and any test that forgets to stub comprehension quietly makes
    # real inference calls. Observed: a run went from 70 seconds to 201 because
    # four tests were talking to a 7B model nobody meant to invoke.
    #
    # Pointed at a port nothing listens on rather than deleted, for the same
    # reason as everything above: absent is what dotenv fills in.
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:1")
    monkeypatch.setenv("LLM_PROVIDER", "")

    # And the cloud keys, EXPLICITLY -- BRAHMASTRA_NO_DOTENV is not enough here.
    #
    # This file's own docstring said the suite "clears the Notion and LLM
    # credentials", and for the LLM half that was simply not true: nothing set
    # these. Disabling dotenv does not undo it either, because llm.py calls
    # load_env() AT IMPORT, which happens during collection -- before any
    # fixture runs -- so the real key is already in os.environ by then. The
    # same import-order trap that env.py exists to document.
    #
    # Meaning the suite has been running with a live, usable Groq key: any test
    # that forgot to stub an LLM call would have made a real one and spent real
    # quota, silently.
    for credential in ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "GROQ_MODEL",
                       "ANTHROPIC_MODEL"):
        monkeypatch.setenv(credential, "")


@pytest.fixture(autouse=True)
def _refuse_real_infrastructure(request, _isolate_storage_choice):
    """
    Fail the test if it resolved the production database.

    Defence 3, and the only one that does not depend on predicting HOW a leak
    happens. The previous two both guard a known mechanism; this one checks the
    outcome, so a future route into real data fails on the test that opened it
    rather than on whichever unlucky test runs when the data changes.

    Checked after the test body, because a test may reconfigure mid-run — which
    is exactly how the reload leak worked.

    `@pytest.mark.config_only` opts out of the NOTE_BACKEND check, for tests
    that set it to exercise how configuration is REPORTED without ever building
    a store. The marker is deliberately explicit: it makes "this test names a
    real backend on purpose" a visible claim rather than an accident.
    """
    declared_config_only = request.node.get_closest_marker("config_only") is not None
    yield

    db_path = os.environ.get("BRAHMASTRA_DB", "")
    if db_path and Path(db_path).resolve() == PRODUCTION_DB:
        pytest.fail(
            f"this test resolved the PRODUCTION database at {PRODUCTION_DB}. "
            "Point BRAHMASTRA_DB at tmp_path. The suite has reached live data "
            "three times before; see tests/conftest.py."
        )
    if os.environ.get("NOTE_BACKEND", "").strip() and not declared_config_only:
        pytest.fail(
            "this test left NOTE_BACKEND set, so it may have been talking to a "
            "real note store. Something re-loaded backend/.env — check the "
            "module uses brahmastra.env.load_env() rather than load_dotenv. If "
            "the test only exercises how configuration is REPORTED and never "
            "builds a store, mark it @pytest.mark.config_only."
        )


@pytest.fixture(autouse=True, scope="session")
def _isolate_runtime_state(tmp_path_factory):
    """
    Keep runtime state out of backend/data.

    A fifth thing the suite could reach. The keepalive records when each engine
    was last touched, and `run_pipeline` records one too — so every test that
    runs the pipeline dropped a `.graph-touch-<hash>` file into the developer's
    real data directory, one per temp store. Harmless in itself, and exactly
    the shape of the four leaks that were not: test state landing somewhere
    real because nothing said where else to put it.
    """
    state = tmp_path_factory.mktemp("runtime-state")
    previous = os.environ.get("BRAHMASTRA_DATA_DIR")
    os.environ["BRAHMASTRA_DATA_DIR"] = str(state)
    yield state
    if previous is None:
        os.environ.pop("BRAHMASTRA_DATA_DIR", None)
    else:
        os.environ["BRAHMASTRA_DATA_DIR"] = previous


@pytest.fixture(autouse=True, scope="session")
def _isolate_checkpoint_queue(tmp_path_factory):
    """Redirect the capture queue away from backend/data/checkpoints."""
    queue = tmp_path_factory.mktemp("checkpoints")
    previous = os.environ.get("BRAHMASTRA_CHECKPOINT_DIR")
    os.environ["BRAHMASTRA_CHECKPOINT_DIR"] = str(queue)
    yield queue
    if previous is None:
        os.environ.pop("BRAHMASTRA_CHECKPOINT_DIR", None)
    else:
        os.environ["BRAHMASTRA_CHECKPOINT_DIR"] = previous
