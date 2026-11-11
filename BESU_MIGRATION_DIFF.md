# Besu Migration — Changes to the Original (`web3db-fs-backend`)

> **Task:** "Make a md file for the changes to the original" (Oct 28 meeting)
> **Scope:** Besu only. Sepolia → Besu. All other layers (SGX, IPFS, NAT) are out of scope.
> **Baseline compared:** `origin/develop` of `nd-dsp-lab/web3db-fs-backend`
> **Date:** 2026-11-04

This documents, end to end, what changed to move the backend onto a private
Hyperledger Besu chain, and what still needs to be done. It pairs with the lab's
onboarding **Package 1 (Smart Contracts, Ethereum & Hyperledger Besu)**, which is
the authoritative source for the assignment.

***

## 1. The assignment in one paragraph

Web3FS currently deploys its contracts to **Sepolia** (public testnet via Infura).
Sepolia costs test ETH, publishes every operation to the world, and is not a chain
we control. The task is to replace it with **Hyperledger Besu** — a permissioned,
EVM-compatible chain where we write the genesis block, fund accounts ourselves
(no faucet), and set **gas price to zero**.

The standing instruction: **find every place the code points at Sepolia and point
it at Besu** — endpoint URLs, chain IDs, contract addresses, ABI files.

***

## 2. Net change set (`git status --porcelain`)

```
 M app/helpers.py
 M smart-contracts/hardhat.config.js
?? .venv-linux/
?? besu/
```

Only **two tracked files** are modified. The rest is the new Besu network
definition plus a stray virtualenv (see §7).

***

## 3. Sepolia → Besu, file by file

### 3.1 `app/helpers.py`

The gas-price and chain-id logic is rewritten. **Before (Sepolia)** vs **after (Besu)**:

```diff
-sepolia_chain_id = 11155111

-# Price gas 25% above the node's quote. The quote lags the network (and
-# Infura can serve stale reads), which left transactions stuck in the mempool.
+# Free-gas network: the chain runs with zeroBaseFee, so transactions carry a
+# gas price of 0 and accounts do not need ETH to transact.
 def _gas_price():
-    return int(w3.eth.gas_price * 1.25)
+    return 0
```

```diff
     tx = {
-        'chainId': sepolia_chain_id,    # required for Sepolia
+        'chainId': w3.eth.chain_id, # not sepolia anymore
         'nonce': w3.eth.get_transaction_count(addr) + nonce_offset,
         'from': addr,
     }
```

```diff
         txns.append(fn.build_transaction({
-            'chainId': sepolia_chain_id,
+            'chainId': w3.eth.chain_id, # not sepolia anymore
             'gasPrice': _gas_price(),
```

**What changed, precisely**

| Concern   | Sepolia (original)                     | Besu (local)                |
| --------- | -------------------------------------- | --------------------------- |
| Gas price | `w3.eth.gas_price * 1.25` (25% margin) | `0` (free gas)              |
| Chain id  | `11155111` hardcoded                   | `w3.eth.chain_id` (dynamic) |

Chain id is read from the node now, so the same code follows whatever chain the
app is pointed at (`20260928` here).

### 3.2 `smart-contracts/hardhat.config.js`

Adds a `besu` network and loads the Besu env file. The `sepolia` entry is left
intact — the change is additive.

```diff
 dotenv.config({ path: path.resolve(__dirname, "../.env") });
+dotenv.config({ path: path.resolve(__dirname, "../.env.besu") });
```

```diff
   networks: {
       hardhat: {},
+      besu: {
+        url: "http://127.0.0.1:8545",
+        chainId: 20260928,
+        accounts: process.env.BESU_DEPLOYER_PRIVATE_KEY
+          ? [process.env.BESU_DEPLOYER_PRIVATE_KEY]
+          : [],
+      },
       sepolia: {
         url: `https://sepolia.infura.io/v3/${process.env.INFURA_API_KEY}`,
         accounts: [process.env.PRIVATE_KEY ? `0x${process.env.PRIVATE_KEY}` : undefined].filter(Boolean)
       }
```

***

## 4. The new Besu network (`besu/`, untracked)

A QBFT network whose repo config defines **4 local nodes**. (The live network has
since been extended to **6 validators** — 2 more voted in and running off-host —
see the companion `runbook.md` §1.) Files that should be committed (generated
keypairs/DB are excluded via `besu/.gitignore`):

| File                     | Purpose                                                  |
| ------------------------ | -------------------------------------------------------- |
| `besu/compose.yaml`      | `docker compose` for the 4 local nodes (`hyperledger/besu:26.9.0`) |
| `besu/qbft-config.json`  | Genesis definition (chain id, consensus, pre-funds)      |
| `besu/static-nodes.json` | Peer `enode` list (discovery disabled)                   |
| `besu/.gitignore`        | Ignores `networkFiles/` and `nodes/`                     |

### Genesis values (`qbft-config.json` → generated `networkFiles/genesis.json`)

```json
"config": {
  "chainId": 20260928,
  "londonBlock": 0,
  "shanghaiTime": 0,
  "cancunTime": 0,
  "zeroBaseFee": true,
  "qbft": {
    "blockperiodseconds": 2,
    "epochlength": 150,
    "requesttimeoutseconds": 4
  }
},
"gasLimit": "0x1c9c380"     // 30,000,000
```

- `zeroBaseFee: true` → base fee is zero → gas price `0`, no ETH needed.
- 4 validators in `extraData` at **genesis** → meets QBFT's minimum (4 validators,
  2/3 liveness). The live set has since grown to 6 via voting (see `runbook.md`).
- `alloc` pre-funds the deployer (`0x5459afF0…`, `BESU_DEPLOYER_ADDRESS`) plus one
  original account → the faucet dependency is gone.

***

## 5. Gas-price and chain-id summary

| State              | Gas price          | Chain id   | Consensus           | Funding         |
| ------------------ | ------------------ | ---------- | ------------------- | --------------- |
| Sepolia (original) | `gas_price * 1.25` | `11155111` | public PoS (Infura) | faucet          |
| Besu (this change) | `0`                | `20260928` | QBFT (4 local nodes in repo; 6 validators live) | genesis `alloc` |

The 25% Septest margin existed only to survive stale Infura quotes on the public
testnet; on a chain we control with `zeroBaseFee`, it is deleted outright.

***

## 6. Cross-check against the lab's Besu checklist (Package 1)

The onboarding doc lists specific "gotchas." Here is how the current config maps,
and **what is still missing**:

| Checklist item                       | Status         | Note                                                                                                                                                                                                                                                                                           |
| ------------------------------------ | -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `zeroBaseFee: true` in genesis       | ✅ present      | `qbft-config.json`                                                                                                                                                                                                                                                                             |
| `gasLimit` in genesis                | ✅ present      | `0x1c9c380` (30M)                                                                                                                                                                                                                                                                              |
| `--min-gas-price=0` on every node    | ⚠️ **missing** | **no node in** **`compose.yaml`** **sets this.** Per Package 1 this is *"the single highest-value gotcha"*: a node with min-gas-price > 0 silently drops zero-gas transactions — no error, the tx just vanishes. **Confirmed absent on all 4 running nodes** (`docker inspect` shows no `--min-gas-price` flag, so the default 1000-wei floor applies). Test a real zero-gas tx before relying on it. |
| `contractSizeLimit` in genesis       | ⚠️ not set     | uses Besu default 24576 B (24 KB), which matches EIP-170's limit. Fine unless a contract nears 24 KB; worth confirming no contract does.                                                                                                                                                       |
| ≥4 validators, 2/3 liveness          | ✅ 6 live | genesis has 4; 2 more voted in off-host (see `runbook.md` §1).                                                                                                                                                                                                                                             |
| Fund accounts in genesis (no faucet) | ✅ `alloc`      | deployer funded.                                                                                                                                                                                                                                                                               |
| Chain id consistent everywhere       | ⚠️ verify      | `20260928` is set in `qbft-config.json`, `genesis.json`, and `hardhat.config.js`. It is intentionally **not** Besu's default `1337`; confirm MetaMask is pointed at `20260928`, not `1337`.                                                                                                    |

> The two `⚠️` "verify" rows (`--min-gas-price=0`, contract size) are the parts
> to confirm against the running node before declaring the migration done.

***

## 7. Before you commit / push

- **`.venv-linux/`** **is untracked and NOT gitignored** (root `.gitignore` matches
  `.venv`/`.venv/`/`venv/`, not `.venv-linux/`). It is a \~222 MB virtualenv; a
  plain `git add -A` will commit it. Add `.venv-linux/` to `.gitignore` first.
- Node secrets stay out: `besu/.gitignore` already excludes `networkFiles/` (node
  keypairs + generated genesis) and `nodes/` (node DB + key). Do **not** `git add -f`.
- `.env.besu` (deployer private key) is ignored via the root `.env.*` rule.
- Runtime wiring is **not** in code: `app/configure.py` still reads the RPC URL
  from `INFURA_URL`. Point it at Besu with `INFURA_URL=http://127.0.0.1:8545` and
  set `CONTRACT_ADDRESS` to a `FileStorage` deployed on chain `20260928`.

### Remaining Besu work (Oct 28 meeting todos)

1. `git push` (current state is uncommitted on `develop`).
2. Write the **runbooks for adding/removing validator nodes**.
3. ~~Make a md file for the changes to the original~~ → this file.
4. **Make a branch**, replace remaining Sepolia calls with Besu calls
   (the code parts in §3 are done; grep for any leftover Sepolia references:
   `INFURA_API_KEY`, `11155111`, `sepolia`, `INFURA_URL` default in `configure.py`).

### Suggested commit shape

- Tracked diffs: `app/helpers.py`, `smart-contracts/hardhat.config.js`.
- New tracked files: `besu/compose.yaml`, `besu/qbft-config.json`,
  `besu/static-nodes.json`, `besu/.gitignore`.
- Add to `.gitignore`: `.venv-linux/`.
- Do **not** commit: `besu/networkFiles/`, `besu/nodes/`, `.env*`, secrets, venv.

