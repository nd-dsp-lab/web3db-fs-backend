# web3db-fs-backend

```bash
cp .env.example .env    # then fill it in
./dev.sh                # development: auto-reloads
./restart.sh            # production
pytest                  # e2e tests skip without an IPFS node
docker compose -f ipfs/docker-compose.yml up -d
```

Logs: `logs/web3fs.log`
