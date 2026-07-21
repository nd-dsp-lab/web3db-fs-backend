"""File listing for the drive view, plus storage-capacity stats for the
sidebar usage bar."""
import logging

import requests
from fastapi import APIRouter
from web3 import Web3

from configure import IPFS_API_URL, IPFS_GATEWAY_URL, contract

logger = logging.getLogger(__name__)

router = APIRouter()

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


# get all the files from the user on the smart contract -> updated to return metadata from new smart contract
@router.get("/")
async def get_files(user_address: str = None):
    logger.debug("Fetching user files from smart contract")
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
                logger.warning("getFileOwner failed for %s (attempt %d): %s", cid, attempt + 1, e)

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
                logger.warning("getSharedUsers failed for %s: %s", cid, e)

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

    logger.debug("User files: %s", structured_files)
    return {"user_files": structured_files}


# Storage capacity for the sidebar usage bar: free disk on the volume backing
# the IPFS repo (via repo/stat when reachable, else this host's disk — the
# Docker volume lives on it anyway).
@router.get("/storage-stats")
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
        logger.warning("repo/stat failed: %s", e)
    try:
        du = shutil.disk_usage("/")
        stats["disk_total"] = du.total
        stats["disk_free"] = du.free
    except Exception as e:
        logger.warning("disk_usage failed: %s", e)
    return stats
