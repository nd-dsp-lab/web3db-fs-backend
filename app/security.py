"""Download authorization: wallet-signature login -> short-lived HMAC token.

The frontend proves wallet ownership once (personal_sign) and gets a token;
the download and thumbnail routes then check on-chain permissions for the
token's address instead of trusting a spoofable user_address query param.
"""
import os
import time
import logging
import hmac as hmac_mod
import hashlib
import secrets as secrets_mod

from web3 import Web3
from fastapi.responses import JSONResponse

from configure import contract
from permissions import DOWNLOAD

logger = logging.getLogger(__name__)

AUTH_SECRET_FILE = os.path.join(os.path.dirname(__file__), "..", "auth_secret.txt")
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
        logger.warning("Permission check failed for %s/%s: %s", cid, address, e)
        return False


def require_download_access(cid: str, token: str):
    """Returns an error JSONResponse, or None if access is allowed."""
    address = verify_auth_token(token or "")
    if not address:
        return JSONResponse(status_code=401, content={"error": "Missing or invalid auth token"})
    if not can_download(cid, address):
        return JSONResponse(status_code=403, content={"error": "No download permission for this file"})
    return None
