#!/bin/bash
# Restart the backend inside its SGX enclave: kill whatever holds port 8090,
# then relaunch gramine-sgx detached. Run from anywhere on the SGX host.
#
# Expects the manifests in this directory to be built and signed already
# (sign-remote.sh from the trusted workstation) and the secrets provisioned
# into the sealed mount. Startup takes ~5 minutes — most of it parsing the
# trusted-file manifest — so the health check below polls patiently.
#
# The plain-python restart.sh at the repo root still works for dev hosts;
# this one is what production uses since TLS moved inside the enclave.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
fuser -k 8090/tcp 2>/dev/null
sleep 2
cd "$SCRIPT_DIR"
nohup gramine-sgx web3fs > ../logs/gramine-boot.log 2>&1 &
echo "Enclave starting (PID $!) — polling up to 8 minutes..."
for i in $(seq 1 16); do
    sleep 30
    if curl -sk --max-time 5 https://127.0.0.1:8090/storage-stats | grep -q ipfs; then
        echo "Enclave serving after ~$((i * 30))s"
        exit 0
    fi
done
echo "Enclave did not come up within 8 minutes — check logs/gramine-boot.log" >&2
exit 1
