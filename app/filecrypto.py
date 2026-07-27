"""At-rest encryption for file bytes.

New uploads are sealed with AES-256-GCM before they reach IPFS, so the host
node (and anyone with disk access, `ipfs cat` included) stores only
ciphertext; downloads decrypt after the permission check. In production both
ends happen inside the SGX enclave. Files stored before this existed remain
plaintext — maybe_decrypt() passes anything without the magic header through
untouched.

Encryption is deterministic: the nonce is an HMAC of the plaintext's hash
under the master key, so identical content encrypts to identical bytes and
the CID-based duplicate detection keeps working. Nonce reuse can therefore
only happen for identical plaintext, where it reveals nothing beyond the
content equality that content addressing exposes by design.

The master key is generated on first use next to the other secrets — inside
the Gramine encrypted mount in production, so it never exists in plaintext
outside the enclave. Losing it makes every encrypted file unreadable; it is
sealed to the enclave signing identity, not to this machine, so re-signed
upgrades keep access.
"""
import hashlib
import hmac
import os
import secrets
import threading

from Crypto.Cipher import AES

MAGIC = b"W3FSENC1"
_NONCE_LEN = 12
_TAG_LEN = 16
_HEADER_LEN = len(MAGIC) + _NONCE_LEN + _TAG_LEN

_KEY_FILE = os.path.join(os.path.dirname(__file__), "..", "secrets", "file_master_key")

_key = None
_key_lock = threading.Lock()


def _master_key() -> bytes:
    global _key
    if _key is None:
        with _key_lock:
            if _key is None:
                path = os.getenv("FILE_KEY_FILE") or _KEY_FILE
                try:
                    with open(path) as f:
                        _key = bytes.fromhex(f.read().strip())
                except FileNotFoundError:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    fresh = secrets.token_bytes(32)
                    with open(path, "w") as f:
                        f.write(fresh.hex())
                    _key = fresh
    return _key


def encrypt(data: bytes) -> bytes:
    key = _master_key()
    nonce = hmac.new(key, b"w3fs-nonce" + hashlib.sha256(data).digest(), hashlib.sha256).digest()[:_NONCE_LEN]
    ciphertext, tag = AES.new(key, AES.MODE_GCM, nonce=nonce).encrypt_and_digest(data)
    return MAGIC + nonce + tag + ciphertext


def is_encrypted(data: bytes) -> bool:
    return data[:len(MAGIC)] == MAGIC


def decrypt(data: bytes) -> bytes:
    """Raises ValueError if the ciphertext was tampered with (GCM tag check)."""
    nonce = data[len(MAGIC):len(MAGIC) + _NONCE_LEN]
    tag = data[len(MAGIC) + _NONCE_LEN:_HEADER_LEN]
    ciphertext = data[_HEADER_LEN:]
    return AES.new(_master_key(), AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ciphertext, tag)


def maybe_decrypt(data: bytes) -> bytes:
    """Decrypt our own sealed format; pass legacy plaintext files through."""
    return decrypt(data) if is_encrypted(data) else data
