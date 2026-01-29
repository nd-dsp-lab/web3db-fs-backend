from typing import Optional
import requests
from web3 import Web3
from configure import w3, contract, IPFS_API_URL
from permissions import READ, WRITE, DOWNLOAD, DELETE, SHARE, MOVE, CHANGE_OWNER, CHANGE_ROLE


# return transaction data for frontend to sign
def prepare_upload_transaction(cid: str, full_path: str, user_address: str, file_format: Optional[str] = None):
    print(f"[prepare_upload_transaction] CID={cid}, full_path={full_path}")
    try:
        user_address = Web3.to_checksum_address(user_address)
        nonce = w3.eth.get_transaction_count(user_address)
        gas_price = w3.eth.gas_price
        
        # Build transaction but don't sign it
        txn = contract.functions.uploadFile(cid, full_path, file_format or "").build_transaction({
            'chainId': 11155111,    # required for Sepolia
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
        gas_price = w3.eth.gas_price
 
        # Ensure ownership
        owner = contract.functions.getFileOwner(cid).call()
        if owner.lower() != user_address.lower():
            raise Exception("Only file owner can share file.")
        
        # Share permissions (READ + DOWNLOAD)
        grant_mask = READ | DOWNLOAD

        print(f"Sharing CID {cid} with {to_address} using mask {grant_mask}")

        # Build transaction but don't sign it
        txn = contract.functions.grant(cid, to_address, grant_mask).build_transaction({
            'chainId': 11155111,    # required for Sepolia
            'gasPrice': gas_price,
            'nonce': nonce,
            'from': user_address
        })
        
        return txn
    except Exception as e:
        print("Share transaction preparation failed:", e)
        raise

# preparing an unshare transaction for owner to sign
def prepare_unshare_transaction(cid: str, to_address: str, user_address: str):
    try:
        user_address = Web3.to_checksum_address(user_address)
        to_address = Web3.to_checksum_address(to_address)
        nonce = w3.eth.get_transaction_count(user_address)
        gas_price = w3.eth.gas_price

        # Ensure ownership
        owner = contract.functions.getFileOwner(cid).call()
        if owner.lower() != user_address.lower():
            raise Exception("Only file owner can unshare file.")

        # Unshare permissions (~READ + ~DOWNLOAD) -> bits are flipped in smart contract
        revoke_mask = READ | DOWNLOAD

        print(f"Unsharing CID {cid} with {to_address} using mask {revoke_mask}")

        txn = contract.functions.revoke(cid, to_address, revoke_mask).build_transaction({
            'chainId': 11155111,    # required for Sepolia
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
        gas_price = w3.eth.gas_price

        txn = contract.functions.deleteFile(cid).build_transaction({
            'chainId': 11155111,
            'gasPrice': gas_price,
            'nonce': nonce,
            'from': user_address
        })
        return txn
    except Exception as e:
        print("Delete transaction prep failed:", e)
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