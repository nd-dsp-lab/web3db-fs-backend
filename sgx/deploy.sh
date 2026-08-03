#!/bin/bash
# Deploy the backend to the SGX host, end to end. Run from the trusted
# workstation (the machine that can reach both the SGX host and the key
# host) — never on the SGX host itself: signing is split so the enclave
# key stays on the key host.
#
#   sgx/deploy.sh
#
# Steps:
#   1. fast-forward the checkout on the SGX host
#   2. re-render the manifest from scratch (rm first — the rendered manifest
#      embeds every trusted-file hash, so an mtime-based `make` happily
#      reuses hashes of files a pull just changed and the enclave then
#      refuses to open them at boot)
#   3. split-sign via sign-remote.sh (measure on SGX host, sign on key host)
#   4. restart the enclave and wait for the health poll (~5 min boot)
#
# Overridables (defaults are production):
#   SGX_HOST   SSH host running the enclave  (default shossain@tjws-06.cse.nd.edu)
#   REPO_DIR   backend checkout on SGX_HOST  (default web3db-fs-backend)
#   BRANCH     branch to deploy              (default develop)
#   HOST/PORT  uvicorn bind                  (default 127.0.0.1:8090)
#   TLS        1 = HTTPS from sealed certs   (default 1)
set -euo pipefail

SGX_HOST=${SGX_HOST:-shossain@tjws-06.cse.nd.edu}
REPO_DIR=${REPO_DIR:-web3db-fs-backend}
BRANCH=${BRANCH:-develop}
HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8090}
TLS=${TLS:-1}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "== pull $BRANCH on $SGX_HOST"
ssh "$SGX_HOST" "cd $REPO_DIR && git pull --ff-only origin $BRANCH"

echo "== re-render manifest (forced — rendered manifest embeds trusted-file hashes)"
ssh "$SGX_HOST" "cd $REPO_DIR/sgx && rm -f web3fs.manifest web3fs.manifest.sgx web3fs.sig web3fs.mrenclave && make direct HOST=$HOST PORT=$PORT TLS=$TLS >/dev/null"

echo "== verify render matches the checkout"
ssh "$SGX_HOST" "cd $REPO_DIR && for f in \$(git ls-files 'app/*.py'); do
    sha256sum \$f | cut -d' ' -f1
done | sort -u > /tmp/deploy-hashes && ok=1 && while read h; do
    grep -q \$h sgx/web3fs.manifest || { echo \"stale hash for a tracked app file: \$h\" >&2; ok=0; }
done < /tmp/deploy-hashes && rm /tmp/deploy-hashes && [ \$ok = 1 ]" \
    || { echo "rendered manifest does not match the checkout — aborting before sign" >&2; exit 1; }

echo "== split-sign"
SGX_HOST="$SGX_HOST" SGX_DIR="$REPO_DIR/sgx" "$SCRIPT_DIR/sign-remote.sh" web3fs

echo "== restart enclave (up to 8 min)"
ssh "$SGX_HOST" "$REPO_DIR/sgx/restart.sh"

echo "== deployed: MRENCLAVE"
ssh "$SGX_HOST" "cat $REPO_DIR/sgx/web3fs.mrenclave 2>/dev/null || true"
