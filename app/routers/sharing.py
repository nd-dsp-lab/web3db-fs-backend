"""Sharing: grant/revoke (single + batch), shared-user queries, recipient
resolution (share by email via Privy), and share-notification emails (SES)."""
import os
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from web3 import Web3

from configure import contract
from security import verify_auth_token
from models import (
    ShareRequest,
    UnshareRequest,
    ShareBatchRequest,
    UnshareBatchRequest,
    NotifyShareRequest,
    ResolveRecipientRequest,
    CidBatchRequest,
    NotifyRequestRequest,
    NotifyDecisionRequest,
    RequestAccessRequest,
    ApproveRequestRequest,
    DenyRequestRequest,
    CancelRequestRequest,
)
from helpers import (
    NotOwnerError,
    prepare_share_transaction,
    prepare_unshare_transaction,
    prepare_share_batch_transaction,
    prepare_unshare_batch_transaction,
    prepare_request_access_transaction,
    prepare_approve_request_transaction,
    prepare_deny_request_transaction,
    prepare_cancel_request_transaction,
)
from constants import REQUEST_STATUS

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


class EmailNotConfigured(Exception):
    """SES credentials are absent — the server cannot send mail at all."""


def _send_email(to_email: str, subject: str, body: str):
    """Send one plain-text notification. Raises EmailNotConfigured when SES
    isn't set up, smtplib.SMTPException when the send itself fails; every
    caller treats both as best-effort, since the on-chain action they are
    reporting has already happened and is not being undone."""
    import smtplib
    from email.mime.text import MIMEText

    smtp_user = os.getenv("SES_SMTP_USER")
    smtp_password = os.getenv("SES_SMTP_PASSWORD")
    if not smtp_user or not smtp_password:
        raise EmailNotConfigured()

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = NOTIFY_FROM
    msg["To"] = to_email
    with smtplib.SMTP(SES_SMTP_HOST, SES_SMTP_PORT, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        smtp.sendmail(NOTIFY_FROM, [to_email], msg.as_string())


def _privy_email_for_address(address: str) -> Optional[str]:
    """Reverse of /resolve-recipient: a wallet address -> the email to reach
    them at. Needed because a request names addresses, not people.

    Tries the direct wallet lookup first, then falls back to scanning the user
    list -- the same fallback /resolve-recipient already needs, because Google
    sign-ins carry a google_oauth account rather than an email one.
    Returns None when the address belongs to nobody we can email."""
    auth, headers = _privy_auth()
    if auth is None:
        return None

    def email_of(user: dict) -> Optional[str]:
        accounts = user.get("linked_accounts", [])
        for a in accounts:
            if a.get("type") == "email" and a.get("address"):
                return a["address"].lower()
        for a in accounts:
            if a.get("type") == "google_oauth" and a.get("email"):
                return a["email"].lower()
        return None

    wanted = address.lower()
    try:
        with httpx.Client(timeout=15.0) as client:
            direct = client.post(f"{PRIVY_API_BASE}/users/wallet/address",
                                 json={"address": address}, auth=auth, headers=headers)
            if direct.status_code == 200:
                found = email_of(direct.json())
                if found:
                    return found

            listed = client.get(f"{PRIVY_API_BASE}/users", auth=auth, headers=headers)
            if listed.status_code == 200:
                for u in listed.json().get("data", []):
                    for acct in u.get("linked_accounts", []):
                        if (acct.get("type") == "wallet"
                                and (acct.get("address") or "").lower() == wanted):
                            return email_of(u)
    except Exception as e:
        logger.warning("[privy] email lookup failed for %s: %s", address, e)
    return None


def _filename_of(cid: str) -> str:
    try:
        full_path = contract.functions.fileMetadata(cid).call()[1]
    except Exception as e:
        logger.warning("fileMetadata failed for %s: %s", cid, e)
        return cid
    levels = [p for p in full_path.split("/") if p]
    return levels[-1] if levels else full_path


@router.post("/notify-share")
def notify_share(request: NotifyShareRequest):
    to_email = request.recipient_email.strip().lower()
    if "@" not in to_email:
        return JSONResponse(status_code=400, content={"error": "Invalid recipient email"})

    sharer = request.sharer.strip()
    body = (
        f"{sharer} shared \"{request.filename}\" with you on Web3FS.\n\n"
        f"Open {APP_URL} and sign in with this email address ({to_email}) to view the file.\n\n"
        f"— Web3FS"
    )
    try:
        _send_email(to_email, f"{sharer} shared \"{request.filename}\" with you on Web3FS", body)
        logger.info("[notify-share] sent to %s for file %s", to_email, request.filename)
        return {"sent": True}
    except EmailNotConfigured:
        return JSONResponse(status_code=500, content={"error": "SES SMTP not configured on server"})
    except Exception as e:
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
        txn = prepare_share_transaction(request.cid, request.to_address, request.user_address, request.duration_blocks)
        return {"transaction": txn}
    except Exception as e:
        logger.error("Failed to prepare share transaction: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


# Batch share (folder share): one grantFiles tx covering many cids
@router.post("/share-batch")
def share_batch(request: ShareBatchRequest):
    try:
        txn, count = prepare_share_batch_transaction(request.cids, request.to_address, request.user_address, request.duration_blocks)
        if txn is None:
            return JSONResponse(status_code=400, content={"error": "No owned files to share"})
        return {"transaction": txn, "count": count}
    except Exception as e:
        logger.error("Failed to prepare batch share transaction: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


# Batch unshare (folder unshare): one revokeFiles tx covering many cids
@router.post("/unshare-batch")
def unshare_batch(request: UnshareBatchRequest):
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


# A file's recipient list is private to its owner, so the requester comes from
# the auth token, never from a caller-supplied address: cids are public, so a
# user_address query param would let anyone read any owner's sharing graph.
@router.get("/shared-users")
def get_shared_users(cid: str, x_auth_token: Optional[str] = Header(None)):
    requester = verify_auth_token(x_auth_token or "")
    if not requester:
        logger.warning("shared-users denied for %s: missing or invalid auth token", cid)
        return JSONResponse(status_code=401, content={"error": "Missing or invalid auth token"})
    try:
        owner = contract.functions.getFileOwner(cid).call()
        if owner.lower() == requester.lower():
            # getSharedUsers returns every address ever granted access;
            # getPermissions is expiry-aware (0 once _expiresAtBlock has
            # passed), so it says which of them still have access *now*.
            #
            # Lapsed recipients are listed rather than dropped, flagged so the
            # UI can grey them out and say when their access ended. Dropping
            # them made someone whose access ran out disappear from "People
            # with access" with no trace they had ever been there. A *revoked*
            # recipient does disappear, and correctly so -- _revoke removes
            # them from getSharedUsers outright, so nothing here has to tell
            # the two cases apart.
            #
            # expires_at_block is the absolute block number the UI shows
            # directly (0 -> None means permanent) -- deliberately not a
            # "blocks remaining" countdown, which would go stale the moment
            # it's rendered.
            shared_with = []
            for addr in contract.functions.getSharedUsers(cid).call():
                lapsed = contract.functions.getPermissions(cid, addr).call() == 0
                expires_at = contract.functions.getExpiresAtBlock(cid, addr).call()
                shared_with.append({
                    "address": addr,
                    "expires_at_block": expires_at or None,
                    "lapsed": lapsed,
                })
            return {"shared_with": shared_with}
        else:
            return {"shared_by": owner}
    except Exception as e:
        logger.error("Error fetching shared users for CID %s: %s", cid, e)
        return {"shared_with": [], "error": str(e)}


# Folder share modal: union of shared users across every owned cid in the
# folder (POST because a folder can hold more cids than a query string fits).
# The requester comes from the token, so the body carries cids and nothing else.
@router.post("/shared-users-batch")
def get_shared_users_batch(request: CidBatchRequest, x_auth_token: Optional[str] = Header(None)):
    requester = verify_auth_token(x_auth_token or "")
    if not requester:
        logger.warning("shared-users-batch denied: missing or invalid auth token")
        return JSONResponse(status_code=401, content={"error": "Missing or invalid auth token"})
    try:
        best = {}    # address -> soonest expires_at_block (None = permanent)
        live = {}    # address -> still has access to at least one cid here
        for cid in request.cids:
            owner = contract.functions.getFileOwner(cid).call()
            if owner.lower() != requester.lower():
                continue
            for u in contract.functions.getSharedUsers(cid).call():
                lapsed_here = contract.functions.getPermissions(cid, u).call() == 0
                expires_at = contract.functions.getExpiresAtBlock(cid, u).call() or None
                # A folder is a union, so someone can be active on one file and
                # lapsed on another. They count as lapsed only when they have
                # lost access to every file here -- otherwise they can still
                # open part of the folder, and calling that "expired" would be
                # its own kind of wrong.
                live[u] = live.get(u, False) or not lapsed_here
                if u not in best:
                    best[u] = expires_at
                elif expires_at is not None and (best[u] is None or expires_at < best[u]):
                    # Show the soonest deadline, so a permanent grant on one
                    # file never hides a soon-to-expire grant on another.
                    best[u] = expires_at
        shared_with = [
            {"address": addr, "expires_at_block": best[addr], "lapsed": not live[addr]}
            for addr in sorted(best)
        ]
        return {"shared_with": shared_with}
    except Exception as e:
        logger.error("Error fetching shared users batch: %s", e)
        return {"shared_with": [], "error": str(e)}


# --- access extension requests ---
# A share that has expired stays visible to its recipient, greyed out, so they
# can ask the owner for more time instead of watching the file vanish. The
# contract holds the whole negotiation; these endpoints only prepare the
# transactions each side signs, exactly like /share does.
@router.post("/request-extension")
def request_extension(request: RequestAccessRequest):
    try:
        txn = prepare_request_access_transaction(
            request.cid, request.user_address, request.duration_blocks)
        return {"transaction": txn}
    except Exception as e:
        logger.error("Failed to prep request-extension transaction: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/approve-request")
def approve_request(request: ApproveRequestRequest):
    try:
        txn = prepare_approve_request_transaction(
            request.cid, request.requester, request.user_address, request.duration_blocks)
        return {"transaction": txn}
    except NotOwnerError as e:
        return JSONResponse(status_code=403, content={"error": str(e)})
    except Exception as e:
        logger.error("Failed to prep approve-request transaction: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/deny-request")
def deny_request(request: DenyRequestRequest):
    try:
        txn = prepare_deny_request_transaction(
            request.cid, request.requester, request.user_address)
        return {"transaction": txn}
    except NotOwnerError as e:
        return JSONResponse(status_code=403, content={"error": str(e)})
    except Exception as e:
        logger.error("Failed to prep deny-request transaction: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/cancel-request")
def cancel_request(request: CancelRequestRequest):
    try:
        txn = prepare_cancel_request_transaction(request.cid, request.user_address)
        return {"transaction": txn}
    except Exception as e:
        logger.error("Failed to prep cancel-request transaction: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


# Every open request across all of the caller's files, for the "Shared with
# others" view. The owner comes from the auth token and never from a
# parameter: getPendingRequests takes an address, so a caller-supplied one
# would let anyone read anyone else's pending requests -- the same reason
# /shared-users identifies its caller this way.
@router.get("/share-requests")
def get_share_requests(x_auth_token: Optional[str] = Header(None)):
    requester = verify_auth_token(x_auth_token or "")
    if not requester:
        logger.warning("share-requests denied: missing or invalid auth token")
        return JSONResponse(status_code=401, content={"error": "Missing or invalid auth token"})
    try:
        owner = Web3.to_checksum_address(requester)
        cids, requesters, durations, requested_at = \
            contract.functions.getPendingRequests(owner).call()

        pending = []
        for cid, addr, duration, at_block in zip(cids, requesters, durations, requested_at):
            # The contract has no idea a file is in the trash -- trashing is a
            # moveFile, not a permission change -- so a request against a
            # trashed file would otherwise surface here.
            try:
                full_path = contract.functions.fileMetadata(cid).call()[1]
            except Exception as e:
                logger.warning("fileMetadata failed for %s: %s", cid, e)
                full_path = ""
            levels = [p for p in full_path.split("/") if p]
            if levels and levels[0] == ".trash":
                continue
            pending.append({
                "cid": cid,
                "filename": levels[-1] if levels else full_path,
                "requester": addr,
                "duration_blocks": duration,
                "requested_at_block": at_block,
            })
        return {"requests": pending}
    except Exception as e:
        logger.error("Error fetching share requests for %s: %s", requester, e)
        return {"requests": [], "error": str(e)}


# --- request-flow notifications ---
# Both endpoints derive every party from the chain and the auth token, never
# from the body: the caller cannot name who gets emailed. They also refuse to
# send unless the on-chain state actually supports the claim, so neither can
# be used to mail someone repeatedly by replaying the request.
#
# Best-effort throughout, like /notify-share: the transaction being reported
# has already mined, and a failed email is not worth failing the action over.

@router.post("/notify-request")
def notify_request(request: NotifyRequestRequest, x_auth_token: Optional[str] = Header(None)):
    """Tell a file's owner that someone has asked for more time."""
    requester = verify_auth_token(x_auth_token or "")
    if not requester:
        return JSONResponse(status_code=401, content={"error": "Missing or invalid auth token"})
    try:
        requester_cs = Web3.to_checksum_address(requester)
        # Only a request that is actually pending on-chain earns an email.
        status = contract.functions.getRequest(request.cid, requester_cs).call()[3]
        if REQUEST_STATUS[status] != "pending":
            logger.warning("[notify-request] no pending request from %s on %s", requester, request.cid)
            return JSONResponse(status_code=409, content={"error": "No pending request for this file"})

        owner = contract.functions.getFileOwner(request.cid).call()
        to_email = _privy_email_for_address(owner)
        if not to_email:
            logger.info("[notify-request] no email on file for owner %s", owner)
            return {"sent": False, "reason": "Owner has no email address"}

        filename = _filename_of(request.cid)
        short = f"{requester_cs[:6]}...{requester_cs[-4:]}"
        body = (
            f"{short} is asking for more time on \"{filename}\", which you shared with them.\n\n"
            f"Their access has expired. Open {APP_URL} and go to \"Shared with others\" "
            f"to approve or decline.\n\n"
            f"— Web3FS"
        )
        _send_email(to_email, f"{short} asked for more time on \"{filename}\"", body)
        logger.info("[notify-request] sent to owner for %s", request.cid)
        return {"sent": True}
    except EmailNotConfigured:
        return JSONResponse(status_code=500, content={"error": "SES SMTP not configured on server"})
    except Exception as e:
        logger.error("[notify-request] failed: %s", e)
        return JSONResponse(status_code=502, content={"error": str(e)})


def _decision_email(cid: str, requester: str, caller: str, expect: str):
    """Shared body of /notify-approve and /notify-deny: both are the owner
    telling one requester what they decided, and differ only in wording.

    Every party is read from the chain and the token, so the caller cannot aim
    an email at someone by naming them, and nothing is sent unless the chain
    already says `expect` happened -- otherwise these are a way to mail a
    person on demand by replaying the call."""
    owner = contract.functions.getFileOwner(cid).call()
    if owner.lower() != caller.lower():
        logger.warning("[notify-%s] %s does not own %s", expect, caller, cid)
        return JSONResponse(status_code=403, content={"error": "Only the file owner can send this"})

    requester_cs = Web3.to_checksum_address(requester)
    status = contract.functions.getRequest(cid, requester_cs).call()[3]
    if REQUEST_STATUS[status] != expect:
        logger.warning("[notify-%s] on-chain status for %s is %s", expect, cid, REQUEST_STATUS[status])
        return JSONResponse(status_code=409, content={"error": "On-chain status does not match"})

    to_email = _privy_email_for_address(requester_cs)
    if not to_email:
        logger.info("[notify-%s] no email on file for %s", expect, requester_cs)
        return {"sent": False, "reason": "Requester has no email address"}

    filename = _filename_of(cid)
    if expect == "approved":
        expires_at = contract.functions.getExpiresAtBlock(cid, requester_cs).call()
        when = f" Your access now runs until block {expires_at:,}." if expires_at else ""
        subject = f'Your access to "{filename}" was extended'
        body = (f'The owner of "{filename}" approved your request for more time.{when}\n\n'
                f"Open {APP_URL} to view the file.\n\n— Web3FS")
    else:
        subject = f'Your request for "{filename}" was declined'
        body = (f'The owner of "{filename}" declined your request for more time.\n\n'
                f"You can ask again from {APP_URL} if you still need access.\n\n— Web3FS")

    _send_email(to_email, subject, body)
    logger.info("[notify-%s] sent for %s", expect, cid)
    return {"sent": True}


@router.post("/notify-approve")
def notify_approve(request: NotifyDecisionRequest, x_auth_token: Optional[str] = Header(None)):
    """Tell a requester the owner granted them more time."""
    caller = verify_auth_token(x_auth_token or "")
    if not caller:
        return JSONResponse(status_code=401, content={"error": "Missing or invalid auth token"})
    try:
        return _decision_email(request.cid, request.requester, caller, "approved")
    except EmailNotConfigured:
        return JSONResponse(status_code=500, content={"error": "SES SMTP not configured on server"})
    except Exception as e:
        logger.error("[notify-approve] failed: %s", e)
        return JSONResponse(status_code=502, content={"error": str(e)})


@router.post("/notify-deny")
def notify_deny(request: NotifyDecisionRequest, x_auth_token: Optional[str] = Header(None)):
    """Tell a requester the owner declined."""
    caller = verify_auth_token(x_auth_token or "")
    if not caller:
        return JSONResponse(status_code=401, content={"error": "Missing or invalid auth token"})
    try:
        return _decision_email(request.cid, request.requester, caller, "denied")
    except EmailNotConfigured:
        return JSONResponse(status_code=500, content={"error": "SES SMTP not configured on server"})
    except Exception as e:
        logger.error("[notify-deny] failed: %s", e)
        return JSONResponse(status_code=502, content={"error": str(e)})
