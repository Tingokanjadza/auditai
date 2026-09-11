# syntax=docker/dockerfile:1
#
# Container image for the LLM-Assisted IT Audit Risk and Control Assessment System.
#
#   docker build -t llm-it-auditor .
#   docker run --rm -p 8501:8501 llm-it-auditor          # audit console (default)
#   docker run --rm -p 8000:8000 -e APP_ROLE=api llm-it-auditor api
#   docker run --rm -p 8501:8501 -p 8000:8000 -e APP_ROLE=all llm-it-auditor all
#
# The image runs offline with no API key: the default provider is the deterministic mock,
# so `docker run` alone produces a complete, working demo with synthetic data.
#
# Two deliberate choices worth explaining to a reader:
#
# * **Python 3.12, although the source is written for 3.9.** The application targets the
#   3.9 interpreter on the author's machine (typing.Optional rather than `X | None`, and
#   so on), and that code runs unchanged on 3.12. Deploying on 3.12 is what platforms
#   support today, and it is also what makes the modern Anthropic SDK installable: the
#   1.x line requires >= 3.10, so the container gets anthropic 1.x while the local 3.9
#   virtualenv keeps the 0.x line. app/llm/anthropic_provider.py is written against the
#   API surface both support, so the same source works either way.
# * **Multi-stage.** Dependencies are installed into a virtualenv in a builder image that
#   has a compiler; the runtime image copies only the finished virtualenv, so no build
#   toolchain, no pip cache and no compiler ships in the thing that faces the network.

# ---------------------------------------------------------------------------
# Stage 1: build the virtualenv
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Every pinned dependency publishes a cp312 manylinux wheel for both amd64 and arm64
# (checked against the index: numpy and scikit-learn at manylinux_2_17, pandas at
# manylinux_2_28 - which bookworm's glibc 2.36 satisfies), so nothing here is expected to
# compile. The toolchain is installed anyway so that an architecture without a published
# wheel falls back to building from source instead of failing the build; it is discarded
# with this stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Copied on its own so the dependency layer is rebuilt only when the pins change.
COPY requirements.txt ./

# The Anthropic SDK is installed separately rather than pinned in requirements.txt,
# because it cannot be installed on the 3.9 interpreter the project is developed against:
# anthropic 1.x requires Python >= 3.10 (and depends on httpx2, so it does not disturb the
# pinned httpx). It is optional at runtime - app/llm/anthropic_provider.py imports it
# lazily and the factory falls back to the offline mock, loudly, when it is missing or
# unconfigured - so a build without network access to it still produces a working image.
#
# The comments stay outside the RUN on purpose: a comment line inside a line continuation
# is joined into one shell command by some Dockerfile parsers, where "#" would comment out
# everything after it and silently skip an install.
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt \
    && python -m pip install "anthropic>=1,<2"

# ---------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# PYTHONUNBUFFERED keeps uvicorn and Streamlit logs flowing to `docker logs` instead of
# sitting in a pipe buffer until the process exits - the difference between a debuggable
# container and a silent one.
#
# APP_ROLE is the only application variable set here, and it is not a Settings field: it
# tells the health check which service this container is running. Nothing that
# app/config.py reads is set as an image variable, because an environment variable takes
# priority over a .env file and a baked-in default would silently override the operator's
# own configuration.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_ROLE=ui

COPY --from=builder /opt/venv /opt/venv

# An unprivileged, fixed uid: fixed because a named volume mounted over /app/data must
# keep matching the user that writes to it. /app is the home directory as well, so
# Streamlit's own dot-directory lands somewhere writable.
RUN groupadd --system --gid 10001 audit \
    && useradd --system --uid 10001 --gid audit --home-dir /app --shell /usr/sbin/nologin audit

WORKDIR /app

# .dockerignore keeps this to source plus the control library: no .env, no virtualenv, no
# database, no previously uploaded evidence or generated report.
COPY --chown=audit:audit . /app

# The runtime data directories, created up front and owned by the runtime user so that a
# fresh named volume mounted at /app/data inherits that ownership from the image.
RUN mkdir -p /app/data/uploads /app/data/reports /app/data/synthetic /app/data/evaluation \
             /app/data/sample_evidence /app/data/controls \
    && chown -R audit:audit /app \
    && chmod +x /app/scripts/start_api.sh /app/scripts/start_ui.sh

# ---- entrypoint: one image, either service (or both)
# Written here rather than as a third file in scripts/ so that the dispatch logic lives
# beside the CMD that depends on it. The start commands themselves stay in scripts/, the
# single place the Procfile and render.yaml also call.
RUN printf '%s\n' \
    '#!/bin/sh' \
    '# Select the service this container runs: api | ui | all.' \
    '# Anything else is executed verbatim, so `docker run <image> python run.py init`' \
    '# and `docker run <image> sh` still work.' \
    'set -eu' \
    'role="${1:-${APP_ROLE:-ui}}"' \
    'case "$role" in' \
    '    api) exec /app/scripts/start_api.sh ;;' \
    '    ui)  exec /app/scripts/start_ui.sh ;;' \
    '    all)' \
    '        # Two services, two ports, so $PORT - which names exactly one - is cleared' \
    '        # for both children and each falls back to its own setting.' \
    '        PORT= API_PORT="${API_PORT:-8000}" /app/scripts/start_api.sh &' \
    '        api_pid=$!' \
    '        PORT= STREAMLIT_PORT="${STREAMLIT_PORT:-8501}" /app/scripts/start_ui.sh &' \
    '        ui_pid=$!' \
    '        trap "kill $api_pid $ui_pid 2>/dev/null || true" INT TERM' \
    '        # Exit as soon as either half dies: a container that keeps running with a' \
    '        # dead service looks healthy to an orchestrator and is restarted by nobody.' \
    '        while kill -0 "$api_pid" 2>/dev/null && kill -0 "$ui_pid" 2>/dev/null; do' \
    '            sleep 5' \
    '        done' \
    '        kill "$api_pid" "$ui_pid" 2>/dev/null || true' \
    '        exit 1' \
    '        ;;' \
    '    *)' \
    '        if [ "$#" -gt 0 ]; then exec "$@"; fi' \
    '        echo "audit-entrypoint: unknown role: $role (expected api, ui or all)" >&2' \
    '        exit 64' \
    '        ;;' \
    'esac' \
    > /usr/local/bin/audit-entrypoint \
    && chmod +x /usr/local/bin/audit-entrypoint

# ---- health check
# Probes the service this container is actually running. curl is not installed in the
# slim base and adding it for one HTTP GET is not worth the surface, so the probe uses
# the interpreter that is already here.
#
# The role comes from APP_ROLE rather than from the command line, because a health check
# runs as its own process and cannot see the container's arguments: when you override the
# command, set APP_ROLE to match (docker-compose.yml does).
RUN printf '%s\n' \
    '#!/bin/sh' \
    'set -eu' \
    'probe() { python -c "import sys,urllib.request; urllib.request.urlopen(sys.argv[1], timeout=4).read()" "$1" >/dev/null; }' \
    'role="${APP_ROLE:-ui}"' \
    'case "$role" in' \
    '    api) probe "http://127.0.0.1:${PORT:-${API_PORT:-8000}}/health" ;;' \
    '    ui)  probe "http://127.0.0.1:${PORT:-${STREAMLIT_PORT:-8501}}/_stcore/health" ;;' \
    '    all) probe "http://127.0.0.1:${API_PORT:-8000}/health"' \
    '         probe "http://127.0.0.1:${STREAMLIT_PORT:-8501}/_stcore/health" ;;' \
    '    *)   exit 0 ;;' \
    'esac' \
    > /usr/local/bin/audit-healthcheck \
    && chmod +x /usr/local/bin/audit-healthcheck

USER audit

# 8501 the console, 8000 the API. EXPOSE documents them; -p still has to publish them.
EXPOSE 8501 8000

# start-period is generous because the first start creates the SQLite schema and seeds
# the 14-control library before the port answers.
HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD ["/usr/local/bin/audit-healthcheck"]

ENTRYPOINT ["/usr/local/bin/audit-entrypoint"]

# The console is the demo, so it is the default. Override with `api` or `all`, and set
# APP_ROLE to the same value so the health check follows.
CMD ["ui"]
