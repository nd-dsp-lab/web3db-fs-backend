"""One-shot enclave entrypoint: activate a pending TLS key and certificate.

`csr` seals key.pem.new; `provision` seals cert.pem.new alongside it. This
checks that the certificate actually belongs to that key, then makes the
pair live. Splitting activation from issuance is what keeps a failed renewal
harmless: until this runs, the server is still serving the old pair, and
nothing that came back from the CA has been trusted.

    ssh <sgx-host> 'cd <sgx-dir> && gramine-sgx promote'

Then restart the server for it to pick up the new pair.
"""
import os
import sys

from cryptography import x509
from cryptography.hazmat.primitives import serialization

TLS_DIR = "/web3fs/secrets/tls"


def _read(name):
    with open(os.path.join(TLS_DIR, name), "rb") as f:
        return f.read()


def _write(name, data):
    path = os.path.join(TLS_DIR, name)
    # Rewriting an encrypted file verifies the old contents first, so replace
    # rather than overwrite.
    if os.path.exists(path):
        os.remove(path)
    with open(path, "wb") as f:
        f.write(data)


def main():
    for required in ("key.pem.new", "cert.pem.new"):
        if not os.path.exists(os.path.join(TLS_DIR, required)):
            sys.exit(f"nothing to promote: {required} is missing")

    key_pem, cert_pem = _read("key.pem.new"), _read("cert.pem.new")
    key = serialization.load_pem_private_key(key_pem, password=None)
    cert = x509.load_pem_x509_certificate(cert_pem)

    pub = lambda k: k.public_bytes(  # noqa: E731 - one expression, used twice
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if pub(cert.public_key()) != pub(key.public_key()):
        sys.exit("certificate does not match the pending key — refusing to promote")

    _write("key.pem", key_pem)
    _write("cert.pem", cert_pem)
    os.remove(os.path.join(TLS_DIR, "key.pem.new"))
    os.remove(os.path.join(TLS_DIR, "cert.pem.new"))

    names = cert.subject.rfc4514_string()
    print(f"promoted {names}, valid until {cert.not_valid_after_utc:%Y-%m-%d}; "
          f"restart the server to serve it", file=sys.stderr)


if __name__ == "__main__":
    main()
