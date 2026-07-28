"""One-shot enclave entrypoint: generate a TLS keypair inside the enclave and
print its certificate signing request.

The private key is written straight into the sealed mount and never exists
anywhere else — not on the operator's workstation, not on the host. What
comes out on stdout is a CSR: a public key and a hostname, signed by the
private key to prove possession. Nothing in it is secret, so the rest of
the issuance flow handles public data only and needs no trusted machine.

    ssh <sgx-host> 'cd <sgx-dir> && gramine-sgx csr' > fs-api.csr

The new key lands beside the live one as key.pem.new; the running server
keeps using key.pem until `promote` swaps them. A failed or abandoned
issuance therefore leaves production untouched.
"""
import os
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

TLS_DIR = "/web3fs/secrets/tls"
DOMAIN = os.getenv("TLS_DOMAIN", "fs-api.web3db.org")


def main():
    key = ec.generate_private_key(ec.SECP256R1())

    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, DOMAIN)]))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(DOMAIN)]), critical=False)
        .sign(key, hashes.SHA256())
    )

    os.makedirs(TLS_DIR, exist_ok=True)
    path = os.path.join(TLS_DIR, "key.pem.new")
    # Encrypted files verify their existing contents on write, so a leftover
    # (or damaged) pending key has to go before this one can land.
    if os.path.exists(path):
        os.remove(path)
    with open(path, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))

    sys.stdout.write(csr.public_bytes(serialization.Encoding.PEM).decode())
    print(f"sealed a new P-256 key for {DOMAIN}; promote it once the "
          f"certificate comes back", file=sys.stderr)


if __name__ == "__main__":
    main()
