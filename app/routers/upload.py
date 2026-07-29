"""Uploads: single file and folder (batch) registration on IPFS + contract,
plus transaction-receipt verification (which unpins on delete/cleanFolder)."""
import logging
from typing import Optional, List

from fastapi import APIRouter, UploadFile, Form
from fastapi.responses import JSONResponse

import filecrypto
import ipfs
import logredact
from configure import w3, contract
from constants import ZERO_ADDRESS
from models import TransactionRequest
from helpers import (
    prepare_upload_transaction,
    prepare_upload_batch_transaction,
    unpin_cids,
    common_ancestor_folder,
    prepare_inherited_shares,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Contract calls that end a file's life on-chain, and so free the local node to
# drop its pins. Everything else leaves the pinset alone.
RELEASING_FUNCTIONS = ("deleteFile", "cleanFolder")


def _released_cids(func_name: str, func_params: dict) -> list:
    """The CIDs a mined delete releases, read out of its decoded arguments.

    Solidity argument names have varied across contract revisions, hence the
    fallbacks; an unrecognised shape yields nothing rather than guessing.
    """
    if func_name == "deleteFile":
        cid = func_params.get("cid") or func_params.get("_cid")
        return [cid] if cid else []
    if func_name == "cleanFolder":
        return list(func_params.get("cids") or [])
    return []


# Register the file to ipfs and get a cid
@router.post("/upload")
async def upload_file(file: UploadFile, user_address: str = Form(...), folder_path: str = Form(""), file_format: Optional[str] = None):
    # Sealed before IPFS ever sees it: the node stores only ciphertext, and
    # the CID (computed below) addresses the ciphertext. Encryption is
    # deterministic, so the duplicate checks still catch identical content.
    file_data = filecrypto.encrypt(await file.read())

    # 1. Add to IPFS unpinned — just to compute the CID. Pinning is deferred
    # until the duplicate check passes, so a rejected duplicate never touches
    # the original owner's pin (unpinning here used to cause GC data loss).
    cid = ipfs.add_unpinned(file.filename, file_data)

    # 2. Early duplicate check — before pinning or building the tx
    existing_owner = contract.functions.getFileOwner(cid).call()
    if existing_owner != ZERO_ADDRESS:
        return JSONResponse(status_code=409, content={
            "success": False,
            "reason": "file_already_exists",
            "cid": cid,
            "owner": existing_owner
        })

    # New content — pin it now
    ipfs.pin(cid)

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

    logger.debug("[upload] full_path to send to contract: %s", logredact.path(full_path))

    # Prepare transaction for frontend to sign
    transaction_data = prepare_upload_transaction(cid, full_path, user_address, file_format)

    # Inherited folder sharing: if the destination folder is shared, prepare
    # grant txs (one per recipient) for the frontend to sign after the upload
    share_transactions, auto_shared_with = prepare_inherited_shares([cid], folder_path, user_address)

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
    logger.info("Uploading %d files from folder for %s", len(files), logredact.addr(user_address))

    uploaded_files = []
    skipped_files = []

    for idx, file in enumerate(files):
        # The frontend sends the destination path per file, filename included.
        full_path = paths[idx] if idx < len(paths) else "/"
        logger.debug("[%d] Uploading %s to IPFS (path: %s)",
                     idx, logredact.fname(file.filename), logredact.path(full_path))

        # Chrome sends webkitRelativePath as the multipart filename for folder
        # uploads ("Docs/a.pdf"). A slashed name makes `ipfs add` build a
        # wrapper directory and return the dir CID last, so the stored CID
        # would resolve to a gateway listing page instead of the file — always
        # add under the leaf name.
        actual_filename = file.filename.split('/')[-1]

        # Read, seal (see /upload), and add to IPFS
        file_data = filecrypto.encrypt(await file.read())
        # Add unpinned — pin only after the duplicate checks pass (see /upload)
        try:
            cid = ipfs.add_unpinned(actual_filename, file_data)
        except Exception as e:
            logger.warning("Failed to upload %s to IPFS: %s", logredact.fname(file.filename), e)
            continue

        # Skip files whose content already exists on-chain — building the tx
        # would revert with "File already exists" and 500 the whole batch
        try:
            existing_owner = contract.functions.getFileOwner(cid).call()
        except Exception:
            existing_owner = ZERO_ADDRESS
        if existing_owner != ZERO_ADDRESS:
            logger.warning("Skipping %s: CID already owned by %s",
                           logredact.fname(actual_filename), logredact.addr(existing_owner))
            skipped_files.append({"filename": actual_filename, "cid": cid, "owner": existing_owner})
            continue
        # ...and identical files within the same batch (same CID twice)
        if any(u["cid"] == cid for u in uploaded_files):
            logger.warning("Skipping %s: duplicate content within this batch",
                           logredact.fname(actual_filename))
            skipped_files.append({"filename": actual_filename, "cid": cid, "owner": user_address})
            continue

        # Accepted for upload — pin the content now. A file that fails to pin
        # must not reach the contract: the CID would be registered while the
        # bytes stay collectable, so it would resolve to nothing after a GC.
        # One bad file is skipped rather than failing the whole batch.
        try:
            ipfs.pin(cid)
        except Exception as e:
            logger.error("Skipping %s: pin failed (%s)", logredact.fname(actual_filename), e)
            skipped_files.append({"filename": actual_filename, "cid": cid, "reason": "pin_failed"})
            continue

        file_format = actual_filename.split(".")[-1] if "." in actual_filename else ""
        uploaded_files.append({
            "cid": cid,
            "filename": actual_filename,   # leaf, for the UI
            "full_path": full_path,        # path + leaf, what the contract stores
            "file_format": file_format,
        })

    # One uploadFiles(cids, paths, formats) tx registers the whole batch —
    # a single signature regardless of file count
    transaction = None
    if uploaded_files:
        try:
            transaction = prepare_upload_batch_transaction(
                [u["cid"] for u in uploaded_files],
                [u["full_path"] for u in uploaded_files],
                [u["file_format"] for u in uploaded_files],
                user_address,
            )
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": f"Batch tx prep failed: {e}"})

    # Inherited folder sharing: the batch's drop target is the deepest folder
    # every uploaded file sits under, and its share set is what the new files
    # inherit.
    drop_target = common_ancestor_folder([u["full_path"] for u in uploaded_files])
    share_transactions, auto_shared_with = prepare_inherited_shares(
        [u["cid"] for u in uploaded_files], drop_target, user_address)

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

            # A mined delete is the point at which the bytes stop being ours to
            # keep, so that is where the local node releases them.
            if receipt.status == 1 and func_name in RELEASING_FUNCTIONS:
                cids = _released_cids(func_name, func_params)
                if cids:
                    logger.info("%s mined — releasing %d cid(s) from the local node",
                                func_name, len(cids))
                    response_payload["unpin_result"] = unpin_cids(cids)
                else:
                    logger.warning("%s mined but named no CIDs to release", func_name)
                    response_payload["unpin_result"] = {"warning": "Empty CID list"}
        except Exception as e:
            logger.warning("Couldn't decode tx input for unpin: %s", e)
            response_payload["decoded_function_error"] = str(e)

        return response_payload

    except Exception as e:
        logger.error("Transaction verification failed: %s", e)
        return {"success": False, "error": str(e)}
