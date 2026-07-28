"""/attestation: quote plumbing, cert binding, and the no-SGX fallback."""
import base64
import hashlib

import pytest

import routers.attestation as attestation

# A syntactically valid-enough quote: 48-byte header + 384-byte report with
# recognizable measurement fields, plus trailing signature junk.
MRENCLAVE = bytes(range(32))
MRSIGNER = bytes(range(32, 64))

TEST_CERT_DER = b"not-a-real-cert-but-hashable"
TEST_CERT_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    + base64.encodebytes(TEST_CERT_DER).decode()
    + "-----END CERTIFICATE-----\n"
)


def _fake_quote(report_data: bytes) -> bytes:
    assert len(report_data) == 64
    report = bytearray(384)
    report[64:96] = MRENCLAVE
    report[128:160] = MRSIGNER
    report[320:384] = report_data
    return b"\x03\x00" + bytes(46) + bytes(report) + b"sig-junk"


@pytest.fixture
def fake_attestation_dir(tmp_path, monkeypatch):
    """A pseudo-file directory that echoes whatever report_data was written,
    the way Gramine's /dev/attestation does."""
    cert = tmp_path / "cert.pem"
    cert.write_text(TEST_CERT_PEM)
    monkeypatch.setattr(attestation, "TLS_CERT_FILE", str(cert))
    monkeypatch.setattr(attestation, "ATTESTATION_DIR", str(tmp_path))
    monkeypatch.setattr(attestation, "_cached", None)

    expected_report_data = hashlib.sha256(TEST_CERT_DER).digest() + b"\x00" * 32
    (tmp_path / "quote").write_bytes(_fake_quote(expected_report_data))
    return tmp_path


def test_not_in_enclave_is_501(client, monkeypatch, tmp_path):
    monkeypatch.setattr(attestation, "ATTESTATION_DIR", str(tmp_path / "absent"))
    monkeypatch.setattr(attestation, "_cached", None)
    r = client.get("/attestation")
    assert r.status_code == 501
    assert "enclave" in r.json()["error"]


def test_quote_binds_tls_cert_hash(client, fake_attestation_dir):
    r = client.get("/attestation")
    assert r.status_code == 200
    body = r.json()

    cert_hash = hashlib.sha256(TEST_CERT_DER).hexdigest()
    assert body["tls_cert_sha256"] == cert_hash
    # What was written into the enclave's report_data pseudo-file:
    written = (fake_attestation_dir / "user_report_data").read_bytes()
    assert written == bytes.fromhex(cert_hash) + b"\x00" * 32


def test_measurements_parsed_from_quote(client, fake_attestation_dir):
    body = client.get("/attestation").json()
    assert body["mrenclave"] == MRENCLAVE.hex()
    assert body["mrsigner"] == MRSIGNER.hex()
    assert body["report_data"].startswith(hashlib.sha256(TEST_CERT_DER).hexdigest())

    quote = base64.b64decode(body["quote"])
    assert quote[48 + 64:48 + 96] == MRENCLAVE  # convenience fields match raw quote


def test_quote_generated_once_then_cached(client, fake_attestation_dir):
    first = client.get("/attestation").json()
    # If the endpoint regenerated, it would now read this garbage instead.
    (fake_attestation_dir / "quote").write_bytes(_fake_quote(b"\xff" * 64))
    second = client.get("/attestation").json()
    assert second == first
