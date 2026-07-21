"""Auth token issuance: verify a signed login message, return an HMAC token."""
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from eth_account import Account
from eth_account.messages import encode_defunct

from models import AuthTokenRequest
from security import (
    auth_message,
    _token_signature,
    AUTH_TOKEN_TTL,
    AUTH_MESSAGE_MAX_AGE,
)

router = APIRouter()


@router.post("/auth/token")
async def issue_auth_token(request: AuthTokenRequest):
    if abs(time.time() - request.timestamp) > AUTH_MESSAGE_MAX_AGE:
        return JSONResponse(status_code=400, content={"error": "Login message expired, retry"})
    try:
        message = auth_message(request.address, request.timestamp)
        recovered = Account.recover_message(encode_defunct(text=message), signature=request.signature)
    except Exception as e:
        print(f"Auth signature recovery failed: {e}")
        return JSONResponse(status_code=401, content={"error": "Invalid signature"})
    if recovered.lower() != request.address.lower():
        return JSONResponse(status_code=401, content={"error": "Signature does not match address"})

    address = request.address.lower()
    expiry = int(time.time()) + AUTH_TOKEN_TTL
    payload = f"{address}.{expiry}"
    return {"token": f"{payload}.{_token_signature(payload)}", "expires": expiry}
