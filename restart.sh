#!/bin/bash
# Restart the backend: kill whatever holds port 8090, then relaunch detached.
# Application logs land in logs/web3fs.log (rotating, via logging_config).
# This redirect only captures uvicorn's own output and any startup traceback.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
fuser -k 8090/tcp
mkdir -p "$SCRIPT_DIR/logs"
cd "$SCRIPT_DIR/app"
source ../venv/bin/activate
nohup python3 server.py >> "$SCRIPT_DIR/logs/uvicorn.out" 2>&1 &
echo "Server started (PID $!)"
