from fastapi import FastAPI, UploadFile, Form
import requests
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import io
from web3 import Web3
import json
import os
from dotenv import load_dotenv
import os
from pydantic import BaseModel
from typing import List

# Add this class definition here
class TransactionRequest(BaseModel):
    tx_hash: str

# Load environment variables from .env file in smart-contracts folder
env_path = os.path.join(os.path.dirname(__file__), '..', 'smart-contracts', '.env')
load_dotenv(dotenv_path=env_path)

# This will be a simple fastAPI server that acts as an sgx node 
app = FastAPI()


# Allow frontend origin
origins = [
    "http://localhost:3000",
    "http://10.24.214.16:3000",
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

#connect to web3 via infura --> use environment variable
infura_url = os.getenv("INFURA_URL") or f"https://sepolia.infura.io/v3/{os.getenv('INFURA_API_KEY')}"
w3 = Web3(Web3.HTTPProvider(infura_url))
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

contract_address = os.getenv("CONTRACT_ADDRESS")
if not contract_address:
    raise Exception("CONTRACT_ADDRESS environment variable not found")
contract = w3.eth.contract(address=contract_address, abi=abi)
if not contract:
    raise Exception("Contract not found")
print("Contract loaded:", contract.address)


# return transaction data for frontend to sign
def prepare_transaction(cid: str, filename: str, folder_path: str, user_address: str):
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

# Register the file to ipfs and get a cid 
@app.post("/upload")
async def upload_file(file: UploadFile, user_address: str = Form(...), folder_path: str = Form("/")):
    # Upload to IPFS first
    print(f"Uploading file {file.filename} to IPFS...")
    files = {"file": (file.filename, await file.read())}
    response = requests.post(f"{IPFS_API_URL}/add", files=files)
    cid = response.json()["Hash"]
    
    # Prepare transaction for frontend to sign
    transaction_data = prepare_transaction(cid, file.filename, folder_path, user_address)
    
    clean_folder_path = folder_path.rstrip('/')
    full_path = f"{clean_folder_path}/{file.filename}" if clean_folder_path else f"/{file.filename}"
    
    return {
        "user": user_address, 
        "cid": cid, 
        "filename": file.filename, 
        "folder_path": full_path,
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
        
        # Build and prepare blockchain transaction for each file
        # Use folder_path as-is from frontend, and actual_filename
        transaction_data = prepare_transaction(cid, actual_filename, folder_path, user_address)

        # The full_path should just be folder_path + filename
        clean_folder_path = folder_path.rstrip('/')
        full_path = f"{clean_folder_path}/{actual_filename}" if clean_folder_path else f"/{actual_filename}"

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
        
        return {
            "success": True, 
            "tx_hash": request.tx_hash,
            "block_number": receipt.blockNumber,
            "gas_used": receipt.gasUsed,
            "status": receipt.status
        }
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
