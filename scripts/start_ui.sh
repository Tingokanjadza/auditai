#!/usr/bin/env sh
# Start the Streamlit audit console.
#
# The single definition of the UI start command, called by the Dockerfile entrypoint,
# the Procfile and render.yaml. For local development use `python run.py ui`, which
# opens a browser and keeps the server on localhost.
#
# The console is self-sufficient: it calls the service layer in-process, so it runs with
# no API behind it. Set USE_API=true (and API_BASE_URL) to route it through the backend.
set -eu

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"

# $PORT is injected by the platform; STREAMLIT_PORT is the project's own setting.
# Passed on the command line because Streamlit resolves CLI flags above both the
# environment and .streamlit/config.toml, which cannot interpolate a variable.
PORT="${PORT:-${STREAMLIT_PORT:-8501}}"
HOST="${HOST:-0.0.0.0}"

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
    if [ -x "$APP_DIR/.venv/bin/python" ]; then
        PYTHON="$APP_DIR/.venv/bin/python"
    else
        PYTHON="python3"
    fi
fi

# --server.headless true stops Streamlit trying to open a browser and asking for an
# email address on first run; a container has neither.
exec "$PYTHON" -m streamlit run app/frontend/streamlit_app.py \
    --server.port "$PORT" \
    --server.address "$HOST" \
    --server.headless true
