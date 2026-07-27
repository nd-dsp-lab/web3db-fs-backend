"""Upload endpoints: single file, folder batch, and the CID the batch stores."""
from types import SimpleNamespace

import helpers
import routers.upload as upload

OWNER = "0x1A28b19f6d2ea1A05F9eFFbcCcbF7E9571877981"
OTHER = "0x3081Acc05169336e7875ad9f896bF6511397809a"


def _no_inherited_sharing(monkeypatch):
    # Inherited sharing is prepared in helpers now; stub the whole step.
    monkeypatch.setattr(upload, "prepare_inherited_shares", lambda *a: ([], []))


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
    assert [u["full_path"] for u in uploaded] == ["/Docs/a.pdf", "/Docs/sub/b.txt"]
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
    # Both seams live in helpers now, which is where prepare_inherited_shares
    # calls them — patching the router would no longer be reached.
    monkeypatch.setattr(helpers, "folder_share_set", lambda addr, path: [OTHER])
    monkeypatch.setattr(helpers, "prepare_inherited_grant_transactions",
                        lambda cids, users, addr: [{"grant": u, "cids": list(cids)} for u in users])

    r = _folder_post(client, [
        ("Docs/a.pdf", "/Docs/a.pdf", b"one"),
        ("Docs/b.txt", "/Docs/b.txt", b"two"),
    ])

    body = r.json()
    assert body["auto_shared_with"] == [OTHER]
    assert body["share_transactions"][0]["cids"] == [
        fake_ipfs.cid_for("a.pdf"), fake_ipfs.cid_for("b.txt")]


# --- resilience: a slow or failing IPFS node ---
# The folder loop talks to the node once per file. Without a timeout a stalled
# node holds the request open indefinitely; without checking the pin result a
# file gets registered on-chain while its bytes stay collectable.

def test_every_ipfs_call_carries_a_timeout(client, patch_contract, fake_ipfs, monkeypatch):
    patch_contract()
    _no_inherited_sharing(monkeypatch)
    monkeypatch.setattr(upload, "prepare_upload_batch_transaction", lambda *a: {"tx": "batch"})

    _folder_post(client, [("Docs/a.pdf", "/Docs/a.pdf", b"one")])

    assert fake_ipfs.calls, "expected calls to the node"
    missing = [url for url, kwargs in fake_ipfs.calls if not kwargs.get("timeout")]
    assert missing == [], f"these IPFS calls can hang forever: {missing}"


def test_folder_upload_skips_a_file_whose_pin_fails(
        client, patch_contract, fake_ipfs, monkeypatch):
    patch_contract()
    _no_inherited_sharing(monkeypatch)
    monkeypatch.setattr(upload, "prepare_upload_batch_transaction", lambda *a: {"tx": "batch"})
    fake_ipfs.failing_pins.add(fake_ipfs.cid_for("a.pdf"))

    r = _folder_post(client, [
        ("Docs/a.pdf", "/Docs/a.pdf", b"one"),
        ("Docs/b.txt", "/Docs/b.txt", b"two"),
    ])

    body = r.json()
    # An unpinned file must never reach the contract — the CID would resolve to
    # nothing once the node garbage-collects.
    assert [u["filename"] for u in body["uploaded_files"]] == ["b.txt"]
    assert [s["filename"] for s in body["skipped_files"]] == ["a.pdf"]


# --- /verify-upload: releasing pinned bytes after a mined delete ---
# The unpin step runs only here, after the chain has confirmed the delete. If
# it silently does nothing the files stay pinned forever and the node's disk
# never comes back; if its result never reaches the response, nobody can tell.

def _stub_receipt(monkeypatch, func_name, func_params, status=1):
    """Make verify_upload see a mined tx decoding to func_name(func_params)."""
    receipt = SimpleNamespace(blockNumber=1, gasUsed=21000, status=status)
    eth = SimpleNamespace(
        wait_for_transaction_receipt=lambda *a, **k: receipt,
        get_transaction=lambda h: SimpleNamespace(input="0x"),
    )
    monkeypatch.setattr(upload, "w3", SimpleNamespace(eth=eth))
    monkeypatch.setattr(upload.contract, "decode_function_input",
                        lambda data: (SimpleNamespace(fn_name=func_name), func_params),
                        raising=False)

    released = []
    monkeypatch.setattr(upload, "unpin_cids",
                        lambda cids: released.append(list(cids)) or {"unpins": [], "gc_ok": True})
    return released


def _verify(client):
    return client.post("/verify-upload", json={"tx_hash": "0xabc"})


def test_folder_delete_releases_every_cid_and_reports_it(client, patch_contract, monkeypatch):
    patch_contract()
    released = _stub_receipt(monkeypatch, "cleanFolder", {"cids": ["c1", "c2", "c3"]})

    body = _verify(client).json()

    assert released == [["c1", "c2", "c3"]]
    # The result used to be computed and then dropped on the floor, so a folder
    # delete answered as though no unpinning had happened at all.
    assert body["unpin_result"]["gc_ok"] is True


def test_single_delete_releases_its_cid(client, patch_contract, monkeypatch):
    patch_contract()
    released = _stub_receipt(monkeypatch, "deleteFile", {"cid": "c1"})

    body = _verify(client).json()

    assert released == [["c1"]]
    assert "unpin_result" in body


def test_a_reverted_delete_releases_nothing(client, patch_contract, monkeypatch):
    # status=0 means the chain rejected the delete: the file still exists on
    # chain, so dropping its pin would strand a live entry.
    # patch_contract must come first — it installs a fresh fake contract, which
    # would otherwise discard the decode_function_input stub and make this pass
    # for the wrong reason (decoding throws, so nothing is released anyway).
    patch_contract()
    released = _stub_receipt(monkeypatch, "cleanFolder", {"cids": ["c1"]}, status=0)

    body = _verify(client).json()

    assert released == []
    assert "unpin_result" not in body


def test_an_upload_releases_nothing(client, patch_contract, monkeypatch):
    patch_contract()
    released = _stub_receipt(monkeypatch, "uploadFile", {"cid": "c1"})

    _verify(client)

    assert released == []


def test_a_delete_naming_no_cids_is_reported_not_silent(client, patch_contract, monkeypatch):
    patch_contract()
    released = _stub_receipt(monkeypatch, "cleanFolder", {"cids": []})

    body = _verify(client).json()

    assert released == []
    assert body["unpin_result"] == {"warning": "Empty CID list"}
