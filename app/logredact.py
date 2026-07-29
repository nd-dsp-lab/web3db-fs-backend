"""Pseudonyms for the identifiers that appear in log lines.

The log directory is a plain passthrough mount: unlike secrets/ and thumbs/,
it is written to the host's disk in the clear, because logs nobody can read
without the enclave are logs nobody reads. File *contents* never appear there,
but the identifiers around them used to — and filenames, CIDs and wallet
addresses are most of what the enclave is supposed to be hiding. A line like

    Serving usenixsecurity25-shafran.pdf (QmRBa6...) to 0x1a28b19f...

tells a host administrator what a named person is reading.

Truncating those identifiers would not have helped. A CID prefix is uniquely
resolvable against the host's own IPFS repo, and an address prefix against the
public contract, so the host would simply map the short forms back. Instead
each identifier is replaced by an HMAC of it under a key kept beside the other
secrets — inside the encrypted mount in production. The tags are stable, so a
line can still be followed across a session and matched to other lines about
the same file or user, and anyone with the key (which means anyone running the
enclave) can confirm a tag belongs to a given CID by recomputing it. Without
the key a tag is not reversible: the space of CIDs is large, and guessing is
the only attack.

What this does not hide: request counts, sizes, timings, and the fact that a
particular pseudonymous user was active. A host watching over time can still
see that user#7c1e0b42 uploads every Tuesday. Breaking that would mean not
logging at all.
"""
import hashlib
import hmac
import os
import re
import secrets
import threading

_KEY_FILE = os.path.join(os.path.dirname(__file__), "..", "secrets", "log_pepper")

_key = None
_key_lock = threading.Lock()

# Enough bits that collisions are not a practical concern at this scale, short
# enough to stay readable in a log line.
_TAG_LEN = 8


def _pepper() -> bytes:
    global _key
    if _key is None:
        with _key_lock:
            if _key is None:
                path = os.getenv("LOG_PEPPER_FILE") or _KEY_FILE
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


def _tag(domain: str, value: str) -> str:
    # The domain separator keeps a filename that happens to equal an address
    # from tagging the same in both.
    mac = hmac.new(_pepper(), f"{domain}:{value}".encode(), hashlib.sha256)
    return mac.hexdigest()[:_TAG_LEN]


def cid(value) -> str:
    if not value:
        return "cid#-"
    return f"cid#{_tag('cid', str(value))}"


def addr(value) -> str:
    """Wallet address. Case is normalised so checksummed and lowercase forms of
    one address do not become two different users in the log."""
    if not value:
        return "user#anon"
    return f"user#{_tag('addr', str(value).lower())}"


def fname(value) -> str:
    """Filename, keeping the extension: the type of a file is useful when
    reading a stack trace and is already implied by the endpoint."""
    if not value:
        return "file#-"
    stem, dot, ext = str(value).rpartition(".")
    # rpartition puts everything in `ext` when there is no dot at all
    tag = _tag("fname", str(value))
    return f"file#{tag}.{ext}" if dot and len(ext) <= 8 else f"file#{tag}"


def path(value) -> str:
    """A URL path or folder path, with every identifier inside it replaced.

    Applied to request lines, where the identifiers are embedded rather than
    passed as arguments: /download/<cid>/<filename>, /?user_address=<addr>.
    """
    if not value:
        return ""
    out = _ADDR_RE.sub(lambda m: addr(m.group(0)), str(value))
    out = _CID_RE.sub(lambda m: cid(m.group(0)), out)
    return _FILE_RE.sub(lambda m: fname(m.group(0)), out)


def email(value) -> str:
    """Kept coarser than the rest: the domain is operationally useful (it says
    which identity provider is involved) and is not personal on its own."""
    if not value or "@" not in str(value):
        return "email#-"
    _, _, domain = str(value).partition("@")
    return f"email#{_tag('email', str(value))}@{domain}"


_ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}")
# CIDv0 (base58, always Qm...) and CIDv1 (base32, bafy.../bafk...)
_CID_RE = re.compile(r"\b(Qm[1-9A-HJ-NP-Za-km-z]{44}|ba[a-z2-7]{57,})\b")
# Anything left that looks like a filename. Runs last, so it cannot swallow a
# CID or an address that has already been replaced.
_FILE_RE = re.compile(r"[^/?&=\s]+\.[A-Za-z0-9]{1,8}\b")
