# ============================================================
# Dockerfile — AI Voice Agent (CILA)
# ============================================================
# Produces a self-contained production image that runs the
# FastAPI/uvicorn telephony server.
#
# Build:  docker build -t cila-ai-agent .
# Run:    docker run -p 8000:8000 --env-file .env cila-ai-agent
# ============================================================

# ---------------------------------------------------------------------------
# Base image
# ---------------------------------------------------------------------------
# python:3.11-slim is based on Debian Bookworm and contains only the minimum
# OS packages needed to run Python — no dev tools, docs, or extra locales.
# The slim tag keeps the final image ~200 MB smaller than the full python:3.11
# image, which matters in Kubernetes where images are pulled on every new node.
FROM python:3.11-slim

# ---------------------------------------------------------------------------
# Environment variables — set before any RUN command so they apply everywhere
# ---------------------------------------------------------------------------

# PYTHONDONTWRITEBYTECODE=1: prevents Python from writing .pyc bytecode cache
# files to disk. In a container the cache is never reused between builds, so
# omitting it saves disk I/O and keeps the layer clean.
ENV PYTHONDONTWRITEBYTECODE=1

# PYTHONUNBUFFERED=1: forces stdout/stderr to be sent straight to Docker logs
# without buffering. Critical for seeing real-time server output in
# `docker logs` and in CI pipelines where output is streamed line-by-line.
ENV PYTHONUNBUFFERED=1

# APP_ENV=production: signals to the application code that it is running in a
# production context. The orchestrator and logging modules check this value to
# control feature flags, logging verbosity, and mock-vs-real API routing.
ENV APP_ENV=production

# ---------------------------------------------------------------------------
# Working directory
# ---------------------------------------------------------------------------

# Set the working directory inside the container to /app.
# All subsequent COPY, RUN, and CMD instructions use /app as the base path.
# The application code is copied here and uvicorn is started from here.
WORKDIR /app

# ---------------------------------------------------------------------------
# System dependencies
# ---------------------------------------------------------------------------

# Install OS-level build dependencies needed by Python packages with C extensions:
#   build-essential : gcc, g++, make — required to compile packages like pgvector
#                     and asyncpg that ship with Cython/C extensions.
#   libpq-dev       : PostgreSQL client C headers — required by asyncpg and
#                     psycopg2 to link against the libpq library at compile time.
#   curl            : used by the HEALTHCHECK below.
#
# --no-install-recommends skips "suggested" Debian packages, saving ~50 MB.
# The final `rm -rf /var/lib/apt/lists/*` purges the apt package lists so they
# are not baked into the image layer (they are re-fetched on the next `apt-get
# update` anyway).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Python dependencies — cached layer
# ---------------------------------------------------------------------------

# Copy ONLY requirements.txt before copying the full source tree.
# Docker builds layers in order; copying requirements.txt first means this
# pip install layer is cached and reused on every code-only change, making
# iterative builds much faster (pip install takes ~2 min; this skips it).
COPY requirements.txt .

# Install all Python packages listed in requirements.txt.
# --no-cache-dir: do not store the pip HTTP download cache in the image layer,
# keeping the final image smaller (saves ~50-100 MB for this project).
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Application source code
# ---------------------------------------------------------------------------

# Copy the entire project source tree into /app.
# This layer comes AFTER all pip install steps so that code-only changes do
# not invalidate the (much slower) dependency installation cache layers above.
# A .dockerignore file should exclude: .env, __pycache__, .git, *.pyc, etc.
COPY . .

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

# Expose port 8000 — this is the port uvicorn listens on inside the container.
# EXPOSE is documentation only; it does not publish the port to the host.
# The operator must map it with -p 8000:8000 (Docker) or a Service manifest (K8s).
EXPOSE 8000

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

# Docker will call this command periodically and mark the container "unhealthy"
# if it returns a non-zero exit code after the configured number of retries.
# An unhealthy container is restarted by Docker Swarm / the K8s kubelet.
#
#   --interval=30s    : run the check every 30 seconds
#   --timeout=5s      : fail if the HTTP call takes longer than 5 seconds
#   --start-period=5s : give the app 5 s to initialise before the first check
#   --retries=3       : require 3 consecutive failures before marking unhealthy
#
# /healthz is the lightweight liveness endpoint (does not check DB or Redis).
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

# ---------------------------------------------------------------------------
# Container startup command
# ---------------------------------------------------------------------------

# Start the server using the canonical launcher.
# run_server.py ensures sys.path is configured and .env is loaded before the
# FastAPI app initialises, preventing import errors and missing-env-var crashes.
# Using the exec form (JSON array) avoids spawning a shell, so SIGTERM is
# delivered directly to the Python process — enabling graceful shutdown.
CMD ["python", "run_server.py"]
