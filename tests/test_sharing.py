"""Sharing endpoints: single/batch grant + revoke, shared-user queries,
recipient resolution (share by email via Privy) and notification email.

Recipient resolution decides *which address* a file gets granted to, so a
wrong answer here hands someone else's file to the wrong person — a failure
that looks like nothing at all from the sharer's screen.
"""
from types import SimpleNamespace

import pytest

import routers.sharing as sharing
from permissions import READ, DOWNLOAD

OWNER = "0x1A28b19f6d2ea1A05F9eFFbcCcbF7E9571877981"
RECIPIENT = "0x3081Acc05169336e7875ad9f896bF6511397809a"
WALLET = "0x6e02F541dd762E5077e6d619AA2F2d81371AcABE"
OTHER_WALLET = "0x1111111111111111111111111111111111111111"


def test_share_success(client, monkeypatch):
    monkeypatch.setattr(sharing, "prepare_share_transaction", lambda *a: {"to": "signer"})
    r = client.post("/share", json={"cid": "cidA", "to_address": RECIPIENT, "user_address": OWNER})
    assert r.status_code == 200
    assert r.json() == {"transaction": {"to": "signer"}}


def test_share_failure_is_500(client, monkeypatch):
    def boom(*a):
        raise Exception("not owner")
    monkeypatch.setattr(sharing, "prepare_share_transaction", boom)
    r = client.post("/share", json={"cid": "cidA", "to_address": RECIPIENT, "user_address": OWNER})
    assert r.status_code == 500
    assert "not owner" in r.json()["error"]


def test_share_batch_no_owned_is_400(client, monkeypatch):
    monkeypatch.setattr(sharing, "prepare_share_batch_transaction", lambda *a: (None, 0))
    r = client.post("/share-batch", json={"cids": ["x"], "to_address": RECIPIENT, "user_address": OWNER})
    assert r.status_code == 400


def test_share_batch_success_returns_count(client, monkeypatch):
    monkeypatch.setattr(sharing, "prepare_share_batch_transaction", lambda *a: ({"tx": 1}, 3))
    r = client.post("/share-batch", json={"cids": ["a", "b", "c"], "to_address": RECIPIENT, "user_address": OWNER})
    assert r.status_code == 200
    assert r.json() == {"transaction": {"tx": 1}, "count": 3}


def test_unshare_batch_no_owned_is_400(client, monkeypatch):
    monkeypatch.setattr(sharing, "prepare_unshare_batch_transaction", lambda *a: (None, 0))
    r = client.post("/unshare-batch", json={"cids": ["x"], "to_address": RECIPIENT, "user_address": OWNER})
    assert r.status_code == 400


def test_unshare_failure_is_500(client, monkeypatch):
    def boom(*a):
        raise Exception("revoke failed")
    monkeypatch.setattr(sharing, "prepare_unshare_transaction", boom)
    r = client.post("/unshare", json={"cid": "cidA", "to_address": RECIPIENT, "user_address": OWNER})
    assert r.status_code == 500


def test_shared_users_owner_sees_recipients(client, patch_contract, auth_token):
    # No expires entry -> permanent grant -> expires_at_block is None.
    patch_contract(owners={"cidA": OWNER}, shared={"cidA": [RECIPIENT]},
                    permissions={("cidA", RECIPIENT): READ | DOWNLOAD})
    r = client.get("/shared-users", params={"cid": "cidA"},
                   headers={"x-auth-token": auth_token(OWNER)})
    assert r.status_code == 200
    assert r.json() == {"shared_with": [{"address": RECIPIENT, "expires_at_block": None, "lapsed": False}]}


def test_shared_users_reports_the_expiry_block_for_a_time_limited_grant(client, patch_contract, auth_token):
    patch_contract(owners={"cidA": OWNER}, shared={"cidA": [RECIPIENT]},
                    permissions={("cidA", RECIPIENT): READ},
                    expires={("cidA", RECIPIENT): 500})
    r = client.get("/shared-users", params={"cid": "cidA"},
                   headers={"x-auth-token": auth_token(OWNER)})
    assert r.status_code == 200
    assert r.json() == {"shared_with": [{"address": RECIPIENT, "expires_at_block": 500, "lapsed": False}]}


# getSharedUsers lists every address ever granted access, expired or not --
# it's only cleaned up on an explicit revoke, never by expiry (the "filter,
# not cleanup" design: nothing ever runs at the expiry block itself).
#
# These used to be dropped, which made someone whose access ran out vanish
# from "People with access" with no trace they were ever there. They are now
# listed and flagged, so the owner can see who lapsed and when. Someone
# *revoked* still disappears, correctly: _revoke removes them from
# getSharedUsers outright, so this code never has to tell the cases apart.
def test_shared_users_lists_a_lapsed_recipient_with_the_block_it_ended(client, patch_contract, auth_token):
    patch_contract(owners={"cidA": OWNER}, shared={"cidA": [RECIPIENT]},
                   expires={("cidA", RECIPIENT): 500})  # no permissions entry -> 0 -> lapsed
    r = client.get("/shared-users", params={"cid": "cidA"},
                   headers={"x-auth-token": auth_token(OWNER)})
    assert r.status_code == 200
    assert r.json() == {"shared_with": [
        {"address": RECIPIENT, "expires_at_block": 500, "lapsed": True}]}


def test_shared_users_returns_objects_not_bare_addresses(client, patch_contract, auth_token):
    # Regression guard for the response shape change: callers now get
    # {address, expires_at_block} objects, not bare address strings.
    patch_contract(owners={"cidA": OWNER}, shared={"cidA": [RECIPIENT]},
                    permissions={("cidA", RECIPIENT): READ})
    r = client.get("/shared-users", params={"cid": "cidA"},
                   headers={"x-auth-token": auth_token(OWNER)})
    entry = r.json()["shared_with"][0]
    assert set(entry.keys()) == {"address", "expires_at_block", "lapsed"}


def test_shared_users_non_owner_sees_sharer(client, patch_contract, auth_token):
    patch_contract(owners={"cidA": OWNER})
    r = client.get("/shared-users", params={"cid": "cidA"},
                   headers={"x-auth-token": auth_token(RECIPIENT)})
    assert r.json()["shared_by"].lower() == OWNER.lower()


def test_shared_users_batch_unions_only_owned(client, patch_contract, auth_token):
    # cidA & cidB owned by requester; cidC owned by someone else -> excluded
    patch_contract(
        owners={"cidA": OWNER, "cidB": OWNER, "cidC": RECIPIENT},
        shared={"cidA": [RECIPIENT], "cidB": [RECIPIENT], "cidC": ["0xdead"]},
        permissions={("cidA", RECIPIENT): READ, ("cidB", RECIPIENT): READ},
    )
    r = client.post("/shared-users-batch",
                    json={"cids": ["cidA", "cidB", "cidC"], "user_address": OWNER},
                    headers={"x-auth-token": auth_token(OWNER)})
    assert r.status_code == 200
    assert r.json() == {"shared_with": [{"address": RECIPIENT, "expires_at_block": None, "lapsed": False}]}


def test_shared_users_batch_shows_the_soonest_expiry_across_the_folder(client, patch_contract, auth_token):
    # RECIPIENT has a permanent grant on cidA but a time-limited one on cidB
    # -- the folder should report the soonest deadline, not hide it behind
    # the permanent grant on the other file.
    patch_contract(
        owners={"cidA": OWNER, "cidB": OWNER},
        shared={"cidA": [RECIPIENT], "cidB": [RECIPIENT]},
        permissions={("cidA", RECIPIENT): READ, ("cidB", RECIPIENT): READ},
        expires={("cidB", RECIPIENT): 300},  # cidA has no entry -> permanent
    )
    r = client.post("/shared-users-batch",
                    json={"cids": ["cidA", "cidB"], "user_address": OWNER},
                    headers={"x-auth-token": auth_token(OWNER)})
    assert r.status_code == 200
    assert r.json() == {"shared_with": [{"address": RECIPIENT, "expires_at_block": 300, "lapsed": False}]}


def test_shared_users_batch_counts_partial_folder_access_as_still_active(client, patch_contract, auth_token):
    # A folder is a union, so RECIPIENT can be live on cidA and lapsed on
    # cidB. They can still open part of the folder, so calling them "expired"
    # would be its own kind of wrong -- lapsed means lapsed on everything.
    patch_contract(
        owners={"cidA": OWNER, "cidB": OWNER},
        shared={"cidA": [RECIPIENT], "cidB": [RECIPIENT]},
        permissions={("cidA", RECIPIENT): READ},  # cidB has no entry -> 0 -> lapsed there
    )
    r = client.post("/shared-users-batch",
                    json={"cids": ["cidA", "cidB"], "user_address": OWNER},
                    headers={"x-auth-token": auth_token(OWNER)})
    assert r.status_code == 200
    assert r.json() == {"shared_with": [
        {"address": RECIPIENT, "expires_at_block": None, "lapsed": False}]}


def test_shared_users_batch_marks_lapsed_only_when_every_file_has_run_out(client, patch_contract, auth_token):
    patch_contract(
        owners={"cidA": OWNER, "cidB": OWNER},
        shared={"cidA": [RECIPIENT], "cidB": [RECIPIENT]},
        expires={("cidA", RECIPIENT): 300, ("cidB", RECIPIENT): 500},
        # no permissions entries at all -> 0 everywhere -> lapsed throughout
    )
    r = client.post("/shared-users-batch",
                    json={"cids": ["cidA", "cidB"], "user_address": OWNER},
                    headers={"x-auth-token": auth_token(OWNER)})
    assert r.status_code == 200
    # soonest deadline still wins, as it does for a live grant
    assert r.json() == {"shared_with": [
        {"address": RECIPIENT, "expires_at_block": 300, "lapsed": True}]}


# --- the recipient list is private to the owner ---------------------------
# cids are public (they are on-chain), so identity here has to come from the
# token. A caller-supplied user_address would let anyone read any owner's
# sharing graph just by naming their address.

def test_shared_users_without_token_is_401(client, patch_contract):
    patch_contract(owners={"cidA": OWNER}, shared={"cidA": [RECIPIENT]})
    r = client.get("/shared-users", params={"cid": "cidA", "user_address": OWNER})
    assert r.status_code == 401
    assert "shared_with" not in r.json()


def test_shared_users_ignores_a_spoofed_user_address(client, patch_contract, auth_token):
    # RECIPIENT's token, but claiming to be OWNER in the query string
    patch_contract(owners={"cidA": OWNER}, shared={"cidA": [RECIPIENT]})
    r = client.get("/shared-users", params={"cid": "cidA", "user_address": OWNER},
                   headers={"x-auth-token": auth_token(RECIPIENT)})
    assert r.status_code == 200
    assert "shared_with" not in r.json()          # no recipient list leaked
    assert r.json()["shared_by"].lower() == OWNER.lower()


def test_shared_users_batch_without_token_is_401(client, patch_contract):
    patch_contract(owners={"cidA": OWNER}, shared={"cidA": [RECIPIENT]})
    r = client.post("/shared-users-batch", json={"cids": ["cidA"], "user_address": OWNER})
    assert r.status_code == 401


def test_shared_users_batch_needs_no_user_address(client, patch_contract, auth_token):
    # The endpoint reads the caller from the token, so a body of cids alone is
    # complete. It borrowed DeleteBatchRequest once, which made this a 422.
    patch_contract(owners={"cidA": OWNER}, shared={"cidA": [RECIPIENT]},
                    permissions={("cidA", RECIPIENT): READ})
    r = client.post("/shared-users-batch",
                    json={"cids": ["cidA"]},
                    headers={"x-auth-token": auth_token(OWNER)})
    assert r.status_code == 200
    assert r.json() == {"shared_with": [
        {"address": RECIPIENT, "expires_at_block": None, "lapsed": False}]}


# Older clients still send user_address; unknown fields must stay tolerated,
# and a claimed address must never decide whose sharing graph comes back.
def test_shared_users_batch_ignores_a_spoofed_user_address(client, patch_contract, auth_token):
    patch_contract(owners={"cidA": OWNER}, shared={"cidA": [RECIPIENT]})
    r = client.post("/shared-users-batch",
                    json={"cids": ["cidA"], "user_address": OWNER},
                    headers={"x-auth-token": auth_token(RECIPIENT)})
    assert r.status_code == 200
    assert r.json() == {"shared_with": []}        # owns none of the cids


def test_shared_users_rejects_an_expired_token(client, patch_contract, auth_token):
    patch_contract(owners={"cidA": OWNER}, shared={"cidA": [RECIPIENT]})
    r = client.get("/shared-users", params={"cid": "cidA"},
                   headers={"x-auth-token": auth_token(OWNER, ttl=-1)})
    assert r.status_code == 401


# --- recipient resolution: which address actually receives the share ---

def _privy_user(accounts):
    return {"linked_accounts": accounts}


def eth(address, client_type=None):
    a = {"type": "wallet", "chain_type": "ethereum", "address": address}
    if client_type:
        a["wallet_client_type"] = client_type
    return a


@pytest.fixture
def privy(monkeypatch):
    """Scripted Privy API. Install with routes={(method, url_suffix): (status, json)}."""
    monkeypatch.setenv("PRIVY_APP_ID", "app123")
    monkeypatch.setenv("PRIVY_APP_SECRET", "secret123")

    def _install(routes):
        seen = []

        def _reply(method, url):
            seen.append((method, url))
            for (m, suffix), (status, body) in routes.items():
                if m == method and url.endswith(suffix):
                    return SimpleNamespace(status_code=status, json=lambda b=body: b, text=str(body))
            return SimpleNamespace(status_code=404, json=lambda: {}, text="")

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *exc): return False
            async def post(self, url, **kw): return _reply("POST", url)
            async def get(self, url, **kw): return _reply("GET", url)

        monkeypatch.setattr(sharing.httpx, "AsyncClient", lambda **kw: FakeClient())
        return seen

    return _install


def test_extract_address_prefers_the_privy_embedded_wallet():
    # An external wallet linked to the account is not where a pregenerated
    # share should land — the embedded one is the account's own.
    user = _privy_user([eth(OTHER_WALLET), eth(WALLET, "privy")])
    assert sharing._extract_eth_address(user) == WALLET


def test_extract_address_ignores_non_ethereum_wallets():
    user = _privy_user([
        {"type": "wallet", "chain_type": "solana", "address": "SoLAnA"},
        eth(WALLET),
    ])
    assert sharing._extract_eth_address(user) == WALLET


def test_extract_address_none_when_no_wallet():
    assert sharing._extract_eth_address(_privy_user([{"type": "email", "address": "a@b.c"}])) is None


def test_raw_address_is_checksummed_and_passed_through(client):
    r = client.post("/resolve-recipient", json={"recipient": RECIPIENT.lower()})
    assert r.status_code == 200
    assert r.json() == {"address": RECIPIENT, "existed": True, "pregenerated": False}


def test_malformed_address_is_rejected(client):
    r = client.post("/resolve-recipient", json={"recipient": "0xNOTANADDRESS"})
    assert r.status_code == 400


def test_recipient_that_is_neither_email_nor_address_is_rejected(client):
    r = client.post("/resolve-recipient", json={"recipient": "just-a-name"})
    assert r.status_code == 400


def test_resolution_fails_loudly_when_privy_is_not_configured(client, monkeypatch):
    monkeypatch.delenv("PRIVY_APP_ID", raising=False)
    monkeypatch.delenv("PRIVY_APP_SECRET", raising=False)
    r = client.post("/resolve-recipient", json={"recipient": "someone@example.com"})
    assert r.status_code == 500


def test_existing_email_user_resolves_to_their_wallet(client, privy):
    privy({("POST", "/users/email/address"): (200, _privy_user([eth(WALLET, "privy")]))})
    r = client.post("/resolve-recipient", json={"recipient": "Someone@Example.com"})
    assert r.status_code == 200
    assert r.json() == {"address": WALLET, "existed": True, "pregenerated": False}


def test_existing_user_without_a_wallet_is_404_not_a_new_wallet(client, privy):
    # Pregenerating here would create a second account for the same person.
    seen = privy({("POST", "/users/email/address"): (200, _privy_user([]))})
    r = client.post("/resolve-recipient", json={"recipient": "someone@example.com"})
    assert r.status_code == 404
    assert not any(m == "POST" and u.endswith("/users") for m, u in seen)


def test_google_signin_user_is_found_before_pregenerating(client, privy):
    # The email lookup only matches `email` accounts; a Google user has a
    # google_oauth account instead. Missing this would silently share to a
    # brand-new empty wallet the real user never sees.
    seen = privy({
        ("POST", "/users/email/address"): (404, {}),
        ("GET", "/users"): (200, {"data": [
            {"linked_accounts": [
                {"type": "google_oauth", "email": "Someone@Example.com"},
                eth(WALLET, "privy"),
            ]},
        ]}),
    })
    r = client.post("/resolve-recipient", json={"recipient": "someone@example.com"})
    assert r.status_code == 200
    assert r.json()["address"] == WALLET
    assert r.json()["pregenerated"] is False
    assert not any(m == "POST" and u.endswith("/users") for m, u in seen)


def test_google_match_is_case_insensitive(client, privy):
    privy({
        ("POST", "/users/email/address"): (404, {}),
        ("GET", "/users"): (200, {"data": [
            {"linked_accounts": [
                {"type": "google_oauth", "email": "SOMEONE@EXAMPLE.COM"},
                eth(WALLET, "privy"),
            ]},
        ]}),
    })
    r = client.post("/resolve-recipient", json={"recipient": "someone@example.com"})
    assert r.json()["address"] == WALLET


def test_a_different_google_user_is_not_matched(client, privy):
    # Guards against the scan matching the wrong account and granting to a
    # stranger's wallet.
    privy({
        ("POST", "/users/email/address"): (404, {}),
        ("GET", "/users"): (200, {"data": [
            {"linked_accounts": [
                {"type": "google_oauth", "email": "someone-else@example.com"},
                eth(OTHER_WALLET, "privy"),
            ]},
        ]}),
        ("POST", "/users"): (200, _privy_user([eth(WALLET, "privy")])),
    })
    r = client.post("/resolve-recipient", json={"recipient": "someone@example.com"})
    assert r.json()["address"] == WALLET
    assert r.json()["pregenerated"] is True


def test_unknown_email_pregenerates_a_wallet(client, privy):
    privy({
        ("POST", "/users/email/address"): (404, {}),
        ("GET", "/users"): (200, {"data": []}),
        ("POST", "/users"): (201, _privy_user([eth(WALLET, "privy")])),
    })
    r = client.post("/resolve-recipient", json={"recipient": "new@example.com"})
    assert r.status_code == 200
    assert r.json() == {"address": WALLET, "existed": False, "pregenerated": True}


def test_wallet_creation_failure_is_502_not_a_bogus_address(client, privy):
    privy({
        ("POST", "/users/email/address"): (404, {}),
        ("GET", "/users"): (200, {"data": []}),
        ("POST", "/users"): (500, {"error": "privy down"}),
    })
    r = client.post("/resolve-recipient", json={"recipient": "new@example.com"})
    assert r.status_code == 502


def test_wallet_creation_without_an_address_is_502(client, privy):
    privy({
        ("POST", "/users/email/address"): (404, {}),
        ("GET", "/users"): (200, {"data": []}),
        ("POST", "/users"): (201, _privy_user([])),
    })
    r = client.post("/resolve-recipient", json={"recipient": "new@example.com"})
    assert r.status_code == 502


# --- share notification email ---

@pytest.fixture
def smtp(monkeypatch):
    """Capture what would have been sent, or raise to simulate a send failure."""
    import smtplib
    sent = []

    def _install(fail=False):
        class FakeSMTP:
            def __init__(self, *a, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def starttls(self): pass
            def login(self, u, p): sent.append(("login", u))
            def sendmail(self, frm, to, body):
                if fail:
                    raise smtplib.SMTPException("rejected by SES")
                sent.append(("mail", to, body))

        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
        monkeypatch.setenv("SES_SMTP_USER", "user")
        monkeypatch.setenv("SES_SMTP_PASSWORD", "pw")
        return sent

    return _install


def test_notify_share_sends_to_the_recipient(client, smtp):
    sent = smtp()
    r = client.post("/notify-share", json={
        "recipient_email": "  Someone@Example.com  ", "sharer": "showkot", "filename": "a.pdf"})
    assert r.status_code == 200
    assert r.json() == {"sent": True}
    mail = [s for s in sent if s[0] == "mail"][0]
    assert mail[1] == ["someone@example.com"], "address must be trimmed and lowercased"
    assert "a.pdf" in mail[2]


def test_notify_share_rejects_a_non_email(client, smtp):
    sent = smtp()
    r = client.post("/notify-share", json={
        "recipient_email": "not-an-email", "sharer": "showkot", "filename": "a.pdf"})
    assert r.status_code == 400
    assert sent == []


def test_notify_share_without_smtp_configured_is_500(client, monkeypatch):
    monkeypatch.delenv("SES_SMTP_USER", raising=False)
    monkeypatch.delenv("SES_SMTP_PASSWORD", raising=False)
    r = client.post("/notify-share", json={
        "recipient_email": "a@b.com", "sharer": "showkot", "filename": "a.pdf"})
    assert r.status_code == 500


def test_notify_share_send_failure_is_502(client, smtp):
    # The share itself already succeeded; only the notification failed.
    smtp(fail=True)
    r = client.post("/notify-share", json={
        "recipient_email": "a@b.com", "sharer": "showkot", "filename": "a.pdf"})
    assert r.status_code == 502


# --- access extension requests ---
# The recipient of an expired share asks for more time; the owner resolves it.
# These endpoints only prepare transactions, so the tests here are about
# routing, ownership refusal, and who the caller is allowed to be.

def test_request_extension_prepares_a_transaction(client, monkeypatch):
    monkeypatch.setattr(sharing, "prepare_request_access_transaction", lambda *a: {"to": "signer"})
    r = client.post("/request-extension",
                    json={"cid": "cidA", "duration_blocks": 50, "user_address": RECIPIENT})
    assert r.status_code == 200
    assert r.json() == {"transaction": {"to": "signer"}}


def test_approve_request_prepares_a_transaction(client, monkeypatch):
    seen = {}
    def fake(cid, requester, user_address, duration_blocks):
        seen.update(cid=cid, requester=requester, user=user_address, duration=duration_blocks)
        return {"to": "signer"}
    monkeypatch.setattr(sharing, "prepare_approve_request_transaction", fake)

    r = client.post("/approve-request", json={
        "cid": "cidA", "requester": RECIPIENT, "duration_blocks": 100, "user_address": OWNER})
    assert r.status_code == 200
    # the owner's duration is what gets used, not whatever was asked for
    assert seen == {"cid": "cidA", "requester": RECIPIENT, "user": OWNER, "duration": 100}


def test_approve_request_by_a_non_owner_is_403(client, monkeypatch):
    from helpers import NotOwnerError
    def boom(*a):
        raise NotOwnerError("Only file owner can approve a request for file.")
    monkeypatch.setattr(sharing, "prepare_approve_request_transaction", boom)

    r = client.post("/approve-request", json={
        "cid": "cidA", "requester": RECIPIENT, "duration_blocks": 100, "user_address": RECIPIENT})
    assert r.status_code == 403


def test_deny_request_by_a_non_owner_is_403(client, monkeypatch):
    from helpers import NotOwnerError
    def boom(*a):
        raise NotOwnerError("Only file owner can deny a request for file.")
    monkeypatch.setattr(sharing, "prepare_deny_request_transaction", boom)

    r = client.post("/deny-request",
                    json={"cid": "cidA", "requester": RECIPIENT, "user_address": RECIPIENT})
    assert r.status_code == 403


def test_cancel_request_prepares_a_transaction(client, monkeypatch):
    monkeypatch.setattr(sharing, "prepare_cancel_request_transaction", lambda *a: {"to": "signer"})
    r = client.post("/cancel-request", json={"cid": "cidA", "user_address": RECIPIENT})
    assert r.status_code == 200


def test_share_requests_needs_a_token(client, patch_contract):
    patch_contract()
    assert client.get("/share-requests").status_code == 401


def test_share_requests_lists_pending_asks_for_the_token_holder(client, patch_contract, auth_token):
    from web3 import Web3
    owner_cs = Web3.to_checksum_address(OWNER)
    patch_contract(
        pending={owner_cs: [("cidA", RECIPIENT, 50, 900)]},
        metadata={"cidA": "docs/report.pdf"},
    )
    r = client.get("/share-requests", headers={"x-auth-token": auth_token(OWNER)})
    assert r.status_code == 200
    assert r.json()["requests"] == [{
        "cid": "cidA", "filename": "report.pdf", "requester": RECIPIENT,
        "duration_blocks": 50, "requested_at_block": 900,
    }]


def test_share_requests_ignores_a_caller_supplied_address(client, patch_contract, auth_token):
    # getPendingRequests takes an address, so trusting a query param would let
    # anyone read anyone else's pending requests. The owner comes from the token.
    from web3 import Web3
    patch_contract(
        pending={Web3.to_checksum_address(OWNER): [("cidA", RECIPIENT, 50, 900)]},
        metadata={"cidA": "report.pdf"},
    )
    r = client.get("/share-requests",
                   params={"user_address": OWNER},              # attacker's claim
                   headers={"x-auth-token": auth_token(WALLET)})  # actual identity
    assert r.status_code == 200
    assert r.json()["requests"] == []


def test_share_requests_skips_trashed_files(client, patch_contract, auth_token):
    # Trashing is a moveFile, not a permission change, so the contract has no
    # idea the file is in the trash and still reports the request.
    from web3 import Web3
    owner_cs = Web3.to_checksum_address(OWNER)
    patch_contract(
        pending={owner_cs: [("cidA", RECIPIENT, 50, 900), ("cidB", RECIPIENT, 10, 901)]},
        metadata={"cidA": "/.trash/old.pdf", "cidB": "live.pdf"},
    )
    r = client.get("/share-requests", headers={"x-auth-token": auth_token(OWNER)})
    assert [q["cid"] for q in r.json()["requests"]] == ["cidB"]


# --- request-flow notification emails ---
# Every party is read from the chain and the auth token, never from the body,
# and nothing is sent unless the chain already agrees with what the caller
# claims -- otherwise these are a way to mail someone on demand.

@pytest.fixture
def capture_email(monkeypatch):
    sent = []
    monkeypatch.setattr(sharing, "_send_email",
                        lambda to, subject, body: sent.append((to, subject, body)))
    monkeypatch.setattr(sharing, "_privy_email_for_address",
                        lambda addr: f"{addr.lower()}@example.test")
    return sent


def test_notify_request_needs_a_token(client, patch_contract, capture_email):
    patch_contract()
    assert client.post("/notify-request", json={"cid": "cidA"}).status_code == 401
    assert capture_email == []


def test_notify_request_emails_the_owner(client, patch_contract, auth_token, capture_email):
    from web3 import Web3
    patch_contract(owners={"cidA": Web3.to_checksum_address(OWNER)},
                   requests={("cidA", Web3.to_checksum_address(RECIPIENT)): 1},  # Pending
                   metadata={"cidA": "docs/report.pdf"})
    r = client.post("/notify-request", json={"cid": "cidA"},
                    headers={"x-auth-token": auth_token(RECIPIENT)})
    assert r.status_code == 200 and r.json() == {"sent": True}

    to, subject, body = capture_email[0]
    assert to == f"{OWNER.lower()}@example.test"
    assert "report.pdf" in subject and "report.pdf" in body
    assert "Shared with others" in body


def test_notify_request_refuses_without_a_pending_request(client, patch_contract, auth_token, capture_email):
    # Otherwise the endpoint is a button for mailing an owner on demand.
    from web3 import Web3
    patch_contract(owners={"cidA": Web3.to_checksum_address(OWNER)})  # no request -> status 0
    r = client.post("/notify-request", json={"cid": "cidA"},
                    headers={"x-auth-token": auth_token(RECIPIENT)})
    assert r.status_code == 409
    assert capture_email == []


def test_notify_request_is_quiet_when_the_owner_has_no_email(client, patch_contract, auth_token,
                                                             capture_email, monkeypatch):
    from web3 import Web3
    monkeypatch.setattr(sharing, "_privy_email_for_address", lambda addr: None)
    patch_contract(owners={"cidA": Web3.to_checksum_address(OWNER)},
                   requests={("cidA", Web3.to_checksum_address(RECIPIENT)): 1})
    r = client.post("/notify-request", json={"cid": "cidA"},
                    headers={"x-auth-token": auth_token(RECIPIENT)})
    assert r.status_code == 200 and r.json()["sent"] is False
    assert capture_email == []


def test_notify_approve_rejects_a_non_owner(client, patch_contract, auth_token, capture_email):
    from web3 import Web3
    patch_contract(owners={"cidA": Web3.to_checksum_address(OWNER)})
    r = client.post("/notify-approve",
                    json={"cid": "cidA", "requester": RECIPIENT},
                    headers={"x-auth-token": auth_token(RECIPIENT)})
    assert r.status_code == 403
    assert capture_email == []


def test_notify_approve_refuses_when_the_chain_says_denied(client, patch_contract,
                                                                    auth_token, capture_email):
    # Chain says denied, so /notify-approve must refuse to send.
    from web3 import Web3
    patch_contract(owners={"cidA": Web3.to_checksum_address(OWNER)},
                   requests={("cidA", Web3.to_checksum_address(RECIPIENT)): 3})  # Denied
    r = client.post("/notify-approve",
                    json={"cid": "cidA", "requester": RECIPIENT},
                    headers={"x-auth-token": auth_token(OWNER)})
    assert r.status_code == 409
    assert capture_email == []


def test_notify_approve_tells_the_requester(client, patch_contract,
                                                                auth_token, capture_email):
    from web3 import Web3
    recipient_cs = Web3.to_checksum_address(RECIPIENT)
    patch_contract(owners={"cidA": Web3.to_checksum_address(OWNER)},
                   requests={("cidA", recipient_cs): 2},        # Approved
                   expires={("cidA", recipient_cs): 5400},
                   metadata={"cidA": "report.pdf"})
    r = client.post("/notify-approve",
                    json={"cid": "cidA", "requester": RECIPIENT},
                    headers={"x-auth-token": auth_token(OWNER)})
    assert r.status_code == 200

    to, subject, body = capture_email[0]
    assert to == f"{RECIPIENT.lower()}@example.test"
    assert "extended" in subject
    assert "5,400" in body          # the block their access now runs to


def test_notify_deny_tells_the_requester(client, patch_contract,
                                                                auth_token, capture_email):
    from web3 import Web3
    patch_contract(owners={"cidA": Web3.to_checksum_address(OWNER)},
                   requests={("cidA", Web3.to_checksum_address(RECIPIENT)): 3},  # Denied
                   metadata={"cidA": "report.pdf"})
    r = client.post("/notify-deny",
                    json={"cid": "cidA", "requester": RECIPIENT},
                    headers={"x-auth-token": auth_token(OWNER)})
    assert r.status_code == 200

    _to, subject, body = capture_email[0]
    assert "declined" in subject
    assert "ask again" in body.lower()
