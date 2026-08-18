# Brahmastra backend — FastAPI + the pipeline.
#
# Two decisions dominate the size and the cold-start time of this image:
#
#   1. CPU-ONLY TORCH. sentence-transformers pulls torch, and the default wheel
#      ships CUDA kernels — roughly 2.5 GB that a server without a GPU will
#      never execute. The CPU index below cuts the image by well over a
#      gigabyte with no behavioural difference: entity resolution runs on CPU
#      either way.
#
#   2. THE EMBEDDING MODEL IS BAKED IN. all-MiniLM-L6-v2 is ~88 MB and is
#      downloaded on first use. Left to runtime that download happens on every
#      cold start, on the request that triggered it, and fails outright in a
#      network-restricted environment. Downloading it at build time makes cold
#      starts fast and the container work offline.

# ---------------------------------------------------------------------------
# Stage 1 — build the virtualenv
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# build-essential is needed by a few wheels; it stays in this stage only.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# CPU-only torch first, so the dependency resolver never pulls the CUDA build.
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.3.0"

# Dependencies before source: this layer is cached until pyproject changes,
# which is what keeps ordinary code edits from reinstalling torch.
COPY pyproject.toml README.md* ./
RUN mkdir -p brahmastra && touch brahmastra/__init__.py && pip install .

COPY brahmastra/ ./brahmastra/
COPY main.py ./
RUN pip install --no-deps .

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Absolute, and inside the image. A relative cache path once put the 88 MB
    # model in backend/backend/.cache; here a wrong value would silently
    # re-download on every start.
    BRAHMASTRA_CACHE=/opt/model-cache \
    HF_HOME=/opt/model-cache \
    # Defaults a deploy can override. The database lives on the mounted volume,
    # never in the image layer, or every redeploy would discard the graph.
    GRAPH_BACKEND=sqlite \
    BRAHMASTRA_DB=/data/concept_graph.db \
    BRAHMASTRA_CHECKPOINT_DIR=/data/checkpoints \
    PORT=8001

COPY --from=builder /opt/venv /opt/venv

# Bake the embedding model. Done as its own layer so it is cached across code
# changes, and before dropping privileges so the cache is owned by root and
# read-only to the app.
RUN python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2', cache_folder='/opt/model-cache')" \
 && chmod -R a+rX /opt/model-cache

# Run unprivileged. /data is the mount point for the volume; it is created and
# chowned here so the container still starts when nothing is mounted.
RUN useradd --system --create-home --uid 10001 brahmastra \
 && mkdir -p /data /data/checkpoints \
 && chown -R brahmastra:brahmastra /data

WORKDIR /app
COPY --from=builder /build/brahmastra /app/brahmastra
COPY --from=builder /build/main.py /app/main.py

USER brahmastra
EXPOSE 8001

# Readiness, not liveness: this verifies the graph store actually answers.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "\
import os,sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8001')+'/health/ready',timeout=8).status==200 else 1)"

# ONE worker by default, on purpose. With GRAPH_BACKEND=sqlite the database is
# a single file and concurrent writers produce "database is locked"; the
# pipeline lock is per store, not per process. Scale out only with
# GRAPH_BACKEND=neo4j, and raise WEB_CONCURRENCY there.
ENV WEB_CONCURRENCY=1
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8001} --workers ${WEB_CONCURRENCY:-1}"]
