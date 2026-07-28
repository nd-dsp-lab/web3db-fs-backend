#!/usr/bin/env python3
"""Verify, from any machine, that fs-api.web3db.org is the published Web3FS
backend running inside a genuine Intel SGX enclave.

    python3 verify-attestation.py [--host fs-api.web3db.org] [--port 443]
                                  [--mrenclave <hex of the published release>]

What is checked, and against what root of trust:

  1. BINDING   — the TLS certificate this connection actually presented
                 hashes to the quote's report_data. Proves the TLS endpoint
                 and the enclave are the same party (an impostor with its own
                 valid certificate fails here).
  2. IDENTITY  — MRSIGNER equals the pinned Web3FS signing key, and
                 MRENCLAVE equals the published per-release measurement (if
                 --mrenclave is given; otherwise it is printed for manual
                 comparison).
  3. GENUINE   — the quote's ECDSA signature chain verifies up to Intel's
                 SGX Root CA (embedded below, cross-checkable against
                 https://certificates.trustedservices.intel.com/): the PCK
                 certificate chain roots there, the PCK key signed the
                 Quoting Enclave's report, that report pins the attestation
                 key, and the attestation key signed this quote. Needs
                 `pip install cryptography`; without it the script SAYS SO
                 and completes only checks 1-2, which parse the quote but do
                 not prove it came from real hardware.

Not checked: Intel's TCB status / CRLs for the platform (whether the host's
microcode is the latest). That requires Intel's collateral service; the
signature chain above already proves genuine SGX hardware and genuine
measurements.

Checks 1-2 need only the standard library. Exit code 0 = every performed
check passed; 1 = a check failed; 2 = could not run.
"""
import argparse
import base64
import hashlib
import json
import socket
import ssl
import sys
import urllib.request

# MRSIGNER = SHA-256 of the enclave signing key's public modulus. Constant
# across releases; changes only if the Web3FS signing key rotates.
PINNED_MRSIGNER = "61838aff783799c244260291db365c485210ea2ca4c73ad336c0017fe5065afe"

REPORT = 48
QUOTE_MIN_LEN = REPORT + 384

# Intel SGX Provisioning Certification Root CA (public, expires 2049).
# sha256 fingerprint 44a0196b2b99f889b8e149e95b807a350e7424964399e885a7cbb8ccfab674d3
INTEL_SGX_ROOT_CA_PEM = b"""-----BEGIN CERTIFICATE-----
MIICjzCCAjSgAwIBAgIUImUM1lqdNInzg7SVUr9QGzknBqwwCgYIKoZIzj0EAwIw
aDEaMBgGA1UEAwwRSW50ZWwgU0dYIFJvb3QgQ0ExGjAYBgNVBAoMEUludGVsIENv
cnBvcmF0aW9uMRQwEgYDVQQHDAtTYW50YSBDbGFyYTELMAkGA1UECAwCQ0ExCzAJ
BgNVBAYTAlVTMB4XDTE4MDUyMTEwNDUxMFoXDTQ5MTIzMTIzNTk1OVowaDEaMBgG
A1UEAwwRSW50ZWwgU0dYIFJvb3QgQ0ExGjAYBgNVBAoMEUludGVsIENvcnBvcmF0
aW9uMRQwEgYDVQQHDAtTYW50YSBDbGFyYTELMAkGA1UECAwCQ0ExCzAJBgNVBAYT
AlVTMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEC6nEwMDIYZOj/iPWsCzaEKi7
1OiOSLRFhWGjbnBVJfVnkY4u3IjkDYYL0MxO4mqsyYjlBalTVYxFP2sJBK5zlKOB
uzCBuDAfBgNVHSMEGDAWgBQiZQzWWp00ifODtJVSv1AbOScGrDBSBgNVHR8ESzBJ
MEegRaBDhkFodHRwczovL2NlcnRpZmljYXRlcy50cnVzdGVkc2VydmljZXMuaW50
ZWwuY29tL0ludGVsU0dYUm9vdENBLmRlcjAdBgNVHQ4EFgQUImUM1lqdNInzg7SV
Ur9QGzknBqwwDgYDVR0PAQH/BAQDAgEGMBIGA1UdEwEB/wQIMAYBAf8CAQEwCgYI
KoZIzj0EAwIDSQAwRgIhAOW/5QkR+S9CiSDcNoowLuPRLsWGf/Yi7GSX94BgwTwg
AiEA4J0lrHoMs+Xo5o/sX6O9QWxHRAvZUGOdRQ7cvqRXaqI=
-----END CERTIFICATE-----
"""


def verify_quote_chain(quote: bytes):
    """Cryptographically verify a v3 ECDSA quote up to Intel's root.

    Layout after the 384-byte report: sig_data_len(4), then ECDSA sig over
    header+report (64, raw r||s), attestation pubkey (64, raw x||y), the
    Quoting Enclave's report (384), the PCK signature over it (64), QE auth
    data (2+n), and the PCK certificate chain (type 5 = PEM).
    Returns None on success, an error string on failure.
    """
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    from cryptography.hazmat.primitives.serialization import Encoding as _Encoding

    def raw_sig_to_der(sig64):
        return encode_dss_signature(int.from_bytes(sig64[:32], "big"),
                                    int.from_bytes(sig64[32:], "big"))

    version, key_type = int.from_bytes(quote[0:2], "little"), int.from_bytes(quote[2:4], "little")
    if version != 3 or key_type != 2:
        return f"unsupported quote (version={version}, key_type={key_type})"

    sig = quote[436:]
    ecdsa_sig, attest_pub_raw = sig[0:64], sig[64:128]
    qe_report, qe_report_sig = sig[128:512], sig[512:576]
    auth_len = int.from_bytes(sig[576:578], "little")
    qe_auth = sig[578:578 + auth_len]
    cd = sig[578 + auth_len:]
    cert_type = int.from_bytes(cd[0:2], "little")
    cert_size = int.from_bytes(cd[2:6], "little")
    if cert_type != 5:
        return f"unsupported certification data type {cert_type}"

    # -- PCK chain: leaf <- intermediate <- root, root pinned to Intel's
    pem_blob = cd[6:6 + cert_size]
    chain = []
    for part in pem_blob.split(b"-----END CERTIFICATE-----")[:-1]:
        chain.append(x509.load_pem_x509_certificate(part + b"-----END CERTIFICATE-----"))
    if len(chain) < 3:
        return f"expected 3 certificates in the PCK chain, got {len(chain)}"
    pinned_root = x509.load_pem_x509_certificate(INTEL_SGX_ROOT_CA_PEM)
    if chain[-1].public_bytes(_Encoding.DER) != pinned_root.public_bytes(_Encoding.DER):
        return "PCK chain does not end at the pinned Intel SGX Root CA"
    try:
        for cert, issuer in zip(chain, chain[1:]):
            issuer.public_key().verify(cert.signature, cert.tbs_certificate_bytes,
                                       ec.ECDSA(hashes.SHA256()))
        pinned_root.public_key().verify(pinned_root.signature,
                                        pinned_root.tbs_certificate_bytes,
                                        ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return "PCK certificate chain signature invalid"

    # -- PCK key signed the Quoting Enclave's report
    try:
        chain[0].public_key().verify(raw_sig_to_der(qe_report_sig), qe_report,
                                     ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return "QE report signature invalid (not signed by the PCK key)"

    # -- the QE report pins the attestation key that signs quotes
    if hashlib.sha256(attest_pub_raw + qe_auth).digest() != qe_report[320:352]:
        return "QE report does not pin this attestation key"

    # -- and that attestation key signed this quote's header + report
    attest_pub = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), b"\x04" + attest_pub_raw)
    try:
        attest_pub.verify(raw_sig_to_der(ecdsa_sig), quote[0:432],
                          ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return "quote signature invalid (not signed by the attested key)"

    return None


def fail(msg):
    print(f"  FAIL  {msg}")
    sys.exit(1)


def ok(msg):
    print(f"  ok    {msg}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="fs-api.web3db.org")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--mrenclave", help="expected MRENCLAVE hex of the published release")
    ap.add_argument("--insecure", action="store_true",
                    help="skip WebPKI validation (staging/self-signed only — the "
                         "cert-to-quote binding check still runs)")
    args = ap.parse_args()

    # -- fetch: live TLS certificate + the quote over that same TLS identity
    ctx = ssl.create_default_context()
    if args.insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((args.host, args.port), timeout=15) as raw:
            with ctx.wrap_socket(raw, server_hostname=args.host) as tls:
                cert_der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as e:
        print(f"cannot connect: {e}")
        sys.exit(2)
    ok("TLS connected" + ("; WebPKI SKIPPED (--insecure)" if args.insecure
                           else f"; WebPKI chain valid for {args.host}"))

    url_ctx = ctx if args.insecure else None
    with urllib.request.urlopen(
            f"https://{args.host}:{args.port}/attestation", timeout=20,
            context=url_ctx) as resp:
        body = json.load(resp)
    quote = base64.b64decode(body["quote"])
    if len(quote) < QUOTE_MIN_LEN:
        fail(f"quote too short ({len(quote)} bytes)")

    report_data = quote[REPORT + 320:REPORT + 384]
    mrenclave = quote[REPORT + 64:REPORT + 96].hex()
    mrsigner = quote[REPORT + 128:REPORT + 160].hex()

    # -- 1. binding: the cert THIS connection presented is in the quote
    cert_hash = hashlib.sha256(cert_der).digest()
    if report_data != cert_hash + b"\x00" * 32:
        fail("quote report_data does NOT match the served TLS certificate — "
             "the TLS endpoint is not the attested enclave")
    ok("quote is bound to the served TLS certificate (report_data matches)")

    # -- 2. identity
    if mrsigner != PINNED_MRSIGNER:
        fail(f"MRSIGNER mismatch: quote says {mrsigner}")
    ok("MRSIGNER matches the pinned Web3FS signing key")

    if args.mrenclave:
        if mrenclave != args.mrenclave.lower():
            fail(f"MRENCLAVE mismatch: quote says {mrenclave}, expected {args.mrenclave}")
        ok("MRENCLAVE matches the published release measurement")
    else:
        print(f"  info  MRENCLAVE = {mrenclave}")
        print("        compare against the published release value, or re-run with --mrenclave")

    # -- 3. genuineness: signature chain to Intel's root
    try:
        import cryptography  # noqa: F401
        have_crypto = True
    except ImportError:
        have_crypto = False

    if have_crypto:
        err = verify_quote_chain(quote)
        if err:
            fail(f"quote chain verification: {err}")
        ok("quote signature chain verifies to the pinned Intel SGX Root CA")
    else:
        print("  WARN  'cryptography' not installed — checks 1-2 done, but the quote's")
        print("        signature chain was NOT verified against Intel's root.")
        print("        pip install cryptography  and re-run for the full proof.")

    print("PASS" if have_crypto else "PASS (structural only)")


if __name__ == "__main__":
    main()
