# Besu Node Operations Runbook

> Applies to: `web3db-fs-backend/besu/` (QBFT private network — 4 local nodes + 2 remote = 6 validators)
> Source of truth: [Besu QBFT docs](https://besu.hyperledger.org/private-networks/how-to/configure/consensus/qbft),
> [Add/remove validators without voting](https://besu.hyperledger.org/private-networks/how-to/configure/consensus/add-validators-without-voting),
> [Validators in production](https://besu.hyperledger.org/private-networks/how-to/configure/validators)

Procedures for adding nodes, voting a validator in/out, shutting down, and
replacing a validator — all without breaking QBFT liveness.

---

## 1. Network facts (read first)

| Item | Value |
|------|-------|
| Consensus | QBFT (block-header validator selection) |
| Chain ID | `20260928` |
| Block period | 2 s |
| Epoch length | 150 blocks (~5 min) |
| Request timeout | 4 s |
| Gas | `zeroBaseFee: true`, gas price `0` (no ETH needed) |
| Validators | 6 (node1–node4 local + 2 remote, §1.1) |
| Discovery | off (`--discovery-enabled=false`) — peers join via `static-nodes.json` |

| Node | Internal IP | Validator address | Host p2p |
|------|-------------|-------------------|----------|
| node1 | `172.16.42.11` | `0xcea03f9d9683dc123d956500fae2398d15cfc84b` | `129.74.154.215:30313` |
| node2 | `172.16.42.12` | `0xf06729f077fcfc6d8548f8b660ae73696e5f8287` | `129.74.154.215:30323` |
| node3 | `172.16.42.13` | `0xdb0c36e1e733a3eed1f539f1bb7fef6094791aa3` | `129.74.154.215:30333` |
| node4 | `172.16.42.14` | `0xaeaa30744ad4d1012e6deb52887f3e7376f1ded8` | `129.74.154.215:30343` |

Two further validators were voted in **after** genesis and run off-host (not
managed by this repo's `compose.yaml`):

| Remote validator address |
|--------------------------|
| `0x19b85ff7fde6cc7423a994ea49007de7e3a2347c` |
| `0xdf8cdc35c0b2ac42523d475eeab3e50dc140f1ab` |

`static-nodes.json` lists one off-host peer (`129.74.152.201:30303`), so the
network already spans machines.

### RPC access (verified)

All four nodes run `--rpc-http-api=ETH,NET,WEB3,QBFT` (plus
`--rpc-http-host=0.0.0.0`), so every `qbft_*` method is live on each node's
`:8545`. **But** `--host-allowlist=localhost,127.0.0.1,node1` rejects any request
whose `Host` header isn't allowlisted — which is why a plain
`curl http://172.16.42.12:8545` fails. Two working access paths:

```bash
# 1) node1 — the only node whose RPC is host-mapped. Use this for read-only queries.
curl -s -X POST -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' \
  http://127.0.0.1:8545

# 2) node2–node4 — not host-mapped; reach them through the besu network with an
#    allowlisted Host header (a throwaway alpine container; alpine is already local).
docker run --rm --network dsp-besu_besu alpine:latest wget -qO- \
  --header='Host: localhost' --header='Content-Type: application/json' \
  --post-data='{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' \
  http://172.16.42.<N>:8545        # N = 2,3,4
```

(The Besu containers have no `curl`/`wget`, so exec-ing in and curling won't work;
the throwaway-container pattern above is the reliable path to nodes 2–4.)

---

## 2. The one rule that keeps the network alive

QBFT seals a block only with a **≥ 2/3 super-majority** of validators signing — with
6 validators that means **4 must sign**, so **2 can be down** at once. Besu's rule of
thumb: never let more than **1/3** of validators stop participating, or **the network
stalls** (no new blocks, slow to recover). (Besu's stricter `f = floor((n-1)/3)` bound
applies to *malicious/byzantine* validators, not merely offline ones.)

| Validators | Max down at once | Min needed to seal |
|-----------:|-----------------:|--------------------|
| 4 | 1 | 3 |
| 5 | 1 | 4 |
| 6 (current) | 2 | 4 |
| 7 | 2 | 5 |

> **Hard rule for this network (6 validators): never drop below 4 signers at once**
> (max 2 down). Do validator *changes* in the order "add first, remove second" so the
> count never dips below 6 (§7).

---

## 3. Inspect state before/after any change

```bash
# Who are the validators right now?
curl -s -X POST --data '{"jsonrpc":"2.0","method":"qbft_getValidatorsByBlockNumber","params":["latest"],"id":1}' http://127.0.0.1:8545

# Who is actually signing blocks (shows offline validators)?
curl -s -X POST --data '{"jsonrpc":"2.0","method":"qbft_getSignerMetrics","params":[],"id":1}' http://127.0.0.1:8545

# Connected peers and chain height
curl -s -X POST --data '{"jsonrpc":"2.0","method":"net_peerCount","params":[],"id":1}' http://127.0.0.1:8545
curl -s -X POST --data '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' http://127.0.0.1:8545
```

A node that is a validator but has a big gap since its last block in
`qbft_getSignerMetrics` is a faulting candidate.

---

## 4. Adding a new node (network peer first, validator later)

A new node joins in two stages: **peer** (it connects + syncs), then **validator**
(§5 votes it in). Do not vote in a node that hasn't finished syncing.

### 4.1 Generate the node key

A node's *node key* is its identity — for a QBFT validator, the node key's
address **is** the validator address. Create it before starting the node:

```bash
cd besu
mkdir -p nodes/node5/data

# Besu creates <data-path>/key on first run; export the address + public key.
docker run --rm -v "$(pwd)/nodes/node5/data:/data" hyperledger/besu:26.9.0 \
  --data-path=/data public-key export-address

docker run --rm -v "$(pwd)/nodes/node5/data:/data" hyperledger/besu:26.9.0 \
  --data-path=/data public-key export
```

- `export-address` → the node/validator address (starts `0x`).
- `export` → the 128-hex-char public key used to build the enode.
- `nodes/node5/data/key` is the **private key** — keep it out of git.

> Secret hygiene: `besu/.gitignore` already ignores `nodes/` and `networkFiles/`,
> so the key stays local. Do not `git add -f`.

### 4.2 Connect it across machines

Discovery is off, so peers only connect to `static-nodes.json`. Build the enode:

```
enode://<128-hex-public-key>@<reachable-ip>:30303
```

1. **New node's own `static-nodes.json`** lists the *existing* nodes it should
   dial out to, e.g. `enode://…@129.74.154.215:30313` (or `172.16.42.11:30303`
   if it's on the same docker network).
2. **Existing nodes' `static-nodes.json`** get the new node's enode added, so
   they accept/route to it.
3. If the new node lives on a different machine, open/publish a p2p port there
   (see how `129.74.154.215:30313→30303` maps for node1) and make sure the new
   machine can reach an existing node's published p2p port.

> No-restart alternative: enable `ADMIN` in `--rpc-http-api` and use
> `admin_addPeer` / `admin_removePeer`. This network has `ADMIN` off, so the
> documented path is restarting with an updated `static-nodes.json`.

### 4.3 Start it

Add a `node5` service to `compose.yaml` mirroring node1–node4 (new IP
`172.16.42.15`, new host port, `--node-private-key-file=/var/lib/besu/key`
pointing at `nodes/node5/data`), then:

```bash
docker compose up -d node5
docker compose logs -f node5      # wait for sync
```

Confirm it peers (`net_peerCount` goes up) and reaches `eth_blockNumber` ≈ the
others before voting it in.

---

## 5. Voting a new node IN as a validator

1. Grab the new node's **address** from §4.1 (`export-address`).
2. Propose the vote on a **strict majority** of the current validators — with 6
   validators that means **4 of them**. A vote only counts on the node you send it
   to. All 4 local nodes are a strict majority, so you can reach the threshold
   without touching the remote validators:

```bash
VOTE='{"jsonrpc":"2.0","method":"qbft_proposeValidatorVote","params":["0x<NEW_ADDRESS>",true],"id":1}'

# node1 — host-mapped RPC
curl -s -X POST -H 'Content-Type: application/json' --data "$VOTE" http://127.0.0.1:8545

# node2 / node3 / node4 — via the besu network (see §1 RPC access)
for ip in 172.16.42.12 172.16.42.13 172.16.42.14; do
  docker run --rm --network dsp-besu_besu alpine:latest wget -qO- \
    --header='Host: localhost' --header='Content-Type: application/json' \
    --post-data="$VOTE" "http://$ip:8545" >/dev/null
done          # 4/6 votes — validator joins at the next epoch
```

3. The validator set only changes at the **next epoch boundary** (every 150
   blocks ≈ 5 min). Wait, then verify:

```bash
curl -s -X POST --data '{"jsonrpc":"2.0","method":"qbft_getValidatorsByBlockNumber","params":["latest"],"id":1}' http://127.0.0.1:8545
# the new address must now appear; signer metrics should soon show it producing blocks
```

Mistake? Discard your **own** pending vote on the node(s) you proposed from before it lands:

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","method":"qbft_discardValidatorVote","params":["0x<NEW_ADDRESS>"],"id":1}' \
  http://127.0.0.1:8545
```

---

## 6. Voting a validator OUT (graceful removal)

Same mechanism, `false` instead of `true`, on a strict majority (4 of 6). To
remove node4, send the vote from at least 4 validators (voting node4 out via its
own RPC is allowed — it's a plain proposal until the epoch applies it):

```bash
VOTE='{"jsonrpc":"2.0","method":"qbft_proposeValidatorVote","params":["0x<NODE4_ADDRESS>",false],"id":1}'
curl -s -X POST -H 'Content-Type: application/json' --data "$VOTE" http://127.0.0.1:8545   # node1
for ip in 172.16.42.12 172.16.42.13 172.16.42.14; do
  docker run --rm --network dsp-besu_besu alpine:latest wget -qO- \
    --header='Host: localhost' --header='Content-Type: application/json' \
    --post-data="$VOTE" "http://$ip:8545" >/dev/null
done          # node2+node3+node4 → 4/6 → removed at next epoch
```

This works whether the target is **up or down** — only the *remaining* validators
need to vote. The catch: with 6 validators you must keep ≥ 4 live to seal while
the vote is pending. If you're already down to 3 live, you cannot vote out — go to
§8 (transitions fallback).

After the epoch transition, stop the node if desired (§7.1) and remove it from
`static-nodes.json` and `compose.yaml`.

---

## 7. Shutdown & replacement protocols

### 7.1 Shutting a node down (safe)

- **Non-validator / not-yet-voted-in node** — just `docker compose stop node5`.
- **Validator node** — with 6 validators you may take **2** down without stalling
  (4 of 6 still seal). Never let it drop below 4 signers.

```bash
docker compose stop node4    # OK: 5 remain → still ≥ 4 signers
docker compose start node4   # it re-syncs and rejoins
```

To take a validator **permanently** out, vote it out (§6) first, then stop it —
otherwise the network runs degraded.

### 7.2 Replacing a validator (zero downtime — the right order)

**Add first, then remove.** This keeps ≥ 6 validators the whole time and never
risks a stall:

1. Stand up + sync the replacement node (§4).
2. Vote the replacement **in** (§5) → validator count goes 6 → 7.
3. Verify it is signing (`qbft_getSignerMetrics`) and `qbft_getValidatorsByBlockNumber` shows 7.
4. Vote the old validator **out** (§6) → 7 → 6.
5. Stop + decommission the old node, update `static-nodes.json`/`compose.yaml`.

---

## 8. Recovery: network stalled (majority of validators down)

If > 1/3 validators are down, voting is impossible (you can't reach the 2/3
needed to seal a block, let alone vote). Use a **genesis transition** to hard-set
the validator set without any vote, then do a rolling restart.

1. In the genesis config, add a `transitions` object as a **sibling of `qbft`**
   (both inside `config`), with a **future** block number and the surviving
   validator addresses. Add the same block to `besu/qbft-config.json` (the
   source) and the generated `networkFiles/genesis.json`:

```json
"config": {
  "chainId": 20260928,
  "zeroBaseFee": true,
  "qbft": {
    "blockperiodseconds": 2,
    "epochlength": 150,
    "requesttimeoutseconds": 4
  },
  "transitions": {
    "qbft": [
      { "block": <FUTURE_BLOCK>, "validators": ["0x…", "0x…", "0x…", "0x…"] }
    ]
  }
}
```

2. Re-run `besu operator generate-blockchain-config` (or hand-edit
   `networkFiles/genesis.json`) so every node's mounted genesis matches.
3. Rolling-restart **all** nodes so they're up *before* the transition block:

```bash
docker compose up -d --force-recreate node1
# …one at a time, not all at once…
```

4. Verify at/after the block:

```bash
curl -s -X POST --data '{"jsonrpc":"2.0","method":"qbft_getValidatorsByBlockNumber","params":["latest"],"id":1}' http://127.0.0.1:8545
```

> Warnings (from Besu docs): never set the transition block **in the past** — it
> can fork the chain. Transitions leave the override permanently in genesis, so
> this is a *recovery* tool, not the normal add/remove path.

---

## 9. Command cheatsheet

| Goal | RPC / command |
|------|---------------|
| List validators | `qbft_getValidatorsByBlockNumber ["latest"]` |
| Block-signing health | `qbft_getSignerMetrics []` |
| Vote validator **in** | `qbft_proposeValidatorVote ["0x…", true]` |
| Vote validator **out** | `qbft_proposeValidatorVote ["0x…", false]` |
| Cancel a pending vote | `qbft_discardValidatorVote ["0x…"]` |
| Peers / height | `net_peerCount []`, `eth_blockNumber []` |
| Start/stop a node | `docker compose up -d nodeN` / `docker compose stop nodeN` |
| Logs | `docker compose logs -f nodeN` |

## 10. Cross-check against the chain config

- `epochlength: 150` → all validator-set changes land on an epoch boundary,
  **not instantly**; wait ~5 min and re-verify every time.
- `epochlength` must be **larger than the validator count**: QBFT discards all
  pending votes at each epoch transition, so every validator needs a turn to
  propose (and broadcast its vote) within one epoch before votes are tallied.
  Keep `epochlength` ≥ ~2× the validator count. (Verified in isolation: with
  `epochlength: 2` and 4 validators, a 4/4 vote never lands — only 2 validators
  propose per epoch, short of the 3-vote majority.)
- `zeroBaseFee: true` → validators/accounts need **no ETH**; nothing to fund on
  the new node.
- `--discovery-enabled=false` → new peers only join via `static-nodes.json`
  (or `admin_addPeer` if you enable `ADMIN`).
- Node key address = validator address → generate the key **before** voting, and
  keep `nodes/*/data/key` private.