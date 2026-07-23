"""Sharing: grant/revoke (single + batch), shared-user queries, recipient
resolution (share by email via Privy), and share-notification emails (SES)."""
import os
import logging
from typing import Optional

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from web3 import Web3

from configure import contract
from models import (
    ShareRequest,
    UnshareRequest,
    ShareBatchRequest,
    NotifyShareRequest,
    ResolveRecipientRequest,
    DeleteBatchRequest,
)
from helpers import (
    prepare_share_transaction,
    prepare_unshare_transaction,
    prepare_share_batch_transaction,
    prepare_unshare_batch_transaction,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# --- Share notification email (Amazon SES over SMTP) ---
SES_SMTP_HOST = os.getenv("SES_SMTP_HOST", "email-smtp.us-east-1.amazonaws.com")
SES_SMTP_PORT = 587
NOTIFY_FROM = os.getenv("NOTIFY_FROM", "Web3FS <notifications@fs.web3db.org>")
APP_URL = os.getenv("APP_URL", "https://fs.web3db.org")

# --- Recipient resolution (share by email via Privy) ---
PRIVY_API_BASE = "https://auth.privy.io/api/v1"


def _privy_auth():
    app_id = os.getenv("PRIVY_APP_ID")
    secret = os.getenv("PRIVY_APP_SECRET")
    if not app_id or not secret:
        return None, None
    return (app_id, secret), {"privy-app-id": app_id}


def _extract_eth_address(privy_user: dict) -> Optional[str]:
    accounts = privy_user.get("linked_accounts", [])
    eth_wallets = [a for a in accounts if a.get("type") == "wallet" and a.get("chain_type") == "ethereum"]
    if not eth_wallets:
        return None
    # Prefer the Privy embedded wallet over linked external ones
    embedded = [w for w in eth_wallets if w.get("wallet_client") == "privy" or w.get("wallet_client_type") == "privy"]
    return (embedded or eth_wallets)[0].get("address")


@router.post("/notify-share")
def notify_share(request: NotifyShareRequest):
    import smtplib
    from email.mime.text import MIMEText

    smtp_user = os.getenv("SES_SMTP_USER")
    smtp_password = os.getenv("SES_SMTP_PASSWORD")
    if not smtp_user or not smtp_password:
        return JSONResponse(status_code=500, content={"error": "SES SMTP not configured on server"})

    to_email = request.recipient_email.strip().lower()
    if "@" not in to_email:
        return JSONResponse(status_code=400, content={"error": "Invalid recipient email"})

    sharer = request.sharer.strip()
    body = (
        f"{sharer} shared \"{request.filename}\" with you on Web3FS.\n\n"
        f"Open {APP_URL} and sign in with this email address ({to_email}) to view the file.\n\n"
        f"— Web3FS"
    )
    msg = MIMEText(body)
    msg["Subject"] = f"{sharer} shared \"{request.filename}\" with you on Web3FS"
    msg["From"] = NOTIFY_FROM
    msg["To"] = to_email

    try:
        with smtplib.SMTP(SES_SMTP_HOST, SES_SMTP_PORT, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(smtp_user, smtp_password)
            smtp.sendmail(NOTIFY_FROM, [to_email], msg.as_string())
        logger.info("[notify-share] sent to %s for file %s", to_email, request.filename)
        return {"sent": True}
    except smtplib.SMTPException as e:
        # Notification is best-effort: the share itself already succeeded
        logger.error("[notify-share] send failed: %s", e)
        return JSONResponse(status_code=502, content={"error": f"Email send failed: {e}"})


@router.post("/resolve-recipient")
async def resolve_recipient(request: ResolveRecipientRequest):
    recipient = request.recipient.strip()

    # Raw address: validate and pass through
    if recipient.startswith("0x"):
        try:
            return {"address": Web3.to_checksum_address(recipient), "existed": True, "pregenerated": False}
        except Exception:
            return JSONResponse(status_code=400, content={"error": "Invalid Ethereum address"})

    if "@" not in recipient:
        return JSONResponse(status_code=400, content={"error": "Recipient must be an email or 0x address"})

    auth, headers = _privy_auth()
    if auth is None:
        return JSONResponse(status_code=500, content={"error": "Privy API not configured on server"})

    email = recipient.lower()
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Look up an existing user by email
        lookup = await client.post(
            f"{PRIVY_API_BASE}/users/email/address",
            json={"address": email}, auth=auth, headers=headers,
        )
        if lookup.status_code == 200:
            address = _extract_eth_address(lookup.json())
            if not address:
                return JSONResponse(status_code=404, content={"error": "User exists but has no Ethereum wallet"})
            return {"address": address, "existed": True, "pregenerated": False}

        # The email-address lookup only matches `email` accounts; users who
        # signed in with Google have a `google_oauth` account instead. Scan
        # the user list for a matching Google email before pregenerating.
        all_users = await client.get(f"{PRIVY_API_BASE}/users", auth=auth, headers=headers)
        if all_users.status_code == 200:
            for u in all_users.json().get("data", []):
                for acct in u.get("linked_accounts", []):
                    if acct.get("type") == "google_oauth" and (acct.get("email") or "").lower() == email:
                        address = _extract_eth_address(u)
                        if address:
                            return {"address": address, "existed": True, "pregenerated": False}

        # Unknown email: pregenerate a user + embedded wallet they claim on first login
        created = await client.post(
            f"{PRIVY_API_BASE}/users",
            json={
                "create_ethereum_wallet": True,
                "linked_accounts": [{"type": "email", "address": email}],
            },
            auth=auth, headers=headers,
        )
        if created.status_code not in (200, 201):
            logger.error("[resolve-recipient] Privy user creation failed: %s %s", created.status_code, created.text)
            return JSONResponse(status_code=502, content={"error": "Could not create wallet for that email"})
        address = _extract_eth_address(created.json())
        if not address:
            return JSONResponse(status_code=502, content={"error": "Wallet creation returned no address"})
        logger.info("[resolve-recipient] pregenerated wallet %s for %s", address, email)
        return {"address": address, "existed": False, "pregenerated": True}


# endpoint for sharing a file (frontend has to sign)
@router.post("/share")
def share_file(request: ShareRequest):
    try:
        txn = prepare_share_transaction(request.cid, request.to_address, request.user_address)
        return {"transaction": txn}
    except Exception as e:
        logger.error("Failed to prepare share transaction: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


# Batch share (folder share): one grantFiles tx covering many cids
@router.post("/share-batch")
def share_batch(request: ShareBatchRequest):
    try:
        txn, count = prepare_share_batch_transaction(request.cids, request.to_address, request.user_address)
        if txn is None:
            return JSONResponse(status_code=400, content={"error": "No owned files to share"})
        return {"transaction": txn, "count": count}
    except Exception as e:
        logger.error("Failed to prepare batch share transaction: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


# Batch unshare (folder unshare): one revokeFiles tx covering many cids
@router.post("/unshare-batch")
def unshare_batch(request: ShareBatchRequest):
    try:
        txn, count = prepare_unshare_batch_transaction(request.cids, request.to_address, request.user_address)
        if txn is None:
            return JSONResponse(status_code=400, content={"error": "No owned files to unshare"})
        return {"transaction": txn, "count": count}
    except Exception as e:
        logger.error("Failed to prep batch unshare transaction: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


# endpoint for unsharing a file (frontend has to sign)
@router.post("/unshare")
def unshare_file(request: UnshareRequest):
    try:
        txn = prepare_unshare_transaction(request.cid, request.to_address, request.user_address)
        return {"transaction": txn}
    except Exception as e:
        logger.error("Failed to prep unshare transaction: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/shared-users")
def get_shared_users(cid: str, user_address: str):
    try:
        owner = contract.functions.getFileOwner(cid).call()
        shared_user = Web3.to_checksum_address(user_address)
        owner_checksum = Web3.to_checksum_address(owner)

        if shared_user.lower() == owner_checksum.lower():
            shared_users = contract.functions.getSharedUsers(cid).call()
            return {"shared_with": shared_users}
        else:
            return {"shared_by": owner}
    except Exception as e:
        logger.error("Error fetching shared users for CID %s: %s", cid, e)
        return {"shared_with": [], "error": str(e)}


# Folder share modal: union of shared users across every owned cid in the
# folder (POST because a folder can hold more cids than a query string fits)
@router.post("/shared-users-batch")
def get_shared_users_batch(request: DeleteBatchRequest):
    try:
        requester = Web3.to_checksum_address(request.user_address)
        users = set()
        for cid in request.cids:
            owner = contract.functions.getFileOwner(cid).call()
            if owner.lower() != requester.lower():
                continue
            for u in contract.functions.getSharedUsers(cid).call():
                users.add(u)
        return {"shared_with": sorted(users)}
    except Exception as e:
        logger.error("Error fetching shared users batch: %s", e)
        return {"shared_with": [], "error": str(e)}
