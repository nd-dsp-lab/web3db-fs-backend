#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
fuser -k 8090/tcp
cd "$SCRIPT_DIR/app"
source ../venv/bin/activate
nohup python3 server.py &
echo "Server started (PID $!)"
