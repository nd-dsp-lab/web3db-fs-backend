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
from permissions import READ, WRITE, DOWNLOAD, DELETE, SHARE, MOVE, CHANGE_OWNER, CHANGE_ROLE
from models import TransactionRequest, ShareRequest, UnshareRequest, DeleteRequest, MoveRequest, DeleteFolder, FundWalletRequest, ResolveRecipientRequest, NotifyShareRequest, DeleteBatchRequest, AuthTokenRequest, MoveBatchRequest, ShareBatchRequest
from configure import configure_app, IPFS_API_URL, IPFS_GATEWAY_URL, w3, contract
from helpers import (
    prepare_upload_transaction,
    prepare_upload_batch_transaction,
    prepare_share_transaction,
    prepare_unshare_transaction,
    prepare_share_batch_transaction,
    prepare_unshare_batch_transaction,
    prepare_delete_transaction,
    prepare_move_transaction,
    prepare_move_batch_transaction,
    prepare_delete_folder,
    unpin_cid,
)

# This will be a simple fastAPI server that acts as an sgx node 
app = FastAPI()
configure_app(app)  # CORS + other startup steps

# --- Download auth: wallet-signature login -> short-lived HMAC token ---
# The frontend proves wallet ownership once (personal_sign) and gets a token;
# /download and /thumbnail then check on-chain permissions for the token's
# address instead of trusting a spoofable user_address query param.
import time, hmac as hmac_mod, hashlib, secrets as secrets_mod
from eth_account import Account
from eth_account.messages import encode_defunct

AUTH_SECRET_FILE = os.path.join(os.path.dirname(__file__), "auth_secret.txt")
AUTH_TOKEN_TTL = 24 * 3600
AUTH_MESSAGE_MAX_AGE = 600  # seconds of clock skew allowed on the signed login message

def _load_auth_secret() -> bytes:
    # Prefer AUTH_SECRET from the environment (.env in dev, real env in prod);
    # fall back to an auto-generated local file so dev works out of the box.
    env_secret = os.getenv("AUTH_SECRET")
    if env_secret:
        return bytes.fromhex(env_secret.strip())
    try:
        with open(AUTH_SECRET_FILE) as f:
            return bytes.fromhex(f.read().strip())
    except FileNotFoundError:
        secret = secrets_mod.token_bytes(32)
        with open(AUTH_SECRET_FILE, "w") as f:
            f.write(secret.hex())
        return secret

AUTH_SECRET = _load_auth_secret()

def _token_signature(payload: str) -> str:
    return hmac_mod.new(AUTH_SECRET, payload.encode(), hashlib.sha256).hexdigest()

def auth_message(address: str, timestamp: int) -> str:
    return f"Web3FS sign-in\nAddress: {address.lower()}\nTimestamp: {timestamp}"

def verify_auth_token(token: str):
    """Returns the lowercase wallet address for a valid token, else None."""
    try:
        address, expiry, sig = token.split(".")
        payload = f"{address}.{expiry}"
        if not hmac_mod.compare_digest(sig, _token_signature(payload)):
            return None
        if int(expiry) < time.time():
            return None
        return address
    except (ValueError, AttributeError):
        return None

def can_download(cid: str, address: str) -> bool:
    """On-chain check: file owner, or DOWNLOAD permission bit granted."""
    try:
        checksum = Web3.to_checksum_address(address)
        owner = contract.functions.getFileOwner(cid).call()
        if owner.lower() == address.lower():
            return True
        return bool(contract.functions.getPermissions(cid, checksum).call() & DOWNLOAD)
    except Exception as e:
        print(f"Permission check failed for {cid}/{address}: {e}")
        return False

def require_download_access(cid: str, token: str):
    """Returns an error JSONResponse, or None if access is allowed."""
    address = verify_auth_token(token or "")
    if not address:
        return JSONResponse(status_code=401, content={"error": "Missing or invalid auth token"})
    if not can_download(cid, address):
        return JSONResponse(status_code=403, content={"error": "No download permission for this file"})
    return None

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

    return {
        "user": user_address,
        "transaction": transaction,  # frontend signs this once
        "uploaded_files": uploaded_files,
        "skipped_files": skipped_files,
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
        
        structured_files.append({
            "cid": cid,        # cid
            "filename": filename,   # filename
            "folder_path": folder_path, # temp folderPath
            "file_format": file_format, # fileFormat
            "timestamp": timestamp,   # timestamp
            "owner": owner,
            "is_owner": is_owner,
            "permissions": permissions,
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

@app.post("/auth/token")
async def issue_auth_token(request: AuthTokenRequest):
    if abs(time.time() - request.timestamp) > AUTH_MESSAGE_MAX_AGE:
        return JSONResponse(status_code=400, content={"error": "Login message expired, retry"})
    try:
        message = auth_message(request.address, request.timestamp)
        recovered = Account.recover_message(encode_defunct(text=message), signature=request.signature)
    except Exception as e:
        print(f"Auth signature recovery failed: {e}")
        return JSONResponse(status_code=401, content={"error": "Invalid signature"})
    if recovered.lower() != request.address.lower():
        return JSONResponse(status_code=401, content={"error": "Signature does not match address"})

    address = request.address.lower()
    expiry = int(time.time()) + AUTH_TOKEN_TTL
    payload = f"{address}.{expiry}"
    return {"token": f"{payload}.{_token_signature(payload)}", "expires": expiry}


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

# --- Share notification email (Amazon SES over SMTP) ---
SES_SMTP_HOST = os.getenv("SES_SMTP_HOST", "email-smtp.us-east-1.amazonaws.com")
SES_SMTP_PORT = 587
NOTIFY_FROM = os.getenv("NOTIFY_FROM", "Web3FS <notifications@fs.web3db.org>")
APP_URL = os.getenv("APP_URL", "https://fs.web3db.org")

@app.post("/notify-share")
async def notify_share(request: NotifyShareRequest):
    import smtplib
    from email.mime.text import MIMEText

    smtp_user = os.getenv("SES_SMTP_USER")
    smtp_password = os.getenv("SES_SMTP_PASSWORD")
    if not smtp_user or not smtp_password:
        return JSONResponse(status_code=500, content={"error": "SES SMTP not configured on server"})

    to_email = request.recipient_email.strip().lower()
    if "@" not in to_email:
        return JSONResponse(status_code=400, content={"error": "Invalid recipient email"})

    sharer = request.sharer.strip()
    body = (
        f"{sharer} shared \"{request.filename}\" with you on Web3FS.\n\n"
        f"Open {APP_URL} and sign in with this email address ({to_email}) to view the file.\n\n"
        f"— Web3FS"
    )
    msg = MIMEText(body)
    msg["Subject"] = f"{sharer} shared \"{request.filename}\" with you on Web3FS"
    msg["From"] = NOTIFY_FROM
    msg["To"] = to_email

    try:
        with smtplib.SMTP(SES_SMTP_HOST, SES_SMTP_PORT, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(smtp_user, smtp_password)
            smtp.sendmail(NOTIFY_FROM, [to_email], msg.as_string())
        print(f"[notify-share] sent to {to_email} for file {request.filename}")
        return {"sent": True}
    except smtplib.SMTPException as e:
        # Notification is best-effort: the share itself already succeeded
        print(f"[notify-share] send failed: {e}")
        return JSONResponse(status_code=502, content={"error": f"Email send failed: {e}"})

# --- Recipient resolution (share by email via Privy) ---
PRIVY_API_BASE = "https://auth.privy.io/api/v1"

def _privy_auth():
    app_id = os.getenv("PRIVY_APP_ID")
    secret = os.getenv("PRIVY_APP_SECRET")
    if not app_id or not secret:
        return None, None
    return (app_id, secret), {"privy-app-id": app_id}

def _extract_eth_address(privy_user: dict) -> Optional[str]:
    accounts = privy_user.get("linked_accounts", [])
    eth_wallets = [a for a in accounts if a.get("type") == "wallet" and a.get("chain_type") == "ethereum"]
    if not eth_wallets:
        return None
    # Prefer the Privy embedded wallet over linked external ones
    embedded = [w for w in eth_wallets if w.get("wallet_client") == "privy" or w.get("wallet_client_type") == "privy"]
    return (embedded or eth_wallets)[0].get("address")

@app.post("/resolve-recipient")
async def resolve_recipient(request: ResolveRecipientRequest):
    recipient = request.recipient.strip()

    # Raw address: validate and pass through
    if recipient.startswith("0x"):
        try:
            return {"address": Web3.to_checksum_address(recipient), "existed": True, "pregenerated": False}
        except Exception:
            return JSONResponse(status_code=400, content={"error": "Invalid Ethereum address"})

    if "@" not in recipient:
        return JSONResponse(status_code=400, content={"error": "Recipient must be an email or 0x address"})

    auth, headers = _privy_auth()
    if auth is None:
        return JSONResponse(status_code=500, content={"error": "Privy API not configured on server"})

    email = recipient.lower()
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Look up an existing user by email
        lookup = await client.post(
            f"{PRIVY_API_BASE}/users/email/address",
            json={"address": email}, auth=auth, headers=headers,
        )
        if lookup.status_code == 200:
            address = _extract_eth_address(lookup.json())
            if not address:
                return JSONResponse(status_code=404, content={"error": "User exists but has no Ethereum wallet"})
            return {"address": address, "existed": True, "pregenerated": False}

        # The email-address lookup only matches `email` accounts; users who
        # signed in with Google have a `google_oauth` account instead. Scan
        # the user list for a matching Google email before pregenerating.
        all_users = await client.get(f"{PRIVY_API_BASE}/users", auth=auth, headers=headers)
        if all_users.status_code == 200:
            for u in all_users.json().get("data", []):
                for acct in u.get("linked_accounts", []):
                    if acct.get("type") == "google_oauth" and (acct.get("email") or "").lower() == email:
                        address = _extract_eth_address(u)
                        if address:
                            return {"address": address, "existed": True, "pregenerated": False}

        # Unknown email: pregenerate a user + embedded wallet they claim on first login
        created = await client.post(
            f"{PRIVY_API_BASE}/users",
            json={
                "create_ethereum_wallet": True,
                "linked_accounts": [{"type": "email", "address": email}],
            },
            auth=auth, headers=headers,
        )
        if created.status_code not in (200, 201):
            print(f"[resolve-recipient] Privy user creation failed: {created.status_code} {created.text}")
            return JSONResponse(status_code=502, content={"error": "Could not create wallet for that email"})
        address = _extract_eth_address(created.json())
        if not address:
            return JSONResponse(status_code=502, content={"error": "Wallet creation returned no address"})
        print(f"[resolve-recipient] pregenerated wallet {address} for {email}")
        return {"address": address, "existed": False, "pregenerated": True}

# endpoint for sharing a file (frontend has to sign)
@app.post("/share")
async def share_file(request: ShareRequest):
    try:
        txn = prepare_share_transaction(request.cid, request.to_address, request.user_address)
        return {"transaction": txn}
    except Exception as e:
        print(f"Failed to prepare share transaction: {e}")
        return {"error": str(e)}

# Batch share (folder share): one grantFiles tx covering many cids
@app.post("/share-batch")
async def share_batch(request: ShareBatchRequest):
    try:
        txn, count = prepare_share_batch_transaction(request.cids, request.to_address, request.user_address)
        if txn is None:
            return {"error": "No owned files to share"}
        return {"transaction": txn, "count": count}
    except Exception as e:
        print(f"Failed to prepare batch share transaction: {e}")
        return {"error": str(e)}

# Batch unshare (folder unshare): one revokeFiles tx covering many cids
@app.post("/unshare-batch")
async def unshare_batch(request: ShareBatchRequest):
    try:
        txn, count = prepare_unshare_batch_transaction(request.cids, request.to_address, request.user_address)
        if txn is None:
            return {"error": "No owned files to unshare"}
        return {"transaction": txn, "count": count}
    except Exception as e:
        print(f"Failed to prep batch unshare transaction: {e}")
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

# endpoint for moving a file
@app.post("/move")
async def move_file(request: MoveRequest):
    try:
        txn = prepare_move_transaction(request.cid, request.new_path, request.user_address)
        result = {"transaction": txn}
        return result
    
    except Exception as e:
        print(f"Failed to prep move transaction: {e}")

# batch move: one moveFiles tx for folder rename / bulk trash / bulk restore
@app.post("/move-batch")
async def move_batch(request: MoveBatchRequest = Body(...)):
    try:
        if len(request.cids) != len(request.new_paths):
            return JSONResponse(status_code=400, content={"error": "cids/new_paths length mismatch"})
        txn, count = prepare_move_batch_transaction(request.cids, request.new_paths, request.user_address)
        return {"transaction": txn, "count": count}
    except Exception as e:
        print(f"Failed to prep batch move transaction: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

# batch delete of explicit CIDs (multi-select delete-forever); reuses the
# same cleanFolder contract call as folder deletion — one signed tx
@app.post("/delete-batch")
async def delete_batch(request: DeleteBatchRequest = Body(...)):
    try:
        user_address = Web3.to_checksum_address(request.user_address)
        # only allow CIDs the user actually owns
        owned = []
        for cid in set(request.cids):
            try:
                owner = contract.functions.getFileOwner(cid).call()
            except Exception:
                continue
            if owner.lower() == user_address.lower():
                owned.append(cid)

        txn = prepare_delete_folder(owned, user_address) if owned else None
        return {"transaction": txn, "count": len(owned)}
    except Exception as e:
        print(f"Failed to prep batch delete transaction: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

# endpoint for deleting a folder
@app.post("/delete-folder")
async def delete_folder(request: DeleteFolder = Body(...)):
    try:
        user_address = Web3.to_checksum_address(request.user_address)
        user_files = contract.functions.getUserFiles(user_address).call()
        
        target_cids = set()
        search_path = request.folder_path.strip("/")
        
        is_principal = search_path == "" or "/" not in search_path
        
        print(f"Searching for files to delete in: {search_path}") 
        
        for file_data in user_files:
            cid, full_path = file_data[0], file_data[1].strip("/")
            
            if full_path == search_path or full_path.startswith(search_path + "/"):
                target_cids.add(cid)
        final_cids = list(target_cids)    
        txn = None
        
        if final_cids:
            print(f"Found {len(final_cids)} CIDs to delete: {final_cids}")
            txn = prepare_delete_folder(final_cids, user_address)
        else:
            print("No files found on-chain for this folder path.")

        return {
            "transaction": txn, 
            "target_cids": target_cids, 
            "is_principal": is_principal,
            "count": len(target_cids)
        }
    
    except Exception as e:
        
        print(f"Failed to prep deleting folders transaction: {e}")
        return {"error": str(e)}

@app.get("/shared-users")
def get_shared_users(cid: str, user_address: str):
    try:
        owner = contract.functions.getFileOwner(cid).call()
        shared_user = Web3.to_checksum_address(user_address)
        owner_checksum = Web3.to_checksum_address(owner)

        if shared_user.lower() == owner_checksum.lower():
            shared_users = contract.functions.getSharedUsers(cid).call()
            return {"shared_with": shared_users}
        else:
            return {"shared_by": owner}
    except Exception as e:
        print(f"Error fetching shared users for CID {cid}: {e}")
        return {"shared_with": [], "error": str(e)}

# Folder share modal: union of shared users across every owned cid in the
# folder (POST because a folder can hold more cids than a query string fits)
@app.post("/shared-users-batch")
def get_shared_users_batch(request: DeleteBatchRequest):
    try:
        requester = Web3.to_checksum_address(request.user_address)
        users = set()
        for cid in request.cids:
            owner = contract.functions.getFileOwner(cid).call()
            if owner.lower() != requester.lower():
                continue
            for u in contract.functions.getSharedUsers(cid).call():
                users.add(u)
        return {"shared_with": sorted(users)}
    except Exception as e:
        print(f"Error fetching shared users batch: {e}")
        return {"shared_with": [], "error": str(e)}

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8090, reload=True)