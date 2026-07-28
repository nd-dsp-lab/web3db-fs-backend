"""The enclave-side TLS tools: key generation stays inside, promotion is
refused unless the certificate really belongs to the pending key.

These import the sgx/ scripts by path — they are enclave entrypoints, not
part of the app package.
"""
import datetime
import importlib.util
import os

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

SGX_DIR = os.path.join(os.path.dirname(__file__), "..", "sgx")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SGX_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


csr_tool = _load("csr_tool", "csr.py")
promote_tool = _load("promote_tool", "promote.py")


def _pub(key_or_cert):
    key = key_or_cert.public_key() if hasattr(key_or_cert, "public_key") else key_or_cert
    return key.public_bytes(serialization.Encoding.DER,
                            serialization.PublicFormat.SubjectPublicKeyInfo)


def _self_signed(key, cn="fs-api.web3db.org"):
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    return (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=30))
            .sign(key, hashes.SHA256()))


# --- csr.py ---

def test_csr_seals_key_and_emits_matching_request(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(csr_tool, "TLS_DIR", str(tmp_path))
    csr_tool.main()

    printed = capsys.readouterr().out
    assert "BEGIN CERTIFICATE REQUEST" in printed
    # The private key stays behind; only the request is printed.
    assert "PRIVATE KEY" not in printed

    key = serialization.load_pem_private_key((tmp_path / "key.pem.new").read_bytes(), password=None)
    csr = x509.load_pem_x509_csr(printed.encode())
    assert csr.is_signature_valid
    assert _pub(csr.public_key()) == _pub(key)
    assert csr.subject.rfc4514_string() == "CN=fs-api.web3db.org"


def test_csr_leaves_the_live_key_alone(tmp_path, monkeypatch):
    (tmp_path / "key.pem").write_bytes(b"the key currently being served")
    monkeypatch.setattr(csr_tool, "TLS_DIR", str(tmp_path))
    csr_tool.main()
    assert (tmp_path / "key.pem").read_bytes() == b"the key currently being served"


def test_csr_replaces_an_abandoned_pending_key(tmp_path, monkeypatch):
    (tmp_path / "key.pem.new").write_bytes(b"left over from a failed renewal")
    monkeypatch.setattr(csr_tool, "TLS_DIR", str(tmp_path))
    csr_tool.main()
    assert b"left over" not in (tmp_path / "key.pem.new").read_bytes()


# --- promote.py ---

def _stage(tmp_path, key, cert):
    (tmp_path / "key.pem.new").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    (tmp_path / "cert.pem.new").write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def test_promote_activates_a_matching_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(promote_tool, "TLS_DIR", str(tmp_path))
    key = ec.generate_private_key(ec.SECP256R1())
    _stage(tmp_path, key, _self_signed(key))
    (tmp_path / "key.pem").write_bytes(b"old key")
    (tmp_path / "cert.pem").write_bytes(b"old cert")

    promote_tool.main()

    assert _pub(serialization.load_pem_private_key((tmp_path / "key.pem").read_bytes(),
                                                   password=None)) == _pub(key)
    assert b"BEGIN CERTIFICATE" in (tmp_path / "cert.pem").read_bytes()
    # the pending pair is consumed, so a later promote has nothing to do
    assert not (tmp_path / "key.pem.new").exists()
    assert not (tmp_path / "cert.pem.new").exists()


def test_promote_refuses_a_certificate_for_a_different_key(tmp_path, monkeypatch):
    monkeypatch.setattr(promote_tool, "TLS_DIR", str(tmp_path))
    pending = ec.generate_private_key(ec.SECP256R1())
    stranger = ec.generate_private_key(ec.SECP256R1())
    _stage(tmp_path, pending, _self_signed(stranger))
    (tmp_path / "key.pem").write_bytes(b"old key")

    with pytest.raises(SystemExit) as e:
        promote_tool.main()

    assert "does not match" in str(e.value)
    # production is untouched: the live key stays, the pending pair survives
    assert (tmp_path / "key.pem").read_bytes() == b"old key"
    assert (tmp_path / "key.pem.new").exists()


@pytest.mark.parametrize("present", ["key.pem.new", "cert.pem.new"])
def test_promote_needs_both_halves(tmp_path, monkeypatch, present):
    monkeypatch.setattr(promote_tool, "TLS_DIR", str(tmp_path))
    (tmp_path / present).write_bytes(b"half a renewal")

    with pytest.raises(SystemExit) as e:
        promote_tool.main()

    assert "nothing to promote" in str(e.value)


# --- the signing secret moves into the sealed mount too ---

def test_auth_secret_is_generated_at_the_configured_path(tmp_path, monkeypatch):
    import security

    monkeypatch.delenv("AUTH_SECRET", raising=False)
    sealed = tmp_path / "auth_secret"
    monkeypatch.setenv("AUTH_SECRET_FILE", str(sealed))
    importlib.reload(security)
    try:
        assert sealed.exists(), "secret should be created where the enclave seals it"
        first = security.AUTH_SECRET
        assert len(first) == 32

        importlib.reload(security)
        assert security.AUTH_SECRET == first, "an existing secret must be reused, not replaced"
    finally:
        # other tests import security expecting the suite's fixed secret
        monkeypatch.setenv("AUTH_SECRET", "00" * 32)
        monkeypatch.delenv("AUTH_SECRET_FILE", raising=False)
        importlib.reload(security)


