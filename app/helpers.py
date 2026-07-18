from typing import Optional
import requests
from web3 import Web3
from configure import w3, contract, IPFS_API_URL
from permissions import READ, WRITE, DOWNLOAD, DELETE, SHARE, MOVE, CHANGE_OWNER, CHANGE_ROLE
from web3.datastructures import AttributeDict

sepolia_chain_id = 11155111

# Price gas 25% above the node's quote. The quote lags the network (and
# Infura can serve stale reads), which left transactions stuck in the
# mempool; the margin also lets a retry replace a stuck tx at the same nonce.
def _gas_price():
    return int(w3.eth.gas_price * 1.25)

# return transaction data for frontend to sign
# nonce_offset: batch endpoints prepare several txs before any is broadcast,
# so the chain nonce doesn't advance between calls — offset each tx manually.
def prepare_upload_transaction(cid: str, full_path: str, user_address: str, file_format: Optional[str] = None, nonce_offset: int = 0):
    print(f"[prepare_upload_transaction] CID={cid}, full_path={full_path}")
    try:
        user_address = Web3.to_checksum_address(user_address)
        nonce = w3.eth.get_transaction_count(user_address) + nonce_offset
        gas_price = _gas_price()
        
        # Build transaction but don't sign it
        txn = contract.functions.uploadFile(cid, full_path, file_format or "").build_transaction({
            'chainId': sepolia_chain_id,    # required for Sepolia
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
        gas_price = _gas_price()
 
        # Ensure ownership
        owner = contract.functions.getFileOwner(cid).call()
        if owner.lower() != user_address.lower():
            raise Exception("Only file owner can share file.")
        
        # Share permissions (READ + DOWNLOAD)
        grant_mask = READ | DOWNLOAD

        print(f"Sharing CID {cid} with {to_address} using mask {grant_mask}")

        # Build transaction but don't sign it
        txn = contract.functions.grant(cid, to_address, grant_mask).build_transaction({
            'chainId': sepolia_chain_id,    # required for Sepolia
            'gasPrice': gas_price,
            'nonce': nonce,
            'from': user_address
        })
        
        return txn
    except Exception as e:
        print("Share transaction preparation failed:", e)
        raise

# Batch share: one grantFiles(cids, to, mask) tx for folder share.
# Filters to cids the caller owns; returns (txn, count) or (None, 0).
def prepare_share_batch_transaction(cids: list[str], to_address: str, user_address: str):
    try:
        user_address = Web3.to_checksum_address(user_address)
        to_address = Web3.to_checksum_address(to_address)

        owned = [c for c in cids
                 if contract.functions.getFileOwner(c).call().lower() == user_address.lower()]
        if not owned:
            return None, 0

        grant_mask = READ | DOWNLOAD

        base_txn = {
            'chainId': sepolia_chain_id,
            'gasPrice': _gas_price(),
            'nonce': w3.eth.get_transaction_count(user_address),
            'from': user_address,
        }
        fn = contract.functions.grantFiles(owned, to_address, grant_mask)
        base_txn['gas'] = int(fn.estimate_gas(base_txn) * 1.1)
        return fn.build_transaction(base_txn), len(owned)
    except Exception as e:
        print("Batch share prep failed:", e)
        raise

# Batch unshare: one revokeFiles(cids, to, mask) tx for folder unshare.
def prepare_unshare_batch_transaction(cids: list[str], to_address: str, user_address: str):
    try:
        user_address = Web3.to_checksum_address(user_address)
        to_address = Web3.to_checksum_address(to_address)

        owned = [c for c in cids
                 if contract.functions.getFileOwner(c).call().lower() == user_address.lower()]
        if not owned:
            return None, 0

        revoke_mask = READ | DOWNLOAD

        base_txn = {
            'chainId': sepolia_chain_id,
            'gasPrice': _gas_price(),
            'nonce': w3.eth.get_transaction_count(user_address),
            'from': user_address,
        }
        fn = contract.functions.revokeFiles(owned, to_address, revoke_mask)
        base_txn['gas'] = int(fn.estimate_gas(base_txn) * 1.1)
        return fn.build_transaction(base_txn), len(owned)
    except Exception as e:
        print("Batch unshare prep failed:", e)
        raise

# preparing an unshare transaction for owner to sign
def prepare_unshare_transaction(cid: str, to_address: str, user_address: str):
    try:
        user_address = Web3.to_checksum_address(user_address)
        to_address = Web3.to_checksum_address(to_address)
        nonce = w3.eth.get_transaction_count(user_address)
        gas_price = _gas_price()

        # Ensure ownership
        owner = contract.functions.getFileOwner(cid).call()
        if owner.lower() != user_address.lower():
            raise Exception("Only file owner can unshare file.")

        # Unshare permissions (~READ + ~DOWNLOAD) -> bits are flipped in smart contract
        revoke_mask = READ | DOWNLOAD

        print(f"Unsharing CID {cid} with {to_address} using mask {revoke_mask}")

        txn = contract.functions.revoke(cid, to_address, revoke_mask).build_transaction({
            'chainId': sepolia_chain_id,    # required for Sepolia
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
        gas_price = _gas_price()

        txn = contract.functions.deleteFile(cid).build_transaction({
            'chainId': sepolia_chain_id,
            'gasPrice': gas_price,
            'nonce': nonce,
            'from': user_address
        })
        return txn
    except Exception as e:
        print("Delete transaction prep failed:", e)
        raise

def prepare_move_transaction(cid: str, new_path: str, user_address: str):
    try:
        user_address = Web3.to_checksum_address(user_address)
        nonce = w3.eth.get_transaction_count(user_address)
        gas_price = _gas_price()

        owner = contract.functions.getFileOwner(cid).call()
        if owner.lower() != user_address.lower():
            raise Exception("Only file owner can move file.")   # could change in permissions in future
        
        txn = contract.functions.moveFile(cid, new_path).build_transaction({
            'chainId': sepolia_chain_id,
            'gasPrice': gas_price,
            'nonce': nonce,
            'from': user_address
        })
        return txn
    except Exception as e:
        print("Move transaction prep failed:", e)

# Batch upload: one uploadFiles(cids, paths, formats) tx for folder upload
def prepare_upload_batch_transaction(cids: list[str], paths: list[str], formats: list[str], user_address: str):
    try:
        user_address = Web3.to_checksum_address(user_address)
        base_txn = {
            'chainId': sepolia_chain_id,
            'gasPrice': _gas_price(),
            'nonce': w3.eth.get_transaction_count(user_address),
            'from': user_address,
        }
        fn = contract.functions.uploadFiles(cids, paths, formats)
        base_txn['gas'] = int(fn.estimate_gas(base_txn) * 1.1)
        return fn.build_transaction(base_txn)
    except Exception as e:
        print("Batch upload prep failed:", e)
        raise

# Batch move: one moveFiles(cids, newPaths) tx for folder rename / bulk trash
def prepare_move_batch_transaction(cids: list[str], new_paths: list[str], user_address: str):
    try:
        user_address = Web3.to_checksum_address(user_address)

        # Only the owner can move; filter both lists together
        owned = [(c, p) for c, p in zip(cids, new_paths)
                 if contract.functions.getFileOwner(c).call().lower() == user_address.lower()]
        if not owned:
            return None, 0
        owned_cids = [c for c, _ in owned]
        owned_paths = [p for _, p in owned]

        base_txn = {
            'chainId': sepolia_chain_id,
            'gasPrice': _gas_price(),
            'nonce': w3.eth.get_transaction_count(user_address),
            'from': user_address,
        }
        fn = contract.functions.moveFiles(owned_cids, owned_paths)
        base_txn['gas'] = int(fn.estimate_gas(base_txn) * 1.1)
        return fn.build_transaction(base_txn), len(owned)
    except Exception as e:
        print("Batch move prep failed:", e)
        raise

# preparing a delete folder transaction for owner to sign
def prepare_delete_folder(cids: list[str], user_address: str):
    if not cids:
        # Avoid hitting the blockchain if there's nothing to delete
        return None
    try: 
        user_address = Web3.to_checksum_address(user_address)
        nonce = w3.eth.get_transaction_count(user_address)
        base_txn = {
            'chainId': sepolia_chain_id,
            'nonce': nonce,
            'from': user_address,
        }

        estimated_gas = contract.functions.cleanFolder(cids).estimate_gas(base_txn)
        
        base_txn['gas'] = int(estimated_gas * 1.1)

        txn = contract.functions.cleanFolder(cids).build_transaction(base_txn)
        
        return txn
    except Exception as e: 
        print("Deleting folder prep failed", e)
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

# check if upload transaction succeeded
# def upload_tx_succeeded(receipt: AttributeDict) -> tuple[bool, Optional[str]]:
#     uploaded = contract.events.FileUploaded().process_receipt(receipt)
#     return bool(uploaded), uploaded[0]["args"]["cid"] if uploaded else None