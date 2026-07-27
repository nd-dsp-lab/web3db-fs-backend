"""At-rest file encryption: sealed format, determinism, legacy passthrough."""
import pytest

import filecrypto
import routers.upload as upload


# --- the primitive ---

def test_roundtrip():
    data = b"the quick brown fox" * 100
    sealed = filecrypto.encrypt(data)
    assert sealed != data
    assert filecrypto.is_encrypted(sealed)
    assert filecrypto.decrypt(sealed) == data


def test_deterministic_same_content_same_ciphertext():
    # Identical plaintext must produce identical bytes, or the CID-based
    # duplicate detection would silently stop working for encrypted files.
    assert filecrypto.encrypt(b"same bytes") == filecrypto.encrypt(b"same bytes")


def test_different_content_different_ciphertext():
    assert filecrypto.encrypt(b"a") != filecrypto.encrypt(b"b")


def test_ciphertext_leaks_no_plaintext():
    sealed = filecrypto.encrypt(b"SUPER-SECRET-CONTENT" * 10)
    assert b"SUPER-SECRET-CONTENT" not in sealed


def test_tampered_ciphertext_raises():
    sealed = bytearray(filecrypto.encrypt(b"payload"))
    sealed[-1] ^= 0x01  # flip one ciphertext bit
    with pytest.raises(ValueError):
        filecrypto.decrypt(bytes(sealed))


def test_legacy_plaintext_passes_through():
    # Files stored before encryption existed have no magic header and must
    # come back untouched.
    legacy = b"%PDF-1.5 stored before encryption"
    assert not filecrypto.is_encrypted(legacy)
    assert filecrypto.maybe_decrypt(legacy) == legacy


def test_maybe_decrypt_unseals_own_format():
    data = b"round and round"
    assert filecrypto.maybe_decrypt(filecrypto.encrypt(data)) == data


# --- the wiring ---

def test_upload_hands_ipfs_only_ciphertext(client, patch_contract, fake_ipfs, monkeypatch):
    patch_contract()
    monkeypatch.setattr(upload, "prepare_inherited_shares", lambda *a: ([], []))
    monkeypatch.setattr(upload, "prepare_upload_transaction", lambda *a: {"tx": "up"})

    stored = {}
    real_add = upload.ipfs.add_unpinned  # fake_ipfs backs this with a fake node

    def spying_add(filename, data):
        stored[filename] = data
        return real_add(filename, data)

    monkeypatch.setattr(upload.ipfs, "add_unpinned", spying_add)

    r = client.post("/upload",
                    files={"file": ("s.txt", b"PLAINTEXT-SENTINEL")},
                    data={"user_address": "0x1A28b19f6d2ea1A05F9eFFbcCcbF7E9571877981"})

    assert r.status_code == 200
    assert filecrypto.is_encrypted(stored["s.txt"])
    assert b"PLAINTEXT-SENTINEL" not in stored["s.txt"]
    assert filecrypto.decrypt(stored["s.txt"]) == b"PLAINTEXT-SENTINEL"


def test_download_returns_decrypted_bytes(client, patch_contract, auth_token, fake_gateway):
    owner = "0x1A28b19f6d2ea1A05F9eFFbcCcbF7E9571877981"
    patch_contract(owners={"cidE": owner})
    fake_gateway({"cidE": filecrypto.encrypt(b"SEALED CONTENT")})

    r = client.get("/download/cidE/s.txt", headers={"x-auth-token": auth_token(owner)})

    assert r.status_code == 200
    assert r.content == b"SEALED CONTENT"


def test_download_tampered_ciphertext_is_502(client, patch_contract, auth_token, fake_gateway):
    owner = "0x1A28b19f6d2ea1A05F9eFFbcCcbF7E9571877981"
    patch_contract(owners={"cidE": owner})
    sealed = bytearray(filecrypto.encrypt(b"SEALED CONTENT"))
    sealed[-1] ^= 0x01
    fake_gateway({"cidE": bytes(sealed)})

    r = client.get("/download/cidE/s.txt", headers={"x-auth-token": auth_token(owner)})

    assert r.status_code == 502
