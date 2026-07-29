"""File operations: delete/move (single + batch) and folder deletion.
Each prepares an unsigned transaction the frontend signs."""
import logging

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse
from web3 import Web3

import logredact
from configure import contract
from models import DeleteRequest, MoveRequest, MoveBatchRequest, DeleteBatchRequest, DeleteFolder
from helpers import (
    prepare_delete_transaction,
    prepare_move_transaction,
    prepare_move_batch_transaction,
    prepare_delete_folder,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# endpoint for deleting a file
@router.post("/delete")
def delete_file(request: DeleteRequest):
    try:
        txn = prepare_delete_transaction(request.cid, request.user_address)
        result = {"transaction": txn}
        return result

    except Exception as e:
        logger.error("Failed to prep delete transaction: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


# endpoint for moving a file
@router.post("/move")
def move_file(request: MoveRequest):
    try:
        txn = prepare_move_transaction(request.cid, request.new_path, request.user_address)
        result = {"transaction": txn}
        return result

    except Exception as e:
        logger.error("Failed to prep move transaction: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


# batch move: one moveFiles tx for folder rename / bulk trash / bulk restore
@router.post("/move-batch")
def move_batch(request: MoveBatchRequest = Body(...)):
    try:
        if len(request.cids) != len(request.new_paths):
            return JSONResponse(status_code=400, content={"error": "cids/new_paths length mismatch"})
        txn, count = prepare_move_batch_transaction(request.cids, request.new_paths, request.user_address)
        return {"transaction": txn, "count": count}
    except Exception as e:
        logger.error("Failed to prep batch move transaction: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


# batch delete of explicit CIDs (multi-select delete-forever); reuses the
# same cleanFolder contract call as folder deletion — one signed tx
@router.post("/delete-batch")
def delete_batch(request: DeleteBatchRequest = Body(...)):
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
        logger.error("Failed to prep batch delete transaction: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


# endpoint for deleting a folder
@router.post("/delete-folder")
def delete_folder(request: DeleteFolder = Body(...)):
    try:
        user_address = Web3.to_checksum_address(request.user_address)
        user_files = contract.functions.getUserFiles(user_address).call()

        target_cids = set()
        search_path = request.folder_path.strip("/")

        is_principal = search_path == "" or "/" not in search_path

        logger.debug("Searching for files to delete in: %s", search_path)

        for file_data in user_files:
            cid, full_path = file_data[0], file_data[1].strip("/")

            if full_path == search_path or full_path.startswith(search_path + "/"):
                target_cids.add(cid)
        final_cids = list(target_cids)
        txn = None

        if final_cids:
            logger.info("Found %d CIDs to delete: %s", len(final_cids),
                        [logredact.cid(c) for c in final_cids])
            txn = prepare_delete_folder(final_cids, user_address)
        else:
            logger.info("No files found on-chain for this folder path")

        return {
            "transaction": txn,
            "target_cids": target_cids,
            "is_principal": is_principal,
            "count": len(target_cids)
        }

    except Exception as e:
        logger.error("Failed to prep deleting folders transaction: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})
