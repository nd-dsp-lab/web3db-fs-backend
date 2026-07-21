#!/bin/bash
# Restart the backend: kill whatever holds port 8090, then relaunch detached.
# All logs (app + uvicorn access + startup) go to logs/web3fs.log via
# logging_config, so the process's own stdout/stderr is discarded. To debug a
# crash that happens before logging is up, run `python3 server.py` in the
# foreground.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
fuser -k 8090/tcp
cd "$SCRIPT_DIR/app"
source ../venv/bin/activate
nohup python3 server.py >/dev/null 2>&1 &
echo "Server started (PID $!)"
