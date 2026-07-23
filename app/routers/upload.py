"""Uploads: single file and folder (batch) registration on IPFS + contract,
plus transaction-receipt verification (which unpins on delete/cleanFolder)."""
import json
import logging
from typing import Optional, List

import requests
from fastapi import APIRouter, UploadFile, Form
from fastapi.responses import JSONResponse

from configure import IPFS_API_URL, w3, contract
from models import TransactionRequest
from helpers import (
    prepare_upload_transaction,
    prepare_upload_batch_transaction,
    unpin_cid,
    folder_share_set,
    prepare_inherited_grant_transactions,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# Register the file to ipfs and get a cid
@router.post("/upload")
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

    logger.debug("[upload] full_path to send to contract: %s", full_path)

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
            logger.warning("[upload] inherited share prep failed: %s", e)
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


@router.post("/upload-folder")
async def upload_folder(
    files: List[UploadFile],
    paths: List[str] = Form(...),
    user_address: str = Form(...)
):
    logger.info("Uploading %d files from folder for %s", len(files), user_address)

    uploaded_files = []
    skipped_files = []

    for idx, file in enumerate(files):
        folder_path = paths[idx] if idx < len(paths) else "/"
        logger.debug("[%d] Uploading %s to IPFS (folder: %s)", idx, file.filename, folder_path)

        # Chrome sends webkitRelativePath as the multipart filename for folder
        # uploads ("Docs/a.pdf"). A slashed name makes `ipfs add` build a
        # wrapper directory and return the dir CID last, so the stored CID
        # would resolve to a gateway listing page instead of the file — always
        # add under the leaf name.
        actual_filename = file.filename.split('/')[-1]

        # Read file and upload to IPFS
        file_data = await file.read()
        # Add unpinned — pin only after the duplicate checks pass (see /upload)
        ipfs_response = requests.post(
            f"{IPFS_API_URL}/add?pin=false",
            files={"file": (actual_filename, file_data)}
        )
        if ipfs_response.status_code != 200:
            logger.warning("Failed to upload %s to IPFS", file.filename)
            continue
        logger.debug("Uploaded %s to IPFS", file.filename)
        ipfs_response.raise_for_status()

        # Parse only the last JSON object if multiple exist
        raw_text = ipfs_response.text.strip()
        last_line = raw_text.splitlines()[-1]
        try:
            ipfs_json = json.loads(last_line)
            cid = ipfs_json["Hash"]
        except Exception as e:
            logger.error("Error parsing IPFS response: %s | raw: %s", e, raw_text)
            continue

        # Skip files whose content already exists on-chain — building the tx
        # would revert with "File already exists" and 500 the whole batch
        try:
            existing_owner = contract.functions.getFileOwner(cid).call()
        except Exception:
            existing_owner = "0x0000000000000000000000000000000000000000"
        if existing_owner != "0x0000000000000000000000000000000000000000":
            logger.warning("Skipping %s: CID already owned by %s", actual_filename, existing_owner)
            skipped_files.append({"filename": actual_filename, "cid": cid, "owner": existing_owner})
            continue
        # ...and identical files within the same batch (same CID twice)
        if any(u["cid"] == cid for u in uploaded_files):
            logger.warning("Skipping %s: duplicate content within this batch", actual_filename)
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
            logger.warning("[upload-folder] inherited share prep failed: %s", e)
            share_transactions, auto_shared_with = [], []

    return {
        "user": user_address,
        "transaction": transaction,  # frontend signs this once
        "uploaded_files": uploaded_files,
        "skipped_files": skipped_files,
        "share_transactions": share_transactions,
        "auto_shared_with": auto_shared_with,
    }


# New endpoint to verify transaction was successful
@router.post("/verify-upload")
def verify_upload(request: TransactionRequest):
    try:
        logger.info("Waiting for transaction receipt for: %s", request.tx_hash)
        receipt = w3.eth.wait_for_transaction_receipt(request.tx_hash, timeout=120)

        logger.info("Transaction mined: block=%s gas=%s status=%s",
                    receipt.blockNumber, receipt.gasUsed, receipt.status)

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
                    logger.info("Detected deleteFile for cid %s - unpinning from local IPFS node", cid_unpin)
                    unpin_result = unpin_cid(cid_unpin)
                    response_payload["unpin_result"] = unpin_result
                else:
                    response_payload["unpin_result"] = {"error": "Could not find cid in tx params"}
            elif func_name == "cleanFolder" and receipt.status == 1:
                    cids_to_unpin = func_params.get("cids")
                    if cids_to_unpin:
                        logger.info("Found %d CIDs to unpin", len(cids_to_unpin))
                        results = []
                        for cid in cids_to_unpin:
                            logger.debug("Unpinning: %s", cid)
                            res = unpin_cid(cid)
                            results.append({"cid": cid, "result": res})
                    else:
                        logger.warning("cleanFolder transaction found, but CIDs list was empty")
                        response_payload["unpin_result"] = {"warning": "Empty CID list"}
        except Exception as e:
            logger.warning("Couldn't decode tx input for unpin: %s", e)
            response_payload["decoded_function_error"] = str(e)

        return response_payload

    except Exception as e:
        logger.error("Transaction verification failed: %s", e)
        return {"success": False, "error": str(e)}
