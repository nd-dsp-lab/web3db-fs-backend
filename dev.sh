#!/bin/bash
# Development server: foreground, auto-reloading on any .py change.
#
# Production uses sgx/restart.sh, which runs the same server inside the
# enclave with no UVICORN_RELOAD set, so uvicorn starts a single process and
# no file watcher. Keeping the flag here rather than in .env is deliberate:
# a reload flag living in .env would end up sealed into the enclave's copy
# and silently re-enable the watcher in production.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/app" || exit 1
source ../venv/bin/activate
UVICORN_RELOAD=1 python3 server.py
