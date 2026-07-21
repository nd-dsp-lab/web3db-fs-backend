from fastapi import FastAPI
from fastapi.responses import JSONResponse
from web3 import Web3
import json
import os
import uvicorn
from models import FundWalletRequest
from configure import configure_app, w3
from routers.auth import router as auth_router
from routers.sharing import router as sharing_router
from routers.file_ops import router as file_ops_router
from routers.upload import router as upload_router
from routers.download import router as download_router
from routers.files import router as files_router

# This will be a simple fastAPI server that acts as an sgx node
app = FastAPI()
configure_app(app)  # CORS + other startup steps
app.include_router(auth_router)
app.include_router(sharing_router)
app.include_router(file_ops_router)
app.include_router(upload_router)
app.include_router(download_router)
app.include_router(files_router)


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