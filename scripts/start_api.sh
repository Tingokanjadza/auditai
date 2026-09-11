#!/usr/bin/env sh
# Start the FastAPI backend.
#
# This is the single definition of the API start command: the Dockerfile entrypoint,
# the Procfile and render.yaml all call this file, so a change to how the service
# starts is one edit rather than four that can drift apart.
#
# For local development use `python run.py api` instead. That path binds API_HOST
# (127.0.0.1 by default); this one binds 0.0.0.0, because inside a container or a PaaS
# dyno a service listening on loopback is reachable by nothing but itself. Binding
# 0.0.0.0 on a laptop exposes an API that has no authentication to the whole network.
set -eu

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"

# Every PaaS (Render, Railway, Heroku, Fly) injects the port to listen on as $PORT and
# routes to nothing else. API_PORT is the project's own setting, used when $PORT is absent.
PORT="${PORT:-${API_PORT:-8000}}"
HOST="${HOST:-0.0.0.0}"

# Prefer the project virtualenv so the script also works from a plain shell; in the
# container the venv is already first on PATH and `python` is the right interpreter.
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
    if [ -x "$APP_DIR/.venv/bin/python" ]; then
        PYTHON="$APP_DIR/.venv/bin/python"
    else
        PYTHON="python3"
    fi
fi

# One worker by default: the prototype ships on SQLite, and concurrent writers on a
# single SQLite file produce "database is locked" rather than throughput. Raise
# WEB_CONCURRENCY only together with a PostgreSQL DATABASE_URL.
#
# --proxy-headers makes uvicorn trust X-Forwarded-Proto/For from the platform's router,
# so generated URLs say https rather than http behind a TLS terminator.
exec "$PYTHON" -m uvicorn app.api.main:app \
    --host "$HOST" \
    --port "$PORT" \
    --workers "${WEB_CONCURRENCY:-1}" \
    --proxy-headers \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
