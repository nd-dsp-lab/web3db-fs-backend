from fastapi import FastAPI
import requests
from fastapi.responses import JSONResponse
from web3 import Web3
import json
import os
import uvicorn
from models import FundWalletRequest
from configure import configure_app, IPFS_API_URL, IPFS_GATEWAY_URL, w3, contract
from routers.auth import router as auth_router
from routers.sharing import router as sharing_router
from routers.file_ops import router as file_ops_router
from routers.upload import router as upload_router
from routers.download import router as download_router

# This will be a simple fastAPI server that acts as an sgx node
app = FastAPI()
configure_app(app)  # CORS + other startup steps
app.include_router(auth_router)
app.include_router(sharing_router)
app.include_router(file_ops_router)
app.include_router(upload_router)
app.include_router(download_router)

# CID -> size cache; content is immutable per CID so entries never go stale
_file_size_cache = {}

def get_file_size(cid: str) -> int:
    if cid in _file_size_cache:
        return _file_size_cache[cid]
    try:
        resp = requests.post(f"{IPFS_API_URL}/files/stat?arg=/ipfs/{cid}", timeout=3)
        size = resp.json().get("CumulativeSize", 0) if resp.status_code == 200 else 0
    except Exception:
        size = 0
    if size:  # don't cache failures so they can retry next listing
        _file_size_cache[cid] = size
    return size

# --- Routes --- # (all helper functions in helper.py)

# get all the files from the user on the smart contract -> updated to return metadata from new smart contract
@app.get("/")
async def get_files(user_address: str = None):
    print("Fetching user files from smart contract...")
    if user_address:
        user_address = Web3.to_checksum_address(user_address)
    user_files = contract.functions.getUserFiles(user_address).call()

    # Need to convert struct to readable format
    structured_files = []
    for file_data in user_files:
        cid = file_data[0]
        full_path = file_data[1]
        file_format = file_data[2]
        timestamp = file_data[3]
        
        # Paths may arrive with or without a leading slash (move sends "/a/b",
        # uploads store "a/b") — drop empty segments so both parse the same.
        levels = [p for p in full_path.split("/") if p]

        if len(levels) <= 1:
            filename = levels[0] if levels else full_path
            folder_path = "/"
        else:
            filename = levels[-1]
            folder_path = "/" + "/".join(levels[:-1])

        # get owner and permissions -> this is primarily for later updates to conditionally show buttons (download, share, etc.)
        # Retry once: a single flaky RPC call would otherwise mark the file
        # as not-owned and the UI would misfile it under "Shared with me".
        owner = None
        for attempt in range(2):
            try:
                owner = contract.functions.getFileOwner(cid).call()
                break
            except Exception as e:
                print(f"getFileOwner failed for {cid} (attempt {attempt + 1}): {e}")

        is_owner = (owner is not None and owner.lower() == user_address.lower())

        try:
            permissions = contract.functions.getPermissions(cid, user_address).call()
        except Exception:
            permissions = 0

        # Who the file is shared with (owner only) — drives the Sharing
        # column and the shared-folder icon (folder = intersection of these)
        shared_with = []
        if is_owner:
            try:
                shared_with = contract.functions.getSharedUsers(cid).call()
            except Exception as e:
                print(f"getSharedUsers failed for {cid}: {e}")

        structured_files.append({
            "cid": cid,        # cid
            "filename": filename,   # filename
            "folder_path": folder_path, # temp folderPath
            "file_format": file_format, # fileFormat
            "timestamp": timestamp,   # timestamp
            "owner": owner,
            "is_owner": is_owner,
            "permissions": permissions,
            "shared_with": shared_with,
            "size": get_file_size(cid),
            "ipfs_url": f"{IPFS_GATEWAY_URL}/{cid}"
        })
    
    # print(f"User files: {structured_files}")
    return {"user_files": structured_files}

# Storage capacity for the sidebar usage bar: free disk on the volume backing
# the IPFS repo (via repo/stat when reachable, else this host's disk — the
# Docker volume lives on it anyway).
@app.get("/storage-stats")
def storage_stats():
    import shutil
    stats = {}
    try:
        r = requests.post(f"{IPFS_API_URL}/repo/stat", timeout=5)
        if r.ok:
            j = r.json()
            stats["ipfs_repo_size"] = j.get("RepoSize")
            stats["ipfs_storage_max"] = j.get("StorageMax")
    except Exception as e:
        print(f"repo/stat failed: {e}")
    try:
        du = shutil.disk_usage("/")
        stats["disk_total"] = du.total
        stats["disk_free"] = du.free
    except Exception as e:
        print(f"disk_usage failed: {e}")
    return stats


# --- Wallet funding (gas drip for embedded/email-login wallets) ---
FUND_AMOUNT_ETH = 0.25
FUND_BALANCE_THRESHOLD_ETH = 0.005
FUNDED_ADDRESSES_FILE = os.path.join(os.path.dirname(__file__), "funded_addresses.json")

def _load_funded_addresses() -> set:
    try:
        with open(FUNDED_ADDRESSES_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def _save_funded_address(address: str):
    funded = _load_funded_addresses()
    funded.add(address)
    with open(FUNDED_ADDRESSES_FILE, "w") as f:
        json.dump(sorted(funded), f, indent=2)

@app.post("/fund-wallet")
async def fund_wallet(request: FundWalletRequest):
    try:
        address = Web3.to_checksum_address(request.address)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid address"})

    if address in _load_funded_addresses():
        return {"funded": False, "reason": "Address already funded"}

    balance_eth = w3.from_wei(w3.eth.get_balance(address), "ether")
    if balance_eth >= FUND_BALANCE_THRESHOLD_ETH:
        return {"funded": False, "reason": "Address already has gas"}

    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        return JSONResponse(status_code=500, content={"error": "Funding wallet not configured"})

    faucet = w3.eth.account.from_key(private_key)
    faucet_balance = w3.from_wei(w3.eth.get_balance(faucet.address), "ether")
    if faucet_balance < FUND_AMOUNT_ETH:
        print(f"[fund-wallet] faucet exhausted: {faucet_balance} ETH left")
        return JSONResponse(status_code=503, content={"error": "Funding wallet exhausted"})

    tx = {
        "from": faucet.address,
        "to": address,
        "value": w3.to_wei(FUND_AMOUNT_ETH, "ether"),
        "nonce": w3.eth.get_transaction_count(faucet.address),
        "gas": 21000,
        "gasPrice": w3.eth.gas_price,
        "chainId": w3.eth.chain_id,
    }
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        return JSONResponse(status_code=500, content={"error": "Funding transaction failed"})

    # Record only after confirmed success so a failed drip can be retried
    _save_funded_address(address)
    print(f"[fund-wallet] sent {FUND_AMOUNT_ETH} SepETH to {address}: {tx_hash.hex()}")
    return {"funded": True, "amount_eth": FUND_AMOUNT_ETH, "tx_hash": tx_hash.hex()}

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8090, reload=True)