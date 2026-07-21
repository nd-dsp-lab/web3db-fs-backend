"""Sharing endpoints: single/batch grant + revoke and shared-user queries."""
import routers.sharing as sharing

OWNER = "0x1A28b19f6d2ea1A05F9eFFbcCcbF7E9571877981"
RECIPIENT = "0x3081Acc05169336e7875ad9f896bF6511397809a"


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
