#!/bin/bash
# Drive the split SGX signing from a trusted workstation (not the SGX host).
#
#   sign-remote.sh web3fs [provision ...]
#
# For each manifest name: measure on the SGX host (sign-split.py prepare),
# sign the blob on the key host with plain openssl, assemble the .sig back on
# the SGX host (sign-split.py finish). The signing key never leaves the key
# host; the SGX host never sees it.
#
# Overridables:
#   SGX_HOST  SSH host doing the measuring       (default tjws-06.cse.nd.edu)
#   KEY_HOST  SSH host holding enclave-key.pem   (default web3db-ec2)
#   SGX_DIR   dir on SGX_HOST with the rendered .manifest files
#   KEY_DIR   dir on KEY_HOST with enclave-key.pem / enclave-pub.pem
set -euo pipefail

SGX_HOST=${SGX_HOST:-tjws-06.cse.nd.edu}
KEY_HOST=${KEY_HOST:-web3db-ec2}
SGX_DIR=${SGX_DIR:-web3fs-sgx-test/sgx}
KEY_DIR=${KEY_DIR:-enclave-signing}

[ $# -ge 1 ] || { echo "usage: $0 <manifest-name>..." >&2; exit 1; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# The public half is needed on the SGX host to assemble the SIGSTRUCT.
scp -q "$KEY_HOST:$KEY_DIR/enclave-pub.pem" "$WORK/"
scp -q "$WORK/enclave-pub.pem" "$SGX_HOST:$SGX_DIR/"

for NAME in "$@"; do
    echo "== $NAME: measure on $SGX_HOST"
    ssh "$SGX_HOST" "cd $SGX_DIR && python3 sign-split.py prepare --name $NAME"

    echo "== $NAME: sign on $KEY_HOST"
    scp -q "$SGX_HOST:$SGX_DIR/$NAME.tbs" "$WORK/"
    scp -q "$WORK/$NAME.tbs" "$KEY_HOST:/tmp/"
    ssh "$KEY_HOST" "openssl dgst -sha256 -sign $KEY_DIR/enclave-key.pem -out /tmp/$NAME.sigbin /tmp/$NAME.tbs && rm /tmp/$NAME.tbs"
    scp -q "$KEY_HOST:/tmp/$NAME.sigbin" "$WORK/"
    ssh "$KEY_HOST" "rm /tmp/$NAME.sigbin"

    echo "== $NAME: assemble on $SGX_HOST"
    scp -q "$WORK/$NAME.sigbin" "$SGX_HOST:$SGX_DIR/"
    ssh "$SGX_HOST" "cd $SGX_DIR && python3 sign-split.py finish --name $NAME --sig $NAME.sigbin --pubkey enclave-pub.pem && rm $NAME.sigbin $NAME.tbs $NAME.tbs.json"
done
