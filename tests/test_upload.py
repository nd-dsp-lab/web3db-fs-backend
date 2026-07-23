"""Upload endpoints: single file, folder batch, and the CID the batch stores."""
import routers.upload as upload

OWNER = "0x1A28b19f6d2ea1A05F9eFFbcCcbF7E9571877981"
OTHER = "0x3081Acc05169336e7875ad9f896bF6511397809a"


def _no_inherited_sharing(monkeypatch):
    monkeypatch.setattr(upload, "folder_share_set", lambda *a: [])


# --- /upload (single file) ---

def test_upload_returns_file_cid_and_path(client, patch_contract, fake_ipfs, monkeypatch):
    patch_contract()
    _no_inherited_sharing(monkeypatch)
    monkeypatch.setattr(upload, "prepare_upload_transaction", lambda *a: {"tx": "up"})

    r = client.post("/upload",
                    files={"file": ("a.pdf", b"%PDF-1.5 data")},
                    data={"user_address": OWNER, "folder_path": "/docs"})

    assert r.status_code == 200
    body = r.json()
    assert body["cid"] == fake_ipfs.cid_for("a.pdf")
    assert body["filename"] == "a.pdf"
    assert body["full_path"] == "docs/a.pdf"
    assert body["fileformat"] == "pdf"
    assert body["transaction"] == {"tx": "up"}


def test_upload_pins_only_after_duplicate_check(client, patch_contract, fake_ipfs, monkeypatch):
    patch_contract()
    _no_inherited_sharing(monkeypatch)
    monkeypatch.setattr(upload, "prepare_upload_transaction", lambda *a: {"tx": "up"})

    client.post("/upload", files={"file": ("a.pdf", b"data")}, data={"user_address": OWNER})
    assert fake_ipfs.pinned == [fake_ipfs.cid_for("a.pdf")]


def test_upload_duplicate_is_409_and_never_pins(client, patch_contract, fake_ipfs, monkeypatch):
    # Regression: unpinning a duplicate used to GC the original owner's data,
    # so a duplicate must not touch the pinset at all.
    patch_contract(owners={fake_ipfs.cid_for("a.pdf"): OTHER})
    _no_inherited_sharing(monkeypatch)

    r = client.post("/upload", files={"file": ("a.pdf", b"data")}, data={"user_address": OWNER})

    assert r.status_code == 409
    assert r.json()["reason"] == "file_already_exists"
    assert fake_ipfs.pinned == []


# --- /upload-folder (batch) ---

def _folder_post(client, entries, user=OWNER):
    """entries: [(multipart_filename, full_path, content)]"""
    files = [("files", (name, content)) for name, _p, content in entries]
    data = {"user_address": user, "paths": [p for _n, p, _c in entries]}
    return client.post("/upload-folder", files=files, data=data)


def test_folder_upload_stores_file_cid_not_wrapper_directory(
        client, patch_contract, fake_ipfs, monkeypatch):
    """Regression: Chrome sends webkitRelativePath as the multipart filename,
    and `ipfs add` under a slashed name returns a wrapper-directory CID last.
    Storing that CID made every preview and download serve the gateway's
    directory listing page instead of the file."""
    patch_contract()
    _no_inherited_sharing(monkeypatch)
    monkeypatch.setattr(upload, "prepare_upload_batch_transaction", lambda *a: {"tx": "batch"})

    r = _folder_post(client, [("Docs/a.pdf", "/Docs/a.pdf", b"%PDF-1.5")])

    assert r.status_code == 200
    stored = r.json()["uploaded_files"][0]
    assert fake_ipfs.added_names == ["a.pdf"], "must add under the leaf name"
    assert stored["cid"] == fake_ipfs.cid_for("a.pdf")
    assert not stored["cid"].startswith("Qmdir")


def test_folder_upload_keeps_paths_and_leaf_filenames(
        client, patch_contract, fake_ipfs, monkeypatch):
    patch_contract()
    _no_inherited_sharing(monkeypatch)
    monkeypatch.setattr(upload, "prepare_upload_batch_transaction", lambda *a: {"tx": "batch"})

    r = _folder_post(client, [
        ("Docs/a.pdf", "/Docs/a.pdf", b"one"),
        ("Docs/sub/b.txt", "/Docs/sub/b.txt", b"two"),
    ])

    uploaded = r.json()["uploaded_files"]
    assert [u["filename"] for u in uploaded] == ["a.pdf", "b.txt"]
    assert [u["folder_path"] for u in uploaded] == ["/Docs/a.pdf", "/Docs/sub/b.txt"]
    assert [u["file_format"] for u in uploaded] == ["pdf", "txt"]


def test_folder_upload_skips_cid_already_owned_on_chain(
        client, patch_contract, fake_ipfs, monkeypatch):
    patch_contract(owners={fake_ipfs.cid_for("a.pdf"): OTHER})
    _no_inherited_sharing(monkeypatch)
    monkeypatch.setattr(upload, "prepare_upload_batch_transaction", lambda *a: {"tx": "batch"})

    r = _folder_post(client, [
        ("Docs/a.pdf", "/Docs/a.pdf", b"one"),
        ("Docs/b.txt", "/Docs/b.txt", b"two"),
    ])

    body = r.json()
    assert [s["filename"] for s in body["skipped_files"]] == ["a.pdf"]
    assert [u["filename"] for u in body["uploaded_files"]] == ["b.txt"]


def test_folder_upload_skips_duplicate_within_batch(
        client, patch_contract, fake_ipfs, monkeypatch):
    patch_contract()
    _no_inherited_sharing(monkeypatch)
    monkeypatch.setattr(upload, "prepare_upload_batch_transaction", lambda *a: {"tx": "batch"})

    # same leaf name -> same fake CID, standing in for identical content
    r = _folder_post(client, [
        ("Docs/a.pdf", "/Docs/a.pdf", b"one"),
        ("Docs/sub/a.pdf", "/Docs/sub/a.pdf", b"one"),
    ])

    body = r.json()
    assert len(body["uploaded_files"]) == 1
    assert len(body["skipped_files"]) == 1


def test_folder_upload_all_skipped_returns_null_transaction(
        client, patch_contract, fake_ipfs, monkeypatch):
    patch_contract(owners={fake_ipfs.cid_for("a.pdf"): OTHER})
    _no_inherited_sharing(monkeypatch)

    r = _folder_post(client, [("Docs/a.pdf", "/Docs/a.pdf", b"one")])

    assert r.status_code == 200
    assert r.json()["transaction"] is None


def test_folder_upload_batch_tx_failure_is_500(client, patch_contract, fake_ipfs, monkeypatch):
    patch_contract()
    _no_inherited_sharing(monkeypatch)
    monkeypatch.setattr(upload, "prepare_upload_batch_transaction",
                        lambda *a: (_ for _ in ()).throw(Exception("gas estimate failed")))

    r = _folder_post(client, [("Docs/a.pdf", "/Docs/a.pdf", b"one")])

    assert r.status_code == 500
    assert "gas estimate failed" in r.json()["error"]


def test_folder_upload_grants_to_shared_folder_recipients(
        client, patch_contract, fake_ipfs, monkeypatch):
    patch_contract()
    monkeypatch.setattr(upload, "prepare_upload_batch_transaction", lambda *a: {"tx": "batch"})
    monkeypatch.setattr(upload, "folder_share_set", lambda addr, path: [OTHER])
    monkeypatch.setattr(upload, "prepare_inherited_grant_transactions",
                        lambda cids, users, addr: [{"grant": u, "cids": list(cids)} for u in users])

    r = _folder_post(client, [
        ("Docs/a.pdf", "/Docs/a.pdf", b"one"),
        ("Docs/b.txt", "/Docs/b.txt", b"two"),
    ])

    body = r.json()
    assert body["auto_shared_with"] == [OTHER]
    assert body["share_transactions"][0]["cids"] == [
        fake_ipfs.cid_for("a.pdf"), fake_ipfs.cid_for("b.txt")]
