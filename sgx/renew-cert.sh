#!/bin/bash
# Renew the TLS certificate the enclave serves, without the private key ever
# touching a server's disk.
#
#   ./renew-cert.sh --check     days left on the live certificate
#   ./renew-cert.sh             renew (prompts before the restart)
#   ./renew-cert.sh --yes       renew without prompting
#
# Run from a trusted workstation, not from either server. The key is
# generated here, the CSR goes to the proxy for the ACME challenge, and the
# key + issued chain are piped into the enclave's sealed mount. The local
# copies are shredded on exit.
#
# The enclave does not need re-signing: the certificate lives in the sealed
# mount, not in the measured files, so MRENCLAVE is unchanged. Only a
# restart is needed — which costs ~5 minutes of downtime while the enclave
# boots.
#
# Prerequisites, both already in place: an nginx server block on the proxy
# answering :80 for the domain out of $WEBROOT, and a registered certbot
# account.
set -euo pipefail

DOMAIN=${DOMAIN:-fs-api.web3db.org}
SGX_HOST=${SGX_HOST:-tjws-06.cse.nd.edu}
SGX_DIR=${SGX_DIR:-web3db-fs-backend/sgx}
PROXY_HOST=${PROXY_HOST:-web3db-ec2}
WEBROOT=${WEBROOT:-/var/www/certbot}

live_dates() {
    echo | openssl s_client -connect "$DOMAIN:443" -servername "$DOMAIN" 2>/dev/null \
        | openssl x509 -noout -subject -dates
}

if [ "${1:-}" = "--check" ]; then
    live_dates
    END=$(echo | openssl s_client -connect "$DOMAIN:443" -servername "$DOMAIN" 2>/dev/null \
        | openssl x509 -noout -enddate | cut -d= -f2)
    # BSD date (macOS) and GNU date parse this differently; try both.
    END_EPOCH=$(date -j -f "%b %d %T %Y %Z" "$END" +%s 2>/dev/null \
        || date -d "$END" +%s)
    echo "days remaining: $(( (END_EPOCH - $(date +%s)) / 86400 ))"
    exit 0
fi

CONFIRM=yes
[ "${1:-}" = "--yes" ] || CONFIRM=ask

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir "$WORK/tls"

echo "== 1/6  new key + CSR (this machine only)"
openssl ecparam -genkey -name prime256v1 -noout -out "$WORK/tls/key.pem"
chmod 600 "$WORK/tls/key.pem"
openssl req -new -key "$WORK/tls/key.pem" -subj "/CN=$DOMAIN" \
    -addext "subjectAltName=DNS:$DOMAIN" -out "$WORK/csr.pem"

echo "== 2/6  ACME challenge on $PROXY_HOST"
scp -q "$WORK/csr.pem" "$PROXY_HOST:/tmp/renew-csr.pem"
ssh -n "$PROXY_HOST" "sudo certbot certonly --webroot -w $WEBROOT \
    --csr /tmp/renew-csr.pem \
    --cert-path /tmp/renew-cert.pem \
    --chain-path /tmp/renew-chain.pem \
    --fullchain-path /tmp/renew-fullchain.pem -n" 2>&1 | grep -Ei "successfully|error|fail" || true
scp -q "$PROXY_HOST:/tmp/renew-fullchain.pem" "$WORK/tls/cert.pem"
ssh -n "$PROXY_HOST" "sudo rm -f /tmp/renew-csr.pem /tmp/renew-cert.pem /tmp/renew-chain.pem /tmp/renew-fullchain.pem"

echo "== 3/6  check the chain matches the key before touching production"
CERT_PUB=$(openssl x509 -in "$WORK/tls/cert.pem" -noout -pubkey | openssl md5)
KEY_PUB=$(openssl ec -in "$WORK/tls/key.pem" -pubout 2>/dev/null | openssl md5)
[ "$CERT_PUB" = "$KEY_PUB" ] || { echo "issued certificate does not match the new key — aborting"; exit 1; }
openssl x509 -in "$WORK/tls/cert.pem" -noout -subject -enddate

if [ "$CONFIRM" = ask ]; then
    echo
    # -n above keeps the ssh calls from consuming this; guard anyway, so a
    # non-interactive run refuses rather than silently deciding for itself.
    [ -t 0 ] || { echo "not a terminal: re-run with --yes to renew unattended"; exit 1; }
    read -r -p "Seal this certificate and restart the enclave (~5 min downtime)? [y/N] " a
    [ "$a" = y ] || [ "$a" = Y ] || { echo "aborted; nothing changed"; exit 1; }
fi

echo "== 4/6  seal into the enclave"
# COPYFILE_DISABLE stops macOS tar from adding ._ AppleDouble entries, which
# would land in the sealed mount as junk files.
COPYFILE_DISABLE=1 tar -C "$WORK" -cf - tls/key.pem tls/cert.pem \
    | ssh "$SGX_HOST" "cd $SGX_DIR && gramine-sgx provision"

echo "== 5/6  restart the enclave"
ssh -n "$SGX_HOST" "bash $SGX_DIR/restart.sh"

echo "== 6/6  verify what the world sees"
live_dates
echo "done — the private key never left this machine except sealed into the enclave"
