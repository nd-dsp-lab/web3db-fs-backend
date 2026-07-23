"""Sharing endpoints: single/batch grant + revoke, shared-user queries,
recipient resolution (share by email via Privy) and notification email.

Recipient resolution decides *which address* a file gets granted to, so a
wrong answer here hands someone else's file to the wrong person — a failure
that looks like nothing at all from the sharer's screen.
"""
from types import SimpleNamespace

import pytest

import routers.sharing as sharing

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


def test_shared_users_owner_sees_recipients(client, patch_contract):
    patch_contract(owners={"cidA": OWNER}, shared={"cidA": [RECIPIENT]})
    r = client.get("/shared-users", params={"cid": "cidA", "user_address": OWNER})
    assert r.status_code == 200
    assert r.json() == {"shared_with": [RECIPIENT]}


def test_shared_users_non_owner_sees_sharer(client, patch_contract):
    patch_contract(owners={"cidA": OWNER})
    r = client.get("/shared-users", params={"cid": "cidA", "user_address": RECIPIENT})
    assert r.json()["shared_by"].lower() == OWNER.lower()


def test_shared_users_batch_unions_only_owned(client, patch_contract):
    # cidA & cidB owned by requester; cidC owned by someone else -> excluded
    patch_contract(
        owners={"cidA": OWNER, "cidB": OWNER, "cidC": RECIPIENT},
        shared={"cidA": [RECIPIENT], "cidB": [RECIPIENT], "cidC": ["0xdead"]},
    )
    r = client.post("/shared-users-batch", json={"cids": ["cidA", "cidB", "cidC"], "user_address": OWNER})
    assert r.status_code == 200
    assert r.json() == {"shared_with": [RECIPIENT]}


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
