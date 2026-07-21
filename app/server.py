from fastapi import FastAPI, UploadFile, Form, Body, Header
import requests
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
import io
from web3 import Web3
import json
import os
import httpx
import uvicorn
from dotenv import load_dotenv
from typing import Optional, List
from models import TransactionRequest, DeleteRequest, MoveRequest, DeleteFolder, FundWalletRequest, DeleteBatchRequest, MoveBatchRequest
from configure import configure_app, IPFS_API_URL, IPFS_GATEWAY_URL, w3, contract
from helpers import (
    prepare_upload_transaction,
    prepare_upload_batch_transaction,
    prepare_delete_transaction,
    prepare_move_transaction,
    prepare_move_batch_transaction,
    prepare_delete_folder,
    unpin_cid,
    folder_share_set,
    prepare_inherited_grant_transactions,
)
from security import verify_auth_token, can_download, require_download_access
from routers.auth import router as auth_router
from routers.sharing import router as sharing_router

# This will be a simple fastAPI server that acts as an sgx node
app = FastAPI()
configure_app(app)  # CORS + other startup steps
app.include_router(auth_router)
app.include_router(sharing_router)

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

# Register the file to ipfs and get a cid 
@app.post("/upload")
async def upload_file(file: UploadFile, user_address: str = Form(...), folder_path: str = Form(""), file_format: Optional[str] = None):
    file_data = await file.read()

    # 1. Add to IPFS unpinned — just to compute the CID. Pinning is deferred
    # until the duplicate check passes, so a rejected duplicate never touches
    # the original owner's pin (unpinning here used to cause GC data loss).
    resp = requests.post(
        f"{IPFS_API_URL}/add?pin=false",
        files={"file": (file.filename, file_data)},
        stream=True,
        timeout=10,
    )
    resp.raise_for_status()
    line = resp.raw.readline()
    resp.close()
    cid = json.loads(line)["Hash"]

    # 2. Early duplicate check — before pinning or building the tx
    existing_owner = contract.functions.getFileOwner(cid).call()
    if existing_owner != "0x0000000000000000000000000000000000000000":
        return JSONResponse(status_code=409, content={
            "success": False,
            "reason": "file_already_exists",
            "cid": cid,
            "owner": existing_owner
        })

    # New content — pin it now
    requests.post(f"{IPFS_API_URL}/pin/add?arg={cid}", timeout=30).raise_for_status()

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

    # Inherited folder sharing: if the destination folder is shared, prepare
    # grant txs (one per recipient) for the frontend to sign after the upload
    share_transactions, auto_shared_with = [], []
    if folder_path:
        try:
            auto_shared_with = folder_share_set(user_address, folder_path)
            if auto_shared_with:
                share_transactions = prepare_inherited_grant_transactions([cid], auto_shared_with, user_address)
        except Exception as e:
            print(f"[upload] inherited share prep failed: {e}")
            share_transactions, auto_shared_with = [], []

    return {
        "user": user_address,
        "cid": cid,
        "filename": file.filename,  # leaf for UI
        "folder_path": "/" + folder_path if folder_path else "/",
        "full_path": full_path,
        "fileformat": file_format,
        "transaction": transaction_data,  # Frontend will sign this
        "share_transactions": share_transactions,
        "auto_shared_with": auto_shared_with,
    }


@app.post("/upload-folder")
async def upload_folder(
    files: List[UploadFile],
    paths: List[str] = Form(...),
    user_address: str = Form(...)
):
    print(f"Uploading {len(files)} files from folder for {user_address}...")

    uploaded_files = []
    skipped_files = []

    for idx, file in enumerate(files):
        print(idx, file.filename)
        folder_path = paths[idx] if idx < len(paths) else "/"
        print(f"  Uploading {file.filename} to IPFS (folder: {folder_path})")

        # Read file and upload to IPFS
        file_data = await file.read()
        # Add unpinned — pin only after the duplicate checks pass (see /upload)
        ipfs_response = requests.post(
            f"{IPFS_API_URL}/add?pin=false",
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

        # Skip files whose content already exists on-chain — building the tx
        # would revert with "File already exists" and 500 the whole batch
        try:
            existing_owner = contract.functions.getFileOwner(cid).call()
        except Exception:
            existing_owner = "0x0000000000000000000000000000000000000000"
        if existing_owner != "0x0000000000000000000000000000000000000000":
            print(f"  Skipping {actual_filename}: CID already owned by {existing_owner}")
            skipped_files.append({"filename": actual_filename, "cid": cid, "owner": existing_owner})
            continue
        # ...and identical files within the same batch (same CID twice)
        if any(u["cid"] == cid for u in uploaded_files):
            print(f"  Skipping {actual_filename}: duplicate content within this batch")
            skipped_files.append({"filename": actual_filename, "cid": cid, "owner": user_address})
            continue

        # Accepted for upload — pin the content now
        requests.post(f"{IPFS_API_URL}/pin/add?arg={cid}", timeout=30)
        file_format = actual_filename.split(".")[-1] if "." in actual_filename else ""
        uploaded_files.append({
            "cid": cid,
            "filename": actual_filename,  # Use actual_filename here too
            "folder_path": folder_path,   # already the complete path
            "file_format": file_format,
        })

    # One uploadFiles(cids, paths, formats) tx registers the whole batch —
    # a single signature regardless of file count
    transaction = None
    if uploaded_files:
        try:
            transaction = prepare_upload_batch_transaction(
                [u["cid"] for u in uploaded_files],
                [u["folder_path"] for u in uploaded_files],
                [u["file_format"] for u in uploaded_files],
                user_address,
            )
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": f"Batch tx prep failed: {e}"})

    # Inherited folder sharing: derive the drop target as the deepest common
    # ancestor folder of the batch (entries' folder_path is the full file
    # path, so drop the filename segment), then grant every new cid to the
    # folder's share set — one grantFiles tx per recipient.
    share_transactions, auto_shared_with = [], []
    if uploaded_files:
        try:
            folder_lists = [[p for p in u["folder_path"].split("/") if p][:-1] for u in uploaded_files]
            common = folder_lists[0]
            for fl in folder_lists[1:]:
                n = 0
                while n < len(common) and n < len(fl) and common[n] == fl[n]:
                    n += 1
                common = common[:n]
            if common:
                auto_shared_with = folder_share_set(user_address, "/".join(common))
                if auto_shared_with:
                    share_transactions = prepare_inherited_grant_transactions(
                        [u["cid"] for u in uploaded_files], auto_shared_with, user_address)
        except Exception as e:
            print(f"[upload-folder] inherited share prep failed: {e}")
            share_transactions, auto_shared_with = [], []

    return {
        "user": user_address,
        "transaction": transaction,  # frontend signs this once
        "uploaded_files": uploaded_files,
        "skipped_files": skipped_files,
        "share_transactions": share_transactions,
        "auto_shared_with": auto_shared_with,
    }

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

            # TODO: Make this a helper function (unpin_file(...) or something)
            # if it's a deleteFile call and tx succeeded -> unpin cid from local IPFS
            if func_name == "deleteFile" and receipt.status == 1:
                cid_unpin = func_params.get("cid") or func_params.get("_cid") or None
                if cid_unpin:
                    print(f"Detected deleteFile for cid {cid_unpin} - unpinning from local IPFS node")
                    unpin_result = unpin_cid(cid_unpin)
                    response_payload["unpin_result"] = unpin_result
                else:
                    response_payload["unpin_result"] = {"error": "Could not find cid in tx params"}
            elif func_name == "cleanFolder" and receipt.status == 1:
                    cids_to_unpin = func_params.get("cids")
                    if cids_to_unpin:
                        print(f">>> FOUND {len(cids_to_unpin)} CIDs to unpin")
                        results = []
                        for cid in cids_to_unpin:
                            print(f">>> Unpinning: {cid}")
                            res = unpin_cid(cid)
                            results.append({"cid": cid, "result": res})
                    else:
                        print("cleanFolder transaction found, but CIDs list was empty.")
                        response_payload["unpin_result"] = {"warning": "Empty CID list"}
        except Exception as e:
            print(f"Couldn't decode tx input for unpin: {e}")
            response_payload["decoded_function_error"] = str(e)

        return response_payload
    
    except Exception as e:
        print(f"Transaction verification failed: {e}")
        return {"success": False, "error": str(e)}

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


# endpoint for downloading a file from ipfs -> updated
@app.get("/download/{cid}/{filename}")
async def download_file_with_name(cid: str, filename: str, x_auth_token: Optional[str] = Header(None)):
    denied = require_download_access(cid, x_auth_token)
    if denied:
        return denied
    try:
        # follow_redirects: kubo's gateway 301-redirects /ipfs/{cid} to the
        # subdomain gateway ({cid}.ipfs.localhost)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            # Get file from IPFS using GET (updated from POST)
            # response = await client.get(f"{IPFS_API_URL}/cat", params={"arg": cid})
            response = await client.get(f"{IPFS_GATEWAY_URL}/{cid}")
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

# Download a whole folder as a zip. Token-authenticated; includes files under
# that path the token's address owns or has the DOWNLOAD permission on (so
# folders shared to the user are downloadable from the Shared view too).
@app.get("/download-folder")
async def download_folder_zip(path: str, x_auth_token: Optional[str] = Header(None)):
    import zipfile

    address = verify_auth_token(x_auth_token or "")
    if not address:
        return JSONResponse(status_code=401, content={"error": "Missing or invalid auth token"})

    prefix = "/" + "/".join(p for p in path.split("/") if p)
    if prefix == "/":
        return JSONResponse(status_code=400, content={"error": "Invalid folder path"})

    checksum = Web3.to_checksum_address(address)
    entries = []
    for f in contract.functions.getUserFiles(checksum).call():
        cid, full_path = f[0], f[1]
        norm = "/" + "/".join(p for p in full_path.split("/") if p)
        if not norm.startswith(prefix + "/"):
            continue
        if not can_download(cid, address):
            continue
        entries.append((cid, norm[len(prefix) + 1:]))

    if not entries:
        return JSONResponse(status_code=404, content={"error": "Folder is empty"})

    folder_name = prefix.rsplit("/", 1)[-1]
    buf = io.BytesIO()
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for cid, rel in entries:
                response = await client.get(f"{IPFS_GATEWAY_URL}/{cid}")
                if response.status_code == 200:
                    zf.writestr(f"{folder_name}/{rel}", response.content)
                else:
                    print(f"download-folder: skipping {cid} ({rel}), gateway {response.status_code}")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={folder_name}.zip"},
    )

# --- Thumbnails ---
# Small JPEG previews for image files, generated once per CID with Pillow
# and cached on disk (content is immutable per CID, so the cache never
# stales). Non-image or undecodable content returns 404 and the frontend
# keeps its file-type icon.
THUMBS_DIR = os.path.join(os.path.dirname(__file__), "thumbs")
THUMB_SIZE = (320, 320)

def render_text_thumbnail(content: bytes):
    """Drive-style page snippet for text files. Raises if content isn't UTF-8 text."""
    from PIL import Image, ImageDraw, ImageFont

    text = content[:8192].decode("utf-8")  # raises UnicodeDecodeError on binary
    img = Image.new("RGB", THUMB_SIZE, "#ffffff")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("Menlo.ttc", 13)
    except OSError:
        font = ImageFont.load_default(size=13)

    x, y, line_height = 16, 14, 17
    for line in text.splitlines():
        if y > THUMB_SIZE[1] - line_height:
            break
        draw.text((x, y), line.replace("\t", "    ")[:60], fill="#3c4043", font=font)
        y += line_height
    return img

@app.get("/thumbnail/{cid}")
async def get_thumbnail(cid: str, x_auth_token: Optional[str] = Header(None)):
    from PIL import Image

    # CIDs are base32/base58 alphanumeric — reject anything path-like
    if not cid.isalnum():
        return JSONResponse(status_code=400, content={"error": "Invalid CID"})

    denied = require_download_access(cid, x_auth_token)
    if denied:
        return denied

    os.makedirs(THUMBS_DIR, exist_ok=True)
    thumb_path = os.path.join(THUMBS_DIR, f"{cid}.jpg")

    if not os.path.exists(thumb_path):
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(f"{IPFS_GATEWAY_URL}/{cid}")
            if response.status_code != 200:
                return JSONResponse(status_code=404, content={"error": "File not found on IPFS"})

            content = response.content
            if content[:5] == b"%PDF-":
                # First page of a PDF, rendered via PyMuPDF
                import fitz
                doc = fitz.open(stream=content, filetype="pdf")
                pix = doc[0].get_pixmap(matrix=fitz.Matrix(0.5, 0.5))
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                doc.close()
            else:
                try:
                    img = Image.open(io.BytesIO(content))
                    img = img.convert("RGB")  # flatten alpha/palette for JPEG
                except Exception:
                    img = render_text_thumbnail(content)  # raises if binary
            img.thumbnail(THUMB_SIZE)
            img.save(thumb_path, "JPEG", quality=70)
        except Exception as e:
            print(f"Thumbnail generation failed for {cid}: {e}")
            return JSONResponse(status_code=404, content={"error": "Not a previewable image"})

    return StreamingResponse(
        open(thumb_path, "rb"),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"}
    )

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