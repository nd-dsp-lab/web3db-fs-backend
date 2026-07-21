"""File-operation endpoints: delete/move (single + batch) and folder delete."""
import routers.file_ops as file_ops

OWNER = "0x1A28b19f6d2ea1A05F9eFFbcCcbF7E9571877981"
OTHER = "0x3081Acc05169336e7875ad9f896bF6511397809a"


def test_delete_success(client, monkeypatch):
    monkeypatch.setattr(file_ops, "prepare_delete_transaction", lambda *a: {"tx": "del"})
    r = client.post("/delete", json={"cid": "cidA", "user_address": OWNER})
    assert r.status_code == 200
    assert r.json() == {"transaction": {"tx": "del"}}


def test_delete_failure_is_500(client, monkeypatch):
    monkeypatch.setattr(file_ops, "prepare_delete_transaction",
                        lambda *a: (_ for _ in ()).throw(Exception("bad cid")))
    r = client.post("/delete", json={"cid": "cidA", "user_address": OWNER})
    assert r.status_code == 500


def test_move_success(client, monkeypatch):
    monkeypatch.setattr(file_ops, "prepare_move_transaction", lambda *a: {"tx": "mv"})
    r = client.post("/move", json={"cid": "cidA", "new_path": "/b/x", "user_address": OWNER})
    assert r.status_code == 200
    assert r.json() == {"transaction": {"tx": "mv"}}


def test_move_failure_is_500(client, monkeypatch):
    # Regression: this path used to fall through and return 200 with a null body.
    monkeypatch.setattr(file_ops, "prepare_move_transaction",
                        lambda *a: (_ for _ in ()).throw(Exception("not owner")))
    r = client.post("/move", json={"cid": "cidA", "new_path": "/b/x", "user_address": OWNER})
    assert r.status_code == 500
    assert "not owner" in r.json()["error"]


def test_move_batch_length_mismatch_is_400(client):
    r = client.post("/move-batch", json={"cids": ["a", "b"], "new_paths": ["/x"], "user_address": OWNER})
    assert r.status_code == 400


def test_move_batch_success(client, monkeypatch):
    monkeypatch.setattr(file_ops, "prepare_move_batch_transaction", lambda *a: ({"tx": 1}, 2))
    r = client.post("/move-batch", json={"cids": ["a", "b"], "new_paths": ["/x", "/y"], "user_address": OWNER})
    assert r.status_code == 200
    assert r.json() == {"transaction": {"tx": 1}, "count": 2}


def test_delete_batch_filters_to_owned(client, patch_contract, monkeypatch):
    patch_contract(owners={"cidA": OWNER, "cidB": OTHER})
    # echo the cids the endpoint decided to delete so we can assert the filter
    monkeypatch.setattr(file_ops, "prepare_delete_folder", lambda cids, addr: {"cids": list(cids)})
    r = client.post("/delete-batch", json={"cids": ["cidA", "cidB"], "user_address": OWNER})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["transaction"]["cids"] == ["cidA"]


def test_delete_batch_none_owned_returns_null_tx(client, patch_contract):
    patch_contract(owners={"cidA": OTHER})
    r = client.post("/delete-batch", json={"cids": ["cidA"], "user_address": OWNER})
    assert r.status_code == 200
    assert r.json() == {"transaction": None, "count": 0}


def test_delete_folder_scans_by_prefix(client, patch_contract, monkeypatch):
    patch_contract(user_files=[
        ("cid1", "docs/a.txt", "txt", 1),
        ("cid2", "docs/sub/b.txt", "txt", 2),
        ("cid3", "photos/c.jpg", "jpg", 3),
    ])
    monkeypatch.setattr(file_ops, "prepare_delete_folder", lambda cids, addr: {"n": len(cids)})
    r = client.post("/delete-folder", json={"folder_path": "/docs", "user_address": OWNER})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert set(body["target_cids"]) == {"cid1", "cid2"}
    assert body["is_principal"] is True
