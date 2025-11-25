from fastapi import FastAPI, UploadFile, Form
import requests
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import io
from web3 import Web3
import json
import os
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import Optional

# Add this class definition here
class TransactionRequest(BaseModel):
    tx_hash: str

# Adding model for share request
class ShareRequest(BaseModel):
    cid: str
    to_address: str
    user_address: str

# Adding model for unshare request
class UnshareRequest(BaseModel):
    cid: str
    to_address: str
    user_address: str

# Adding model for delete request
class DeleteRequest(BaseModel):
    cid: str
    user_address: str
    unpin_after: Optional[bool] = False

# Load environment variables from .env file in smart-contracts folder
env_path = os.path.join(os.path.dirname(__file__), '../smart-contracts', '.env')
load_dotenv(dotenv_path=env_path)

# This will be a simple fastAPI server that acts as an sgx node 
app = FastAPI()


# Allow frontend origin
origins = [
    "http://localhost:3000",
    "http://fs.web3db.org",
    "https://fs.web3db.org",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  #or ["*"] for all origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


print(f"server started listening on port 8090")
IPFS_API_URL = "http://localhost:5001/api/v0"

#connect to web3 via infura --> use environment variable
infura_url = os.getenv("INFURA_URL") or f"https://sepolia.infura.io/v3/{os.getenv('INFURA_API_KEY')}"
w3 = Web3(Web3.HTTPProvider(infura_url))
try:
    if w3.is_connected():
        print("Web3 is connected:", True)
except Exception as e:
    print(f"Warning: Could not verify Web3 connection at startup: {e}")
    print("Web3 will be tested when making transactions")

# load contract from Will's deployed contract
contract_path = os.path.join(os.path.dirname(__file__), '..', 'smart-contracts', 'artifacts', 'contracts', 'FileStorage.sol', 'FileStorage.json')
with open(contract_path) as f:
    abi = json.load(f)["abi"]
if not abi:
    raise Exception("ABI not found")

contract_address = os.getenv("CONTRACT_ADDRESS")
if not contract_address:
    raise Exception("CONTRACT_ADDRESS environment variable not found")
contract = w3.eth.contract(address=contract_address, abi=abi)
if not contract:
    raise Exception("Contract not found")
print("Contract loaded:", contract.address)


# return transaction data for frontend to sign
def prepare_upload_transaction(cid: str, filename: str, folder_path: str, user_address: str):
    try:
        user_address = Web3.to_checksum_address(user_address)
        nonce = w3.eth.get_transaction_count(user_address)
        gas_price = w3.eth.gas_price
        
        # Build transaction but don't sign it
        txn = contract.functions.uploadFile(cid, filename, folder_path).build_transaction({
            'chainId': 11155111,    # required for Sepolia
            'gas': 300000,
            'gasPrice': gas_price,
            'nonce': nonce,
            'from': user_address
        })
        
        return txn
    except Exception as e:
        print("Transaction preparation failed:", e)
        raise

# preparing a share transaction for owner to sign
def prepare_share_transaction(cid: str, to_address: str, user_address: str):
    try:
        user_address = Web3.to_checksum_address(user_address)
        to_address = Web3.to_checksum_address(to_address)
        nonce = w3.eth.get_transaction_count(user_address)
        gas_price = w3.eth.gas_price
 
        # Build transaction but don't sign it
        txn = contract.functions.shareFile(cid, to_address).build_transaction({
            'chainId': 11155111,    # required for Sepolia
            'gas': 300000,
            'gasPrice': gas_price,
            'nonce': nonce,
            'from': user_address
        })
        
        return txn
    except Exception as e:
        print("Share transaction preparation failed:", e)
        raise

# preparing an unshare transaction for owner to sign
def prepare_unshare_transaction(cid: str, to_address: str, user_address: str):
    try:
        user_address = Web3.to_checksum_address(user_address)
        to_address = Web3.to_checksum_address(to_address)
        nonce = w3.eth.get_transaction_count(user_address)
        gas_price = w3.eth.gas_price

        txn = contract.functions.unshareFile(cid, to_address).build_transaction({
            'chainId': 11155111,    # required for Sepolia
            'gas': 300000,
            'gasPrice': gas_price,
            'nonce': nonce,
            'from': user_address
        })

        return txn
    except Exception as e:
        print("Unshare transaction preparation failed:", e)
        raise

# preparing a delete transaction for owner to sign
def prepare_delete_transaction(cid: str, user_address: str):
    try:
        user_address = Web3.to_checksum_address(user_address)
        nonce = w3.eth.get_transaction_count(user_address)
        gas_price = w3.eth.gas_price

        txn = contract.functions.deleteFile(cid).build_transaction({
            'chainId': 11155111,
            'gas': 300000,
            'gasPrice': gas_price,
            'nonce': nonce,
            'from': user_address
        })
        return txn
    except Exception as e:
        print("Delete transaction prep failed:", e)
        raise

# helper to unpin cid and trigger garbage collection on local IPFS node
def unpin_cid(cid: str):
    result = {"cid": cid, "unpin_ok": False, "unpin_response": None, "gc_ok": False, "gc_response": None}

    try:
        # remove pin
        rm_response = requests.post(f"{IPFS_API_URL}/pin/rm", params={"arg": cid})
        result["unpin_response"] = {"status_code": rm_response.status_code, "text": rm_response.text}
        if rm_response.ok:
            result["unpin_ok"] = True
        else:
            result["unpin_ok"] = False

        # trigger garbage collection
        gc_response = requests.post(f"{IPFS_API_URL}/repo/gc")
        result["gc_response"] = {"status_code": gc_response.status_code, "text": gc_response.text}
        result["gc_ok"] = gc_response.ok

    except Exception as e:
        result["error"] = str(e)

    return result


# Register the file to ipfs and get a cid 
@app.post("/upload")
async def upload_file(file: UploadFile, user_address: str = Form(...), folder_path: str = Form("/")):
    # Upload to IPFS first
    files = {"file": (file.filename, await file.read())}
    response = requests.post(f"{IPFS_API_URL}/add", files=files)
    cid = response.json()["Hash"]
    
    # Prepare transaction for frontend to sign
    transaction_data = prepare_upload_transaction(cid, file.filename, folder_path, user_address)
    
    clean_folder_path = folder_path.rstrip('/')
    full_path = f"{clean_folder_path}/{file.filename}" if clean_folder_path else f"/{file.filename}"
    
    return {
        "user": user_address, 
        "cid": cid, 
        "filename": file.filename, 
        "folder_path": full_path,
        "transaction": transaction_data  # Frontend will sign this
    }

# get all the files from the user on the smart contract -> updated to return metadata from new smart contract
@app.get("/")
def get_files(user_address: str = None):
    print("Fetching user files from smart contract...")
    if user_address:
        user_address = Web3.to_checksum_address(user_address)
    user_files = contract.functions.getUserFiles(user_address).call()

    # Need to convert struct to readable format
    structured_files = []
    for file_data in user_files:

        # Cleaning up folder path
        folder_path = file_data[2].rstrip('/')
        full_path = f"{folder_path}/{file_data[1]}" if folder_path else f"/{file_data[1]}"

        structured_files.append({
            "cid": file_data[0],        # cid
            "filename": file_data[1],   # filename
            "folder_path": full_path, # folderPath
            "timestamp": file_data[3],   # timestamp
            "ipfs_url": f"http://localhost:8080/ipfs/{file_data[0]}"    # need specific cid to find in ipfs
        })
    
    print(f"User files: {structured_files}")
    return {"user_files": structured_files}

# New endpoint to verify transaction was successful
@app.post("/verify-upload")
async def verify_upload(request: TransactionRequest):
    try:
        print(f"Waiting for transaction receipt for: {request.tx_hash}")
        receipt = w3.eth.wait_for_transaction_receipt(request.tx_hash, timeout=120)
        
        print(f"Transaction successful!")
        print(f"Block number: {receipt.blockNumber}")
        print(f"Gas used: {receipt.gasUsed}")
        print(f"Status: {receipt.status}")

        response_payload = {
            "success": True, 
            "tx_hash": request.tx_hash,
            "block_number": receipt.blockNumber,
            "gas_used": receipt.gasUsed,
            "status": receipt.status
        }
        
        try: 
            tx = w3.eth.get_transaction(request.tx_hash)
            input_data = tx.input
            func_obj, func_params = contract.decode_function_input(input_data)
            func_name = func_obj.fn_name if hasattr(func_obj, 'fn_name') else func_obj.function_identifier
            response_payload["decoded_function"] = {"name": func_name, "args": func_params}

            # if it's a deleteFile call and tx succeeded -> unpin cid from local IPFS
            if func_name == "deleteFile" and receipt.status == 1:
                cid_unpin = func_params.get("cid") or func_params.get("_cid") or None
                if cid_unpin:
                    print(f"Detected deleteFile for cid {cid_unpin} - unpinning from local IPFS node")
                    unpin_result = unpin_cid(cid_unpin)
                    response_payload["unpin_result"] = unpin_result
                else:
                    response_payload["unpin_result"] = {"error": "Could not find cid in tx params"}
        except Exception as e:
            print(f"Couldn't decode tx input for unpin: {e}")
            response_payload["decoded_function_error"] = str(e)

        return response_payload
    
    except Exception as e:
        print(f"Transaction verification failed: {e}")
        return {"success": False, "error": str(e)}

# endpoint for downloading a file from ipfs
@app.get("/download/{cid}/{filename}")
async def download_file_with_name(cid: str, filename: str):
    try:
        # Get file from IPFS using POST
        response = requests.post(f"{IPFS_API_URL}/cat", params={"arg": cid})
        
        if response.status_code != 200:
            raise Exception(f"Failed to fetch file from IPFS: {response.status_code}")
        
        return StreamingResponse(
            io.BytesIO(response.content),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    
    except Exception as e:
        print(f"Download failed: {e}")
        return {"error": f"Failed to download file: {str(e)}"}

# endpoint for sharing a file (frontend has to sign)
@app.post("/share")
async def share_file(request: ShareRequest):
    try:
        txn = prepare_share_transaction(request.cid, request.to_address, request.user_address)
        return {"transaction": txn}
    except Exception as e:
        print(f"Failed to prepare share transaction: {e}")
        return {"error": str(e)}

# endpoint for unsharing a file (frontend has to sign)
@app.post("/unshare")
async def unshare_file(request: UnshareRequest):
    try:
        txn = prepare_unshare_transaction(request.cid, request.to_address, request.user_address)
        return {"transaction": txn}
    except Exception as e:
        print(f"Failed to prep unshare transaction: {e}")
        return {"error": str(e)}

# endpoint for deleting a file
@app.post("/delete")
async def delete_file(request: DeleteRequest):
    try:
        txn = prepare_delete_transaction(request.cid, request.user_address)
        result = {"transaction": txn}
        return result
    
    except Exception as e:
        print(f"Failed to prep delete transaction: {e}")
        return {"error": str(e)}

@app.get("/shared-users")
def get_shared_users(cid: str):
    try:
        users = contract.functions.getSharedUsers(cid).call()
        return {"cid": cid, "shared_with": users}
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)

