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

#connect to web3 via infura --> use Wills sepolia project
w3 = Web3(Web3.HTTPProvider("enter your infura url here"))
try:
    if w3.is_connected():
        print("Web3 is connected:", True)
except Exception as e:
    print(f"Warning: Could not verify Web3 connection at startup: {e}")
    print("Web3 will be tested when making transactions")

print("Web3 is connected:", w3.is_connected())

# load contract from Will's deployed contract
with open("./../smart-contracts/artifacts/contracts/FileStorage.sol/FileStorage.json") as f:
    abi = json.load(f)["abi"]
if not abi:
    raise Exception("ABI not found")

contract = w3.eth.contract(address='enter your contract address here', abi=abi)
if not contract:
    raise Exception("Contract not found")
print("Contract loaded:", contract.address)

my_address = "enter your wallet address here"
private_key = "enter your private key here"

# call smart contract
def call_smart_contract(cid: str):
    try:
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
    except Exception as e:
        print("Web3 transaction failed:", e)
        raise


# Register the file to ipfs and get a cid 
@app.post("/upload")
async def upload_file(file: UploadFile, user_address: str = Form(...)):
    # send the file to IPFS
    files = {"file": (file.filename, await file.read())}
    response = requests.post(f"{IPFS_API_URL}/add", files=files)
    cid = response.json()["Hash"]
    tx_hash = call_smart_contract(cid)
    # return the cid and user address
    return {"user": user_address, "cid": cid, "tx_hash": tx_hash}

