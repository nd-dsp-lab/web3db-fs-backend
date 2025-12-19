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
from typing import Optional, List
from permissions import READ, WRITE, DOWNLOAD, DELETE, SHARE, MOVE, CHANGE_OWNER, CHANGE_ROLE

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
# env_path = os.path.join(os.path.dirname(__file__), '../smart-contracts', '.env')
# load_dotenv(dotenv_path=env_path)

# Load environment variables from .env file in web3db-fs-backend folder
env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(dotenv_path=env_path)

# This will be a simple fastAPI server that acts as an sgx node 
app = FastAPI()


# Allow frontend origin
origins = [
    "http://localhost:3000",
    "http://fs.web3db.org",
    "https://fs.web3db.org",
    "https://proxy.web3db.org",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  #or ["*"] for all origins
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
def prepare_upload_transaction(cid: str, full_path: str, user_address: str, file_format: Optional[str] = None):
    print(f"[prepare_upload_transaction] CID={cid}, full_path={full_path}")
    try:
        user_address = Web3.to_checksum_address(user_address)
        nonce = w3.eth.get_transaction_count(user_address)
        gas_price = w3.eth.gas_price
        
        # Build transaction but don't sign it
        txn = contract.functions.uploadFile(cid, full_path, file_format or "").build_transaction({
            'chainId': 11155111,    # required for Sepolia
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
 
        # Ensure ownership
        owner = contract.functions.getFileOwner(cid).call()
        if owner.lower() != user_address.lower():
            raise Exception("Only file owner can share file.")
        
        # Share permissions (READ + DOWNLOAD)
        grant_mask = READ | DOWNLOAD

        print(f"Sharing CID {cid} with {to_address} using mask {grant_mask}")

        # Build transaction but don't sign it
        txn = contract.functions.grant(cid, to_address, grant_mask).build_transaction({
            'chainId': 11155111,    # required for Sepolia
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

        # Ensure ownership
        owner = contract.functions.getFileOwner(cid).call()
        if owner.lower() != user_address.lower():
            raise Exception("Only file owner can unshare file.")

        # Unshare permissions (~READ + ~DOWNLOAD) -> bits are flipped in smart contract
        revoke_mask = READ | DOWNLOAD

        print(f"Unsharing CID {cid} with {to_address} using mask {revoke_mask}")

        txn = contract.functions.revoke(cid, to_address, revoke_mask).build_transaction({
            'chainId': 11155111,    # required for Sepolia
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
async def upload_file(file: UploadFile, user_address: str = Form(...), folder_path: str = Form(""), file_format: Optional[str] = None):
    # Upload to IPFS first
    print(f"Uploading file {file.filename} to IPFS...")
    files = {"file": (file.filename, await file.read())}
    response = requests.post(f"{IPFS_API_URL}/add", files=files)
    cid = response.json()["Hash"]

    # detecting file format if not given (if none detected, leave empty)
    if file_format is None:
        if "." in file.filename:
            file_format = file.filename.split(".")[-1]
        else:
            file_format = ""
    
    # clean folder path
    folder_path = folder_path.strip()

    if folder_path.startswith("/"):
        folder_path = folder_path[1:]
    folder_path = folder_path.rstrip("/")

    # build full path for contract storage
    if folder_path == "":
        full_path = file.filename
    else:
        full_path = f"{folder_path}/{file.filename}"

    print(f"[upload] full_path to send to contract: {full_path}")
    
    # Prepare transaction for frontend to sign
    transaction_data = prepare_upload_transaction(cid, full_path, user_address, file_format)
    
    return {
        "user": user_address, 
        "cid": cid, 
        "filename": file.filename,  # leaf for UI
        "folder_path": "/" + folder_path if folder_path else "/",
        "full_path": full_path,
        "fileformat": file_format,
        "transaction": transaction_data  # Frontend will sign this
    }


@app.post("/upload-folder")
async def upload_folder(
    files: List[UploadFile],
    paths: List[str] = Form(...),
    user_address: str = Form(...)
):
    print(f"Uploading {len(files)} files from folder for {user_address}...")

    uploaded_files = []

    for idx, file in enumerate(files):
        print(idx, file.filename)
        folder_path = paths[idx] if idx < len(paths) else "/"
        print(f"  Uploading {file.filename} to IPFS (folder: {folder_path})")

        # Read file and upload to IPFS
        file_data = await file.read()
        ipfs_response = requests.post(
            f"{IPFS_API_URL}/add",
            files={"file": (file.filename, file_data)}
        )
        if ipfs_response.status_code != 200:
            print(f"Failed to upload {file.filename} to IPFS")
            continue
        print(f"  Uploaded {file.filename} to IPFS")
        ipfs_response.raise_for_status()
        
        # Parse only the last JSON object if multiple exist
        raw_text = ipfs_response.text.strip()
        last_line = raw_text.splitlines()[-1]
        try:
            ipfs_json = json.loads(last_line)
            cid = ipfs_json["Hash"]
        except Exception as e:
            print("Error parsing IPFS response:", e)
            print("Raw IPFS response:", raw_text)
            continue

        # Extract just the filename without the folder path
        actual_filename = file.filename.split('/')[-1]
        print(f"  Actual filename extracted: {actual_filename}")
        
        # Build and prepare blockchain transaction for each file
        full_path = folder_path  # already complete
        print(f"  Preparing upload transaction for {actual_filename} at path {full_path}")
        filename = full_path.split("/")[-1]
        transaction_data = prepare_upload_transaction(
            cid,
            full_path,
            user_address
        )
        uploaded_files.append({
            "cid": cid,
            "filename": actual_filename,  # Use actual_filename here too
            "folder_path": full_path,
            "transaction": transaction_data
        })

    # Return the last file's data (or modify to return all)
    return {
        "user": user_address, 
        "cid": cid, 
        "uploaded_files": uploaded_files, 
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
        cid = file_data[0]
        full_path = file_data[1]
        file_format = file_data[2]
        timestamp = file_data[3]
        
        levels = full_path.split("/")

        if len(levels) == 1:
            filename = levels[0]
            folder_path = "/"
        else:
            filename = levels[-1]
            folder_path = "/" + "/".join(levels[:-1])

        # get owner and permissions -> this is primarily for later updates to conditionally show buttons (download, share, etc.)
        try:
            owner = contract.functions.getFileOwner(cid).call()
        except Exception:
            owner = None
        
        is_owner = (owner is not None and owner.lower() == user_address.lower())

        try:
            permissions = contract.functions.getPermissions(cid, user_address).call()
        except Exception:
            permissions = 0
        
        structured_files.append({
            "cid": cid,        # cid
            "filename": filename,   # filename
            "folder_path": folder_path, # temp folderPath
            "file_format": file_format, # fileFormat
            "timestamp": timestamp,   # timestamp
            "owner": owner,
            "is_owner": is_owner,
            "permissions": permissions,
            "ipfs_url": f"http://localhost:8080/ipfs/{cid}"    # need specific cid to find in ipfs
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
        shared_users = contract.functions.getSharedUsers(cid).call()
        return {"shared_with": shared_users}
    except Exception as e:
        print(f"Error fetching shared users for CID {cid}: {e}")
        return {"shared_with": []}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)