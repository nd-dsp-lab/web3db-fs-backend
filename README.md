# web3db-fs-backend

```bash
cp .env.example .env    # then fill it in
./dev.sh                # development: auto-reloads
sgx/deploy.sh           # production: deploy into the SGX enclave (run from a trusted workstation)
pytest                  # e2e tests skip without an IPFS node
docker compose -f ipfs/docker-compose.yml up -d
```

Logs: `logs/web3fs.log`
