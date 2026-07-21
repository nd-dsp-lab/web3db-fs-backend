"""/auth/token: signed-login verification and token issuance."""
from security import verify_auth_token


def test_valid_login_issues_token(client, sign_login):
    address, ts, sig = sign_login()
    r = client.post("/auth/token", json={"address": address, "timestamp": ts, "signature": sig})
    assert r.status_code == 200
    body = r.json()
    assert body["token"].count(".") == 2  # address.expiry.hmac
    assert verify_auth_token(body["token"]) == address.lower()
    assert body["expires"] > ts


def test_expired_login_message_rejected(client, sign_login):
    address, ts, sig = sign_login(timestamp=1)  # far in the past
    r = client.post("/auth/token", json={"address": address, "timestamp": ts, "signature": sig})
    assert r.status_code == 400


def test_tampered_signature_rejected(client, sign_login):
    address, ts, sig = sign_login()
    # mutate a nibble inside the r component so a different address is recovered
    i = 10
    bad = sig[:i] + ("f" if sig[i] != "f" else "0") + sig[i + 1:]
    r = client.post("/auth/token", json={"address": address, "timestamp": ts, "signature": bad})
    assert r.status_code == 401


def test_signature_from_other_address_rejected(client, sign_login):
    _, ts, sig = sign_login()
    other = "0x1A28b19f6d2ea1A05F9eFFbcCcbF7E9571877981"
    r = client.post("/auth/token", json={"address": other, "timestamp": ts, "signature": sig})
    assert r.status_code == 401
