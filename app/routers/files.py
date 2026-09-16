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
def get_files(user_address: str = None):
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

        # A shared (non-owned) file whose access has expired (or been
        # revoked) isn't "shared with me" anymore. getUserFiles keeps
        # returning the cid regardless -- it's a list, not a permission
        # check -- but getPermissions is already expiry-aware (0 once
        # _expiresAtBlock has passed), so it's the single source of truth
        # for whether this share is still actually active.
        if not is_owner and permissions == 0:
            continue

        # A recipient needs to know their access is on a clock at all --
        # otherwise a shared file just vanishes later with no warning.
        # Owner's own permissions are set directly in _uploadFile, never
        # through _grant, so they never carry an expiry.
        expires_at_block = None
        if not is_owner:
            try:
                expires_at_block = contract.functions.getExpiresAtBlock(cid, user_address).call() or None
            except Exception as e:
                logger.warning("getExpiresAtBlock failed for %s/%s: %s", cid, user_address, e)

        # Who the file is shared with (owner only) — drives the Sharing
        # column and the shared-folder icon (folder = intersection of these).
        # Filtered the same way: getSharedUsers returns every address ever
        # granted, expired or not, so each is re-checked against the
        # expiry-aware getPermissions before being shown as still shared.
        shared_with = []
        if is_owner:
            try:
                for addr in contract.functions.getSharedUsers(cid).call():
                    try:
                        if contract.functions.getPermissions(cid, addr).call() != 0:
                            shared_with.append(addr)
                    except Exception as e:
                        logger.warning("getPermissions failed for %s/%s: %s", cid, addr, e)
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
            "expires_at_block": expires_at_block,
            "shared_with": shared_with,
            "size": get_file_size(cid),
            "ipfs_url": f"{IPFS_GATEWAY_URL}/{cid}"
        })

    logger.debug("User files: %s", structured_files)
    return {"user_files": structured_files}


# Quota for scaling the sidebar usage bar: the IPFS repo's configured cap
# (via repo/stat when reachable, else this host's disk size — the Docker
# volume lives on it anyway). Deliberately nothing else: repo size and free
# space are node-wide numbers that would reveal the server's capacity and
# other users' aggregate usage.
@router.get("/storage-stats")
def storage_stats():
    import shutil
    stats = {}
    try:
        r = requests.post(f"{IPFS_API_URL}/repo/stat", timeout=5)
        if r.ok:
            stats["ipfs_storage_max"] = r.json().get("StorageMax")
    except Exception as e:
        logger.warning("repo/stat failed: %s", e)
    try:
        stats["disk_total"] = shutil.disk_usage("/").total
    except Exception as e:
        logger.warning("disk_usage failed: %s", e)
    return stats
