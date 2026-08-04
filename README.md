# web3db-fs-backend

Web3FS backend: FastAPI service storing user files **encrypted** on IPFS with
ownership and sharing recorded in an Ethereum smart contract. In production
the entire service — TLS termination included — runs inside an Intel SGX
enclave, so no host or proxy administrator can see file contents or secrets.

```bash
cp .env.example .env    # then fill it in
./dev.sh                # development: auto-reloads
sgx/deploy.sh           # production: deploy into the SGX enclave (run from a trusted workstation)
pytest                  # e2e tests skip without an IPFS node
docker compose -f ipfs/docker-compose.yml up -d
```

Logs: `logs/web3fs.log`
