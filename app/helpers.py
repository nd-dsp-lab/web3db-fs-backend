import logging
from typing import Optional
import requests
from web3 import Web3
from configure import w3, contract, IPFS_API_URL
from permissions import READ, DOWNLOAD

logger = logging.getLogger(__name__)

sepolia_chain_id = 11155111

# Sharing grants READ + DOWNLOAD; unshare revokes the same mask.
SHARE_MASK = READ | DOWNLOAD


class NotOwnerError(Exception):
    """The caller does not own the file they asked to act on."""


# Price gas 25% above the node's quote. The quote lags the network (and
# Infura can serve stale reads), which left transactions stuck in the
# mempool; the margin also lets a retry replace a stuck tx at the same nonce.
def _gas_price():
    return int(w3.eth.gas_price * 1.25)


# Base fields shared by every prepared transaction. nonce_offset lets batch
# endpoints stack several txs before any is broadcast (the chain nonce doesn't
# advance between calls). with_gas_price=False leaves gasPrice for web3 to fill
# (used where gas is estimated without the manual margin).
def _base_tx(user_address: str, nonce_offset: int = 0, with_gas_price: bool = True) -> dict:
    addr = Web3.to_checksum_address(user_address)
    tx = {
        'chainId': sepolia_chain_id,    # required for Sepolia
        'nonce': w3.eth.get_transaction_count(addr) + nonce_offset,
        'from': addr,
    }
    if with_gas_price:
        tx['gasPrice'] = _gas_price()
    return tx


# Raise unless user_address owns cid. action is used in the error message.
# Typed so callers can tell "you don't own this" apart from an RPC failure —
# both used to surface as a bare Exception, and so as the same 500.
def _assert_owner(cid: str, user_address: str, action: str):
    owner = contract.functions.getFileOwner(cid).call()
    if owner.lower() != user_address.lower():
        raise NotOwnerError(f"Only file owner can {action} file.")


# Filter cids down to the ones user_address owns on-chain.
def _owned_only(cids: list[str], user_address: str) -> list[str]:
    lower = user_address.lower()
    return [c for c in cids
            if contract.functions.getFileOwner(c).call().lower() == lower]


# return transaction data for frontend to sign
def prepare_upload_transaction(cid: str, full_path: str, user_address: str, file_format: Optional[str] = None, nonce_offset: int = 0):
    logger.debug("prepare_upload_transaction CID=%s full_path=%s", cid, full_path)
    try:
        txn = contract.functions.uploadFile(cid, full_path, file_format or "").build_transaction(
            _base_tx(user_address, nonce_offset))
        return txn
    except Exception as e:
        logger.error("Transaction preparation failed: %s", e)
        raise


# preparing a share transaction for owner to sign. duration_blocks is a
# relative block count converted to an absolute expiry on-chain, at grant
# time (see grantWithExpiry in FileStorage.sol) -- never precomputed here,
# so it can't go stale between building and mining the transaction.
def prepare_share_transaction(cid: str, to_address: str, user_address: str, duration_blocks: Optional[int] = None):
    try:
        user_address = Web3.to_checksum_address(user_address)
        to_address = Web3.to_checksum_address(to_address)
        _assert_owner(cid, user_address, "share")

        logger.info("Sharing CID %s with %s using mask %s duration_blocks=%s", cid, to_address, SHARE_MASK, duration_blocks)
        if duration_blocks:
            fn = contract.functions.grantWithExpiry(cid, to_address, SHARE_MASK, duration_blocks)
        else:
            fn = contract.functions.grant(cid, to_address, SHARE_MASK)
        txn = fn.build_transaction(_base_tx(user_address))
        return txn
    except Exception as e:
        logger.error("Share transaction preparation failed: %s", e)
        raise


# Batch share: one grantFiles(cids, to, mask) tx for folder share.
# Filters to cids the caller owns; returns (txn, count) or (None, 0).
def prepare_share_batch_transaction(cids: list[str], to_address: str, user_address: str, duration_blocks: Optional[int] = None):
    try:
        user_address = Web3.to_checksum_address(user_address)
        to_address = Web3.to_checksum_address(to_address)

        owned = _owned_only(cids, user_address)
        if not owned:
            return None, 0

        base_txn = _base_tx(user_address)
        if duration_blocks:
            fn = contract.functions.grantWithExpiryFiles(owned, to_address, SHARE_MASK, duration_blocks)
        else:
            fn = contract.functions.grantFiles(owned, to_address, SHARE_MASK)
        base_txn['gas'] = int(fn.estimate_gas(base_txn) * 1.1)
        return fn.build_transaction(base_txn), len(owned)
    except Exception as e:
        logger.error("Batch share prep failed: %s", e)
        raise


# Inherited folder sharing: the users a folder is effectively shared with —
# the intersection of sharedUsers across the owner's existing non-trash files
# under folder_path. Walks up to parent folders when the folder itself has no
# files yet (a freshly created subfolder inherits from its parent). Root is
# never treated as shared, so uploads to "/" inherit nothing.
def folder_share_set(user_address: str, folder_path: str) -> list[str]:
    owner = Web3.to_checksum_address(user_address)
    entries = contract.functions.getUserFiles(owner).call()

    files = []  # (normalized folder, cid) — paths may or may not have a leading slash
    for f in entries:
        levels = [p for p in f[1].split("/") if p]
        if not levels or levels[0] == ".trash":
            continue
        folder = "/" + "/".join(levels[:-1]) if len(levels) > 1 else "/"
        files.append((folder, f[0]))

    path = "/" + folder_path.strip("/")
    while path != "/":
        cids = [cid for fp, cid in files if fp == path or fp.startswith(path + "/")]
        # getUserFiles also returns files shared *to* the owner — keep owned only
        owned_cids = []
        for cid in cids:
            try:
                if contract.functions.getFileOwner(cid).call().lower() == owner.lower():
                    owned_cids.append(cid)
            except Exception as e:
                logger.warning("folder_share_set: getFileOwner failed for %s: %s", cid, e)
        if owned_cids:
            shared = None
            for cid in owned_cids:
                try:
                    users = set(contract.functions.getSharedUsers(cid).call())
                except Exception as e:
                    logger.warning("folder_share_set: getSharedUsers failed for %s: %s", cid, e)
                    users = set()
                shared = users if shared is None else (shared & users)
                if not shared:
                    break
            return sorted(shared or [])
        path = path.rsplit("/", 1)[0] or "/"
    return []


# Deepest folder every one of `full_paths` sits under, as a path without
# leading slash ("" when they share no folder). The paths are full file paths,
# so the filename segment is dropped first.
def common_ancestor_folder(full_paths: list[str]) -> str:
    if not full_paths:
        return ""
    segments = [[p for p in path.split("/") if p][:-1] for path in full_paths]
    common = segments[0]
    for segs in segments[1:]:
        n = 0
        while n < len(common) and n < len(segs) and common[n] == segs[n]:
            n += 1
        common = common[:n]
    return "/".join(common)


# Inherited folder sharing: when the destination folder is already shared, the
# newly uploaded cids are granted to the same people — one grantFiles tx per
# recipient, for the frontend to sign after the upload itself.
#
# Best-effort by design: the upload has already happened and is worth keeping,
# so a failure here is logged and returns empty rather than failing the request.
def prepare_inherited_shares(cids: list[str], folder_path: str, user_address: str):
    if not cids or not folder_path:
        return [], []
    try:
        recipients = folder_share_set(user_address, folder_path)
        if not recipients:
            return [], []
        return prepare_inherited_grant_transactions(cids, recipients, user_address), recipients
    except Exception as e:
        logger.warning("inherited share prep failed for %s: %s", folder_path, e)
        return [], []


# Grants for files being uploaded in the same nonce sequence. The files don't
# exist on-chain yet, so ownership checks and gas estimation would both fail
# ("Not file owner" / "File already exists" state isn't there) — gas is set
# manually and the txs are nonce-offset behind the upload tx, which mines
# first and makes the caller the owner before each grant executes.
def prepare_inherited_grant_transactions(cids: list[str], recipients: list[str], user_address: str, nonce_offset: int = 1):
    user_address = Web3.to_checksum_address(user_address)
    base_nonce = w3.eth.get_transaction_count(user_address)
    txns = []
    for i, to in enumerate(recipients):
        fn = contract.functions.grantFiles(cids, Web3.to_checksum_address(to), SHARE_MASK)
        txns.append(fn.build_transaction({
            'chainId': sepolia_chain_id,
            'gasPrice': _gas_price(),
            'nonce': base_nonce + nonce_offset + i,
            'from': user_address,
            'gas': 150000 * len(cids) + 100000,
        }))
    return txns


# Batch unshare: one revokeFiles(cids, to, mask) tx for folder unshare.
def prepare_unshare_batch_transaction(cids: list[str], to_address: str, user_address: str):
    try:
        user_address = Web3.to_checksum_address(user_address)
        to_address = Web3.to_checksum_address(to_address)

        owned = _owned_only(cids, user_address)
        if not owned:
            return None, 0

        base_txn = _base_tx(user_address)
        fn = contract.functions.revokeFiles(owned, to_address, SHARE_MASK)
        base_txn['gas'] = int(fn.estimate_gas(base_txn) * 1.1)
        return fn.build_transaction(base_txn), len(owned)
    except Exception as e:
        logger.error("Batch unshare prep failed: %s", e)
        raise


# preparing an unshare transaction for owner to sign
def prepare_unshare_transaction(cid: str, to_address: str, user_address: str):
    try:
        user_address = Web3.to_checksum_address(user_address)
        to_address = Web3.to_checksum_address(to_address)
        _assert_owner(cid, user_address, "unshare")

        # Unshare permissions (~READ + ~DOWNLOAD) -> bits are flipped in smart contract
        logger.info("Unsharing CID %s with %s using mask %s", cid, to_address, SHARE_MASK)
        txn = contract.functions.revoke(cid, to_address, SHARE_MASK).build_transaction(
            _base_tx(user_address))
        return txn
    except Exception as e:
        logger.error("Unshare transaction preparation failed: %s", e)
        raise


# preparing a delete transaction for owner to sign
def prepare_delete_transaction(cid: str, user_address: str):
    try:
        txn = contract.functions.deleteFile(cid).build_transaction(
            _base_tx(user_address))
        return txn
    except Exception as e:
        logger.error("Delete transaction prep failed: %s", e)
        raise


def prepare_move_transaction(cid: str, new_path: str, user_address: str):
    try:
        user_address = Web3.to_checksum_address(user_address)
        _assert_owner(cid, user_address, "move")   # could change in permissions in future

        txn = contract.functions.moveFile(cid, new_path).build_transaction(
            _base_tx(user_address))
        return txn
    except Exception as e:
        logger.error("Move transaction prep failed: %s", e)
        raise


# Batch upload: one uploadFiles(cids, paths, formats) tx for folder upload
def prepare_upload_batch_transaction(cids: list[str], paths: list[str], formats: list[str], user_address: str):
    try:
        base_txn = _base_tx(user_address)
        fn = contract.functions.uploadFiles(cids, paths, formats)
        base_txn['gas'] = int(fn.estimate_gas(base_txn) * 1.1)
        return fn.build_transaction(base_txn)
    except Exception as e:
        logger.error("Batch upload prep failed: %s", e)
        raise


# Batch move: one moveFiles(cids, newPaths) tx for folder rename / bulk trash
def prepare_move_batch_transaction(cids: list[str], new_paths: list[str], user_address: str):
    try:
        user_address = Web3.to_checksum_address(user_address)

        # Only the owner can move; filter both lists together
        lower = user_address.lower()
        owned = [(c, p) for c, p in zip(cids, new_paths)
                 if contract.functions.getFileOwner(c).call().lower() == lower]
        if not owned:
            return None, 0
        owned_cids = [c for c, _ in owned]
        owned_paths = [p for _, p in owned]

        base_txn = _base_tx(user_address)
        fn = contract.functions.moveFiles(owned_cids, owned_paths)
        base_txn['gas'] = int(fn.estimate_gas(base_txn) * 1.1)
        return fn.build_transaction(base_txn), len(owned)
    except Exception as e:
        logger.error("Batch move prep failed: %s", e)
        raise


# preparing a delete folder transaction for owner to sign
def prepare_delete_folder(cids: list[str], user_address: str):
    if not cids:
        # Avoid hitting the blockchain if there's nothing to delete
        return None
    try:
        # gasPrice is left for web3 to fill after estimation (no manual margin)
        base_txn = _base_tx(user_address, with_gas_price=False)
        base_txn['gas'] = int(contract.functions.cleanFolder(cids).estimate_gas(base_txn) * 1.1)
        txn = contract.functions.cleanFolder(cids).build_transaction(base_txn)
        return txn
    except Exception as e:
        logger.error("Deleting folder prep failed: %s", e)
        raise


PIN_RM_TIMEOUT = 30
# GC sweeps the whole repo, so it is the slowest call the backend makes and
# needs the widest budget.
GC_TIMEOUT = 300


# Release the bytes behind cids on the local node: drop every pin, then collect
# garbage once. The sweep costs the same whether one pin was dropped or a
# hundred, so it belongs outside the loop — running it per cid made a folder
# delete perform N full sweeps back to back inside a single request.
# Best-effort: this runs after the on-chain delete is already mined, so a node
# problem is reported, never raised.
def unpin_cids(cids: list[str]):
    result = {"unpins": [], "gc_ok": False, "gc_response": None}
    if not cids:
        return result  # nothing released, so nothing for a sweep to collect

    for cid in cids:
        entry = {"cid": cid, "unpin_ok": False, "unpin_response": None}
        try:
            rm = requests.post(f"{IPFS_API_URL}/pin/rm", params={"arg": cid},
                               timeout=PIN_RM_TIMEOUT)
            entry["unpin_response"] = {"status_code": rm.status_code, "text": rm.text}
            entry["unpin_ok"] = rm.ok
        except Exception as e:
            # One unreachable cid must not strand the pins after it.
            logger.warning("unpin failed for %s: %s", cid, e)
            entry["error"] = str(e)
        result["unpins"].append(entry)

    try:
        gc = requests.post(f"{IPFS_API_URL}/repo/gc", timeout=GC_TIMEOUT)
        result["gc_response"] = {"status_code": gc.status_code, "text": gc.text}
        result["gc_ok"] = gc.ok
    except Exception as e:
        logger.warning("repo gc failed: %s", e)
        result["error"] = str(e)

    return result
