"""Transaction relay: the browser signs, the enclave broadcasts.

The chain runs on the enclave host's loopback and is not reachable from a
browser, so a wallet cannot broadcast its own transactions. It signs them
instead and posts the raw bytes here.

This does not widen the trust model. The signature covers every field of the
transaction, so the relay cannot alter what it forwards — it can only decline
to send it. The contract still decides what the sender is allowed to do, and
the enclave never holds a user key.

The token check is about cost, not authority. The chain prices gas at zero,
so an open relay would be free to spam. Requiring a login token, and matching
it against the address recovered from the signature, keeps the relay to
wallets that have already proved ownership.
"""
import logging
from typing import Optional

from eth_account import Account
from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from web3 import Web3

from configure import w3
from models import BroadcastRequest
from security import verify_auth_token

logger = logging.getLogger(__name__)

router = APIRouter()

# A node that already holds the transaction says so rather than accepting it
# again. That is a successful retry, not a failure: the hash is a function of
# the signed bytes, so it is the same one the first attempt produced.
_ALREADY_BROADCAST = ("already known", "known transaction", "already exists")


@router.post("/broadcast")
def broadcast(request: BroadcastRequest, x_auth_token: Optional[str] = Header(None)):
    caller = verify_auth_token(x_auth_token or "")
    if not caller:
        logger.warning("[broadcast] rejected: missing or invalid auth token")
        return JSONResponse(status_code=401,
                            content={"error": "Missing or invalid auth token"})

    raw = request.raw_transaction.strip()
    try:
        raw_bytes = Web3.to_bytes(hexstr=raw)
    except Exception:
        return JSONResponse(status_code=400,
                            content={"error": "raw_transaction is not hex"})

    # Recover the signer before spending a round trip on the node. A valid
    # signature from a different address would mean the token holder is
    # relaying someone else's transaction, which nothing in the app does.
    try:
        signer = Account.recover_transaction(raw)
    except Exception as e:
        logger.warning("[broadcast] undecodable transaction from %s: %s", caller, e)
        return JSONResponse(status_code=400,
                            content={"error": "Could not recover the signer"})

    if signer.lower() != caller.lower():
        logger.warning("[broadcast] token for %s relayed a transaction signed by %s",
                       caller, signer)
        return JSONResponse(status_code=403,
                            content={"error": "Transaction is not signed by the authenticated wallet"})

    try:
        tx_hash = w3.eth.send_raw_transaction(raw_bytes)
    except Exception as e:
        if any(m in str(e).lower() for m in _ALREADY_BROADCAST):
            tx_hash = Web3.keccak(raw_bytes)
            logger.info("[broadcast] %s was already in the pool for %s",
                        tx_hash.hex(), signer)
            return {"tx_hash": tx_hash.hex()}
        logger.error("[broadcast] node refused a transaction from %s: %s", signer, e)
        return JSONResponse(status_code=502,
                            content={"error": f"Node refused the transaction: {e}"})

    logger.info("[broadcast] relayed %s for %s", tx_hash.hex(), signer)
    return {"tx_hash": tx_hash.hex()}
