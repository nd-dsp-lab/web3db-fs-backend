"""Remote attestation: cryptographic proof that this exact backend, inside a
genuine SGX enclave, is the thing terminating the caller's TLS session.

Gramine (with sgx.remote_attestation = "dcap") exposes attestation as
pseudo-files: write 64 bytes into /dev/attestation/user_report_data and the
CPU embeds them in a DCAP quote readable from /dev/attestation/quote, signed
through a chain that ends at Intel's SGX Root CA.

We put sha256(TLS leaf certificate, DER) in the report data. That binds the
quote to the TLS identity: an impostor with its own valid certificate for
the hostname cannot produce a genuine quote whose report data hashes *its*
certificate, because report data can only be set from inside a real enclave
whose MRENCLAVE/MRSIGNER the quote also discloses.

No verifier nonce, deliberately: a replayed quote binds a TLS private key
the replayer does not hold, so the TLS handshake itself is the freshness
proof. That keeps the quote generable once per process and the endpoint
cacheable.

The JSON convenience fields (mrenclave etc.) are parsed out of the quote for
human eyes only — verifiers must parse and cryptographically verify the
quote itself (see sgx/verify-attestation.py).
"""
import base64
import hashlib
import logging
import os
import threading

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# Overridable for tests; the defaults are the enclave's paths.
ATTESTATION_DIR = os.getenv("ATTESTATION_DIR", "/dev/attestation")
TLS_CERT_FILE = os.getenv("TLS_CERT_FILE", "/web3fs/secrets/tls/cert.pem")

# SGX ECDSA quote layout: 48-byte header, then the 384-byte enclave report.
_REPORT = 48
_MRENCLAVE = slice(_REPORT + 64, _REPORT + 96)
_MRSIGNER = slice(_REPORT + 128, _REPORT + 160)
_REPORT_DATA = slice(_REPORT + 320, _REPORT + 384)

_lock = threading.Lock()
_cached = None


def _leaf_cert_der() -> bytes:
    """DER of the first certificate in the served chain — exactly the bytes a
    client receives from the TLS handshake (getpeercert(binary_form=True))."""
    with open(TLS_CERT_FILE, encoding="utf-8") as f:
        pem = f.read()
    body = pem.split("-----BEGIN CERTIFICATE-----")[1].split("-----END CERTIFICATE-----")[0]
    return base64.b64decode("".join(body.split()))


def _generate() -> dict:
    cert_hash = hashlib.sha256(_leaf_cert_der()).digest()
    with open(os.path.join(ATTESTATION_DIR, "user_report_data"), "wb") as f:
        f.write(cert_hash + b"\x00" * 32)
    with open(os.path.join(ATTESTATION_DIR, "quote"), "rb") as f:
        quote = f.read()

    return {
        "quote": base64.b64encode(quote).decode(),
        "report_data_scheme": "sha256(TLS leaf certificate, DER) || 32 zero bytes",
        # Convenience only — trust the quote, not this JSON.
        "tls_cert_sha256": cert_hash.hex(),
        "mrenclave": quote[_MRENCLAVE].hex(),
        "mrsigner": quote[_MRSIGNER].hex(),
        "report_data": quote[_REPORT_DATA].hex(),
    }


@router.get("/attestation")
def attestation():
    global _cached
    if _cached is None:
        with _lock:
            if _cached is None:
                if not os.path.exists(os.path.join(ATTESTATION_DIR, "quote")):
                    return JSONResponse(status_code=501, content={
                        "error": "Attestation unavailable: not running inside an SGX enclave",
                        "detail": "This deployment mode has no hardware quote to offer. "
                                  "Production serves one at https://fs-api.web3db.org/attestation.",
                    })
                try:
                    _cached = _generate()
                except OSError as e:
                    # Quote generation rides through the host's aesmd; if that
                    # hiccups, file serving must not be collateral damage.
                    logger.error("Quote generation failed: %s", e)
                    return JSONResponse(status_code=503, content={
                        "error": "Quote generation failed; try again shortly"})
    return _cached
