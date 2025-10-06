from fastapi import FastAPI, UploadFile, Form
import requests
from fastapi.middleware.cors import CORSMiddleware
from web3 import Web3
import json
import os

# This will be a simple fastAPI server that acts as an sgx node 
app = FastAPI()


# Allow frontend origin
origins = [
    "http://localhost:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  #or ["*"] for all origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


print(f"server started listening on port 8000")
IPFS_API_URL = "http://localhost:5001/api/v0"

#connect to smart contract 
w3 = Web3(Web3.HTTPProvider("enter your sepolia node url here"))
with open("./../smart-contracts/artifacts/contracts/FileStorage.sol/FileStorage.json") as f:
    abi = json.load(f)["abi"]
contract = w3.eth.contract(address='enter contract address', abi=abi)
my_address = "enter your address here"
private_key = "enter your private key here"

# call smart contract
async def call_smart_contract(cid: str):
    # connect to local Ethereum node
    nonce = w3.eth.get_transaction_count(my_address)
    txn = contract.functions.uploadFile(cid).build_transaction({
        'chainId': 11155111,
        'gas': 70000,
        'gasPrice': w3.to_wei('1', 'gwei'),
        'nonce': nonce,
    })
    signed_txn = w3.eth.account.sign_transaction(txn, private_key=private_key)
    tx_hash = w3.eth.send_raw_transaction(signed_txn.rawTransaction)
    print(f"Transaction hash: {tx_hash.hex()}")
    return tx_hash.hex()


# Register the file to ipfs and get a cid 
@app.post("/upload")
async def upload_file(file: UploadFile, user_address: str = Form(...)):
    # send the file to IPFS
    files = {"file": (file.filename, await file.read())}
    response = requests.post(f"{IPFS_API_URL}/add", files=files)
    cid = response.json()["Hash"]
    tx_hash = await call_smart_contract(cid)
    # return the cid and user address
    return {"user": user_address, "cid": cid, "tx_hash": tx_hash}

