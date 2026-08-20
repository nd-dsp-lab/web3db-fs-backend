"""Download endpoints: single file, folder-as-zip, and thumbnail guards.

Authorization itself is covered in test_security; here we assert the routes
wire it up and that the bytes/headers they emit are right.
"""
import io
import zipfile

from permissions import READ, DOWNLOAD

OWNER = "0x1a28b19f6d2ea1a05f9effbcccbf7e9571877981"
OTHER = "0x3081acc05169336e7875ad9f896bf6511397809a"
PDF = b"%PDF-1.5 hello"


# --- /download/{cid}/{filename} ---

def test_download_without_token_is_401(client, patch_contract):
    patch_contract(owners={"cidA": OWNER})
    r = client.get("/download/cidA/a.pdf")
    assert r.status_code == 401


def test_download_without_permission_is_403(client, patch_contract, auth_token):
    from web3 import Web3
    patch_contract(owners={"cidA": OWNER},
                   permissions={("cidA", Web3.to_checksum_address(OTHER)): READ})
    r = client.get("/download/cidA/a.pdf", headers={"x-auth-token": auth_token(OTHER)})
    assert r.status_code == 403


def test_download_returns_bytes_and_attachment_header(
        client, patch_contract, auth_token, fake_gateway):
    patch_contract(owners={"cidA": OWNER})
    fake_gateway({"cidA": PDF})

    r = client.get("/download/cidA/a.pdf", headers={"x-auth-token": auth_token(OWNER)})

    assert r.status_code == 200
    assert r.content == PDF
    assert r.headers["content-disposition"] == "attachment; filename=\"a.pdf\"; filename*=UTF-8''a.pdf"
    # Explicit length, never chunked: the reverse proxy speaks HTTP/1.0, where
    # chunked transfer-encoding is invalid and broke PDF preview through it.
    assert r.headers["content-length"] == str(len(PDF))
    assert "transfer-encoding" not in r.headers


def test_download_non_ascii_filename_is_not_502(
        client, patch_contract, auth_token, fake_gateway):
    # macOS screenshot names carry a narrow no-break space (U+202F) before
    # AM/PM; headers are latin-1, so the raw name used to crash into a 502.
    patch_contract(owners={"cidA": OWNER})
    fake_gateway({"cidA": PDF})

    name = "Screenshot 2026-08-20 at 2.51.04\u202fAM.png"
    r = client.get(f"/download/cidA/{name}", headers={"x-auth-token": auth_token(OWNER)})

    assert r.status_code == 200
    assert r.content == PDF
    cd = r.headers["content-disposition"]
    assert cd.encode("latin-1")  # header must be encodable
    assert "filename*=UTF-8''Screenshot%202026-08-20%20at%202.51.04%E2%80%AFAM.png" in cd


def test_download_shared_user_with_download_bit_allowed(
        client, patch_contract, auth_token, fake_gateway):
    from web3 import Web3
    patch_contract(owners={"cidA": OWNER},
                   permissions={("cidA", Web3.to_checksum_address(OTHER)): READ | DOWNLOAD})
    fake_gateway({"cidA": PDF})

    r = client.get("/download/cidA/a.pdf", headers={"x-auth-token": auth_token(OTHER)})
    assert r.status_code == 200


def test_download_gateway_miss_is_502(client, patch_contract, auth_token, fake_gateway):
    patch_contract(owners={"cidA": OWNER})
    fake_gateway({})  # nothing on the node

    r = client.get("/download/cidA/a.pdf", headers={"x-auth-token": auth_token(OWNER)})
    assert r.status_code == 502
    assert "error" in r.json()


# --- /download-folder ---

def test_download_folder_without_token_is_401(client, patch_contract):
    patch_contract()
    assert client.get("/download-folder", params={"path": "/docs"}).status_code == 401


def test_download_folder_root_path_is_400(client, patch_contract, auth_token):
    patch_contract()
    r = client.get("/download-folder", params={"path": "/"},
                   headers={"x-auth-token": auth_token(OWNER)})
    assert r.status_code == 400


def test_download_folder_empty_is_404(client, patch_contract, auth_token):
    patch_contract(user_files=[("cid1", "other/a.txt", "txt", 1)])
    r = client.get("/download-folder", params={"path": "/docs"},
                   headers={"x-auth-token": auth_token(OWNER)})
    assert r.status_code == 404


def test_download_folder_zips_matching_files_with_relative_paths(
        client, patch_contract, auth_token, fake_gateway):
    patch_contract(
        owners={"cid1": OWNER, "cid2": OWNER},
        user_files=[
            ("cid1", "docs/a.txt", "txt", 1),
            ("cid2", "docs/sub/b.txt", "txt", 2),
            ("cid3", "photos/c.jpg", "jpg", 3),  # outside the folder
        ])
    fake_gateway({"cid1": b"AAA", "cid2": b"BBB"})

    r = client.get("/download-folder", params={"path": "/docs"},
                   headers={"x-auth-token": auth_token(OWNER)})

    assert r.status_code == 200
    assert r.headers["content-disposition"] == "attachment; filename=\"docs.zip\"; filename*=UTF-8''docs.zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert sorted(zf.namelist()) == ["docs/a.txt", "docs/sub/b.txt"]
        assert zf.read("docs/a.txt") == b"AAA"


def test_download_folder_omits_files_without_permission(
        client, patch_contract, auth_token, fake_gateway):
    # cid2 belongs to someone else with no bits granted — it must not be zipped
    patch_contract(
        owners={"cid1": OWNER, "cid2": OTHER},
        user_files=[("cid1", "docs/a.txt", "txt", 1), ("cid2", "docs/secret.txt", "txt", 2)])
    fetched = fake_gateway({"cid1": b"AAA", "cid2": b"SECRET"})

    r = client.get("/download-folder", params={"path": "/docs"},
                   headers={"x-auth-token": auth_token(OWNER)})

    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert zf.namelist() == ["docs/a.txt"]
    assert "cid2" not in fetched, "unauthorized content must never leave the node"


def test_download_folder_skips_cids_missing_from_gateway(
        client, patch_contract, auth_token, fake_gateway):
    patch_contract(owners={"cid1": OWNER, "cid2": OWNER},
                   user_files=[("cid1", "docs/a.txt", "txt", 1),
                               ("cid2", "docs/gone.txt", "txt", 2)])
    fake_gateway({"cid1": b"AAA"})

    r = client.get("/download-folder", params={"path": "/docs"},
                   headers={"x-auth-token": auth_token(OWNER)})

    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert zf.namelist() == ["docs/a.txt"]


# --- /thumbnail/{cid} ---

def test_thumbnail_rejects_path_like_cid(client, patch_contract, auth_token):
    patch_contract()
    r = client.get("/thumbnail/..%2F..%2Fetc%2Fpasswd", headers={"x-auth-token": auth_token(OWNER)})
    assert r.status_code in (400, 404)


def test_thumbnail_without_token_is_401(client, patch_contract):
    patch_contract(owners={"cidA": OWNER})
    assert client.get("/thumbnail/cidA").status_code == 401
