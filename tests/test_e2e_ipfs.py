"""End-to-end round trips against a real kubo node.

Everything else in the suite fakes IPFS. That is what let the folder-upload
bug ship: `ipfs add` under a slashed filename wraps the file in a directory
and returns the directory's CID last, so the CID registered on-chain resolved
to the gateway's HTML listing page instead of the file. No fake would have
caught it unless someone already knew to build the fake that way.

These tests push real bytes through /upload and /upload-folder into a real
node and pull them back through /download, comparing byte for byte. Only the
contract and the transaction preparation are stubbed — nothing else.

Skipped automatically when no node is reachable, so `pytest` stays green on a
machine without one. CI starts a throwaway node from ipfs/docker-compose.yml.
Point IPFS_API_URL / IPFS_GATEWAY_URL at a scratch node to run them locally;
never at the production node, since uploads pin content permanently.
"""
import io
import os
import zipfile

import pytest
import requests

import routers.upload as upload
from configure import IPFS_API_URL

OWNER = "0x1A28b19f6d2ea1A05F9eFFbcCcbF7E9571877981"


def _node_is_up():
    try:
        return requests.post(f"{IPFS_API_URL}/version", timeout=3).status_code == 200
    except Exception:
        return False


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not _node_is_up(), reason=f"no IPFS node at {IPFS_API_URL}"),
]


@pytest.fixture
def stub_chain(monkeypatch):
    """Stub only the on-chain half: transaction prep needs an RPC we don't have
    here, and the contract is covered by test_helpers."""
    monkeypatch.setattr(upload, "prepare_upload_transaction", lambda *a, **k: {"tx": "stub"})
    monkeypatch.setattr(upload, "prepare_upload_batch_transaction", lambda *a, **k: {"tx": "stub"})
    monkeypatch.setattr(upload, "folder_share_set", lambda *a: [])


def _unique(prefix=b"%PDF-1.5 "):
    """Fresh content per run: a repeat CID would hit the duplicate check."""
    return prefix + os.urandom(64)


def test_single_upload_round_trips_byte_for_byte(client, patch_contract, auth_token, stub_chain):
    contract = patch_contract()
    content = _unique()

    up = client.post("/upload", files={"file": ("a.pdf", content)},
                     data={"user_address": OWNER, "folder_path": "/docs"})
    assert up.status_code == 200, up.text
    cid = up.json()["cid"]

    contract._owners[cid] = OWNER  # the upload tx is what would set this on-chain
    down = client.get(f"/download/{cid}/a.pdf", headers={"x-auth-token": auth_token(OWNER)})

    assert down.status_code == 200
    assert down.content == content


def test_folder_upload_round_trips_with_webkit_relative_path(
        client, patch_contract, auth_token, stub_chain):
    """The exact production failure: Chrome sends the relative path as the
    multipart filename. Downloading must return the PDF, not a listing page."""
    contract = patch_contract()
    content = _unique()

    up = client.post(
        "/upload-folder",
        files=[("files", ("CoReMe2026/paper.pdf", content))],
        data={"user_address": OWNER, "paths": ["/CoReMe2026/paper.pdf"]})
    assert up.status_code == 200, up.text
    cid = up.json()["uploaded_files"][0]["cid"]

    contract._owners[cid] = OWNER
    down = client.get(f"/download/{cid}/paper.pdf", headers={"x-auth-token": auth_token(OWNER)})

    assert down.status_code == 200
    assert not down.content.startswith(b"<!DOCTYPE html>"), \
        "gateway returned a directory listing: a wrapper-directory CID was stored"
    assert down.content == content


def test_folder_upload_stores_a_file_cid_not_a_directory(client, patch_contract, stub_chain):
    """Same defect seen from the node's side rather than the gateway's:
    `ipfs ls` on a file CID lists nothing, on a directory it lists children."""
    patch_contract()
    up = client.post(
        "/upload-folder",
        files=[("files", ("Docs/report.pdf", _unique()))],
        data={"user_address": OWNER, "paths": ["/Docs/report.pdf"]})
    cid = up.json()["uploaded_files"][0]["cid"]

    ls = requests.post(f"{IPFS_API_URL}/ls", params={"arg": cid}, timeout=10).json()
    links = ls["Objects"][0]["Links"]
    assert links == [], f"stored CID is a directory containing {[l['Name'] for l in links]}"


def test_multi_file_folder_upload_and_zip_download(client, patch_contract, auth_token, stub_chain):
    contract = patch_contract()
    a, b = _unique(b"AAA"), _unique(b"BBB")

    up = client.post(
        "/upload-folder",
        files=[("files", ("Docs/a.txt", a)), ("files", ("Docs/sub/b.txt", b))],
        data={"user_address": OWNER, "paths": ["/Docs/a.txt", "/Docs/sub/b.txt"]})
    assert up.status_code == 200, up.text
    uploaded = up.json()["uploaded_files"]
    assert len(uploaded) == 2

    for u in uploaded:
        contract._owners[u["cid"]] = OWNER
    contract._user_files = [(u["cid"], u["folder_path"], u["file_format"], 1) for u in uploaded]

    down = client.get("/download-folder", params={"path": "/Docs"},
                      headers={"x-auth-token": auth_token(OWNER)})

    assert down.status_code == 200
    with zipfile.ZipFile(io.BytesIO(down.content)) as zf:
        assert sorted(zf.namelist()) == ["Docs/a.txt", "Docs/sub/b.txt"]
        assert zf.read("Docs/a.txt") == a
        assert zf.read("Docs/sub/b.txt") == b


def test_uploaded_content_is_pinned(client, patch_contract, stub_chain):
    """Pinning is what stops the node's GC from deleting a user's file."""
    patch_contract()
    up = client.post("/upload", files={"file": ("pinned.bin", _unique(b"PIN"))},
                     data={"user_address": OWNER})
    cid = up.json()["cid"]

    pins = requests.post(f"{IPFS_API_URL}/pin/ls", params={"arg": cid}, timeout=10)
    assert pins.status_code == 200, f"content not pinned: {pins.text}"


def test_duplicate_upload_is_rejected_without_unpinning_the_original(
        client, patch_contract, stub_chain):
    """A rejected duplicate must leave the first owner's pin untouched —
    unpinning here used to GC data out from under them."""
    contract = patch_contract()
    content = _unique()

    first = client.post("/upload", files={"file": ("dup.bin", content)},
                        data={"user_address": OWNER})
    cid = first.json()["cid"]
    contract._owners[cid] = OWNER

    second = client.post("/upload", files={"file": ("dup.bin", content)},
                         data={"user_address": OWNER})
    assert second.status_code == 409

    pins = requests.post(f"{IPFS_API_URL}/pin/ls", params={"arg": cid}, timeout=10)
    assert pins.status_code == 200, "duplicate rejection unpinned the original"
