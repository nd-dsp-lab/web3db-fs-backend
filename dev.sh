#!/bin/bash
# Development server: foreground, auto-reloading on any .py change.
#
# Production uses restart.sh, which runs the same server with no
# UVICORN_RELOAD set, so uvicorn starts a single process and no file watcher.
# Keeping the flag here rather than in .env is deliberate: the deploy
# procedure copies .env to the server, so a reload flag living there would
# silently re-enable the watcher in production on the next config refresh.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/app" || exit 1
source ../venv/bin/activate
UVICORN_RELOAD=1 python3 server.py
