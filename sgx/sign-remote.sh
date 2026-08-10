#!/bin/bash
# Drive the split SGX signing from the trusted workstation.
#
#   sign-remote.sh web3fs [provision ...]
#
# For each manifest name: measure on the SGX host (sign-split.py prepare),
# sign the blob locally with plain openssl, assemble the .sig back on the
# SGX host (sign-split.py finish). The signing key lives only on this
# workstation (plus an offline backup); neither server ever sees it — the
# SGX host handles public material only.
#
# Overridables:
#   SGX_HOST  SSH host doing the measuring       (default tjws-06.cse.nd.edu)
#   SGX_DIR   dir on SGX_HOST with the rendered .manifest files
#   KEY_FILE  local path to enclave-key.pem      (default ~/key/enclave-key.pem)
set -euo pipefail

SGX_HOST=${SGX_HOST:-tjws-06.cse.nd.edu}
SGX_DIR=${SGX_DIR:-web3fs-sgx-test/sgx}
KEY_FILE=${KEY_FILE:-$HOME/key/enclave-key.pem}

[ $# -ge 1 ] || { echo "usage: $0 <manifest-name>..." >&2; exit 1; }
[ -r "$KEY_FILE" ] || { echo "signing key not found: $KEY_FILE" >&2; exit 1; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# The public half is needed on the SGX host to assemble the SIGSTRUCT.
# Derive it from the key rather than tracking a second file.
openssl rsa -in "$KEY_FILE" -pubout -out "$WORK/enclave-pub.pem" 2>/dev/null
scp -q "$WORK/enclave-pub.pem" "$SGX_HOST:$SGX_DIR/"

for NAME in "$@"; do
    echo "== $NAME: measure on $SGX_HOST"
    ssh "$SGX_HOST" "cd $SGX_DIR && python3 sign-split.py prepare --name $NAME"

    echo "== $NAME: sign locally"
    scp -q "$SGX_HOST:$SGX_DIR/$NAME.tbs" "$WORK/"
    openssl dgst -sha256 -sign "$KEY_FILE" -out "$WORK/$NAME.sigbin" "$WORK/$NAME.tbs"

    echo "== $NAME: assemble on $SGX_HOST"
    scp -q "$WORK/$NAME.sigbin" "$SGX_HOST:$SGX_DIR/"
    ssh "$SGX_HOST" "cd $SGX_DIR && python3 sign-split.py finish --name $NAME --sig $NAME.sigbin --pubkey enclave-pub.pem && rm $NAME.sigbin $NAME.tbs $NAME.tbs.json"
done
