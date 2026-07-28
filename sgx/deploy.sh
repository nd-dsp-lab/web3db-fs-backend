#!/bin/bash
# Blue/green deploy of the enclave: boot the idle slot, prove it serves,
# flip the proxy to it, retire the old one.
#
#   ./deploy.sh --status    which slot is live, what is running
#   ./deploy.sh             deploy (prompts before the flip)
#   ./deploy.sh --yes       deploy without prompting
#   ./deploy.sh --stage     boot and verify the idle slot, stop before flipping
#   ./deploy.sh --rollback  flip straight back to the other slot
#
# --stage then a later --yes splits the slow part (a ~5 minute enclave boot)
# from the instant part, so a maintenance window only has to cover the flip.
#
# Run from a trusted workstation: signing happens on the key host, and the
# SGX host never holds the signing key.
#
# Why this exists: an enclave takes ~5 minutes to boot, so restarting in
# place means five minutes of downtime for every deploy and every
# certificate renewal. Two slots — identical enclaves on different ports —
# let the new one warm up while the old one keeps serving. Users see the
# switch as a graceful nginx reload; in-flight requests drain on the old
# worker.
#
# The flip is one line in the proxy's TLS stream map. Rollback is the same
# line, which is why the retired enclave is only stopped at the very end.
set -euo pipefail

DOMAIN=${DOMAIN:-fs-api.web3db.org}
SGX_HOST=${SGX_HOST:-tjws-06.cse.nd.edu}
SGX_DIR=${SGX_DIR:-web3db-fs-backend/sgx}
BACKEND_DIR=${BACKEND_DIR:-web3db-fs-backend}
PROXY_HOST=${PROXY_HOST:-web3db-ec2}
STREAM_CONF=${STREAM_CONF:-/etc/nginx/streams-enabled/fs-split.conf}
SLOT_A_PORT=8090
SLOT_B_PORT=8092
HERE="$(cd "$(dirname "$0")" && pwd)"

live_port() {
    ssh -n "$PROXY_HOST" "sudo grep -oE '$DOMAIN[[:space:]]+127.0.0.1:[0-9]+' $STREAM_CONF" \
        | grep -oE '[0-9]+$'
}

slot_of() { [ "$1" = "$SLOT_A_PORT" ] && echo a || echo b; }
port_of() { [ "$1" = a ] && echo "$SLOT_A_PORT" || echo "$SLOT_B_PORT"; }

running_slots() {
    # gramine's cmdline ends in "init <manifest-name>", so the slot is right there
    ssh -n "$SGX_HOST" 'pgrep -af "sgx/[l]oader" | sed "s|.*/loader .*init |  |"' 2>/dev/null || true
}

flip_to() {
    local port=$1
    ssh -n "$PROXY_HOST" "sudo sed -i -E 's|($DOMAIN[[:space:]]+)127.0.0.1:[0-9]+|\\1127.0.0.1:$port|' $STREAM_CONF \
        && sudo nginx -t >/dev/null && sudo systemctl reload nginx"
}

LIVE_PORT=$(live_port)
LIVE_SLOT=$(slot_of "$LIVE_PORT")
IDLE_SLOT=$([ "$LIVE_SLOT" = a ] && echo b || echo a)
IDLE_PORT=$(port_of "$IDLE_SLOT")

case "${1:-}" in
--status)
    echo "live slot:  $LIVE_SLOT (port $LIVE_PORT)"
    echo "idle slot:  $IDLE_SLOT (port $IDLE_PORT)"
    echo "enclaves running on $SGX_HOST:"
    running_slots
    echo -n "public endpoint: "
    curl -s -o /dev/null -w "%{http_code}\n" --max-time 10 "https://$DOMAIN/storage-stats"
    exit 0
    ;;
--rollback)
    echo "flipping $LIVE_SLOT -> $IDLE_SLOT (must already be running)"
    ssh -n "$SGX_HOST" "curl -sk --max-time 5 https://127.0.0.1:$IDLE_PORT/storage-stats" >/dev/null \
        || { echo "slot $IDLE_SLOT is not serving — nothing to roll back to"; exit 1; }
    flip_to "$IDLE_PORT"
    curl -s -o /dev/null -w "public endpoint now: %{http_code}\n" --max-time 15 "https://$DOMAIN/storage-stats"
    exit 0
    ;;
esac

CONFIRM=ask
[ "${1:-}" = "--yes" ] && CONFIRM=yes
STAGE_ONLY=no
[ "${1:-}" = "--stage" ] && STAGE_ONLY=yes

echo "live: slot $LIVE_SLOT ($LIVE_PORT) — deploying to slot $IDLE_SLOT ($IDLE_PORT)"

echo "== 1/7  pull and render manifests"
ssh -n "$SGX_HOST" "cd $BACKEND_DIR && git pull --ff-only origin develop >/dev/null && \
    mkdir -p thumbs-a thumbs-b && cd sgx && make web3fs-$IDLE_SLOT.manifest TLS=1 >/dev/null"

echo "== 2/7  sign on the key host"
SGX_DIR="$SGX_DIR" "$HERE/sign-remote.sh" "web3fs-$IDLE_SLOT" | grep MRENCLAVE

echo "== 3/7  boot slot $IDLE_SLOT (the live slot keeps serving)"
# Idempotent, so --stage followed by --yes doesn't start a second copy.
ssh -n "$SGX_HOST" "if curl -sk --max-time 5 https://127.0.0.1:$IDLE_PORT/storage-stats | grep -q ipfs; then \
        echo '  already serving — reusing it'; \
    else cd $SGX_DIR && \
        (nohup gramine-sgx web3fs-$IDLE_SLOT > ../logs/gramine-$IDLE_SLOT.log 2>&1 </dev/null &) && \
        echo '  started'; fi"

echo "== 4/7  wait for it to serve (~5 min)"
ssh -n "$SGX_HOST" "for i in \$(seq 1 16); do sleep 30; \
    if curl -sk --max-time 5 https://127.0.0.1:$IDLE_PORT/storage-stats | grep -q ipfs; then \
        echo \"  slot $IDLE_SLOT serving after ~\$((i*30))s\"; exit 0; fi; done; \
    echo '  slot did not come up'; exit 1" \
    || { echo "aborting; nothing was flipped, slot $LIVE_SLOT still live"; exit 1; }

echo "== 5/7  make sure the proxy can reach it"
# Slot a's tunnel is the container's (tunnel.yml); slot b needs its own.
# Ask the proxy whether the forward exists rather than grepping for the ssh
# process: a pattern describing the tunnel also appears in the command line
# that creates it, so pgrep matches itself and skips the work.
if ssh -n "$PROXY_HOST" "ss -tln | grep -q '127.0.0.1:$IDLE_PORT '"; then
    echo "  tunnel already up"
else
    ssh -n "$SGX_HOST" "ssh -i ~/key/ec2.pem -o StrictHostKeyChecking=no \
        -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
        -f -N -R $IDLE_PORT:localhost:$IDLE_PORT ubuntu@proxy.web3db.org"
    echo "  tunnel started"
fi
ssh -n "$PROXY_HOST" "curl -sk --max-time 8 https://127.0.0.1:$IDLE_PORT/storage-stats" >/dev/null \
    || { echo "proxy cannot reach slot $IDLE_SLOT — aborting, slot $LIVE_SLOT still live"; exit 1; }

if [ "$STAGE_ONLY" = yes ]; then
    echo
    echo "staged: slot $IDLE_SLOT is up and reachable, slot $LIVE_SLOT still live."
    echo "flip when ready:  ./deploy.sh --yes   (skips straight to the flip"
    echo "                  since the slot is already serving)"
    exit 0
fi

if [ "$CONFIRM" = ask ]; then
    echo
    [ -t 0 ] || { echo "not a terminal: re-run with --yes"; exit 1; }
    read -r -p "Flip live traffic to slot $IDLE_SLOT? [y/N] " a
    [ "$a" = y ] || [ "$a" = Y ] || { echo "not flipped; slot $IDLE_SLOT left running for inspection"; exit 1; }
fi

echo "== 6/7  flip"
flip_to "$IDLE_PORT"
curl -s -o /dev/null -w "  public endpoint: %{http_code}\n" --max-time 15 "https://$DOMAIN/storage-stats"

echo "== 7/7  retire slot $LIVE_SLOT"
# Only now, so a bad flip can be undone with --rollback while the old
# enclave is still warm.
read -r -t 30 -p "  stop the old slot now? [Y/n] (auto-yes in 30s) " a || a=y
if [ "${a:-y}" != n ] && [ "${a:-y}" != N ]; then
    # bracket keeps the pattern from matching this command's own line
    ssh -n "$SGX_HOST" "pkill -f 'init web3fs-[$LIVE_SLOT]' && echo '  stopped' || echo '  was not running'"
else
    echo "  left running — stop it later, or use --rollback to go back to it"
fi
