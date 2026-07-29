"""Log pseudonyms: identifiers must not survive into a log line, but must
stay correlatable for whoever holds the key.
"""
import importlib
import re

import pytest

import logredact

CID = "QmRBa64fgpr5EmVyq5D78mbvPj2SwUfRwgEQYiv2VYy6a1"
CIDV1 = "bafkreidtlsolw5xsgdlzp2jmxvlytwii7x5mkin6cmijhqlcckd7zjefm4"
ADDR = "0x1A28b19f6d2ea1A05F9eFFbcCcbF7E9571877981"


@pytest.fixture(autouse=True)
def pepper(tmp_path, monkeypatch):
    """A fresh key per test, at a path the enclave would seal."""
    monkeypatch.setenv("LOG_PEPPER_FILE", str(tmp_path / "log_pepper"))
    importlib.reload(logredact)
    yield
    importlib.reload(logredact)


# --- the identifier must be gone ---

@pytest.mark.parametrize("fn,value", [
    (logredact.cid, CID),
    (logredact.cid, CIDV1),
    (logredact.addr, ADDR),
    (logredact.fname, "usenixsecurity25-shafran.pdf"),
])
def test_the_original_does_not_appear_in_the_tag(fn, value):
    out = fn(value)
    assert value not in out
    assert value.lower() not in out.lower()


@pytest.mark.parametrize("fn,value", [
    (logredact.cid, CID),
    (logredact.cid, CIDV1),
    (logredact.addr, ADDR),
    (logredact.fname, "usenixsecurity25-shafran.pdf"),
])
def test_no_prefix_of_the_original_survives_either(fn, value):
    """Truncation is the tempting shortcut and it does not work: a CID prefix
    is resolvable against the host's own IPFS repo, an address prefix against
    the public contract. Any run of the original is a leak."""
    out = fn(value)
    body = out.split("#", 1)[1]
    for length in range(4, len(value) + 1):
        assert value[:length].lower() not in body.lower(), (
            f"the first {length} characters of the identifier survived into {out!r}")


def test_a_filename_keeps_only_its_extension():
    out = logredact.fname("usenixsecurity25-shafran.pdf")
    assert out.endswith(".pdf")
    assert "usenix" not in out and "shafran" not in out


def test_an_email_keeps_only_its_domain():
    out = logredact.email("ada.lovelace@nd.edu")
    assert out.endswith("@nd.edu")
    assert "ada" not in out and "lovelace" not in out


# --- but the tag must stay useful ---

def test_tags_are_stable_so_lines_can_be_correlated():
    assert logredact.cid(CID) == logredact.cid(CID)
    assert logredact.addr(ADDR) == logredact.addr(ADDR)


def test_an_address_tags_the_same_whatever_its_case():
    # the app logs a checksummed address in some places and lowercase in
    # others; one user must not read as two
    assert logredact.addr(ADDR) == logredact.addr(ADDR.lower())


def test_different_identifiers_get_different_tags():
    other = "QmX9Ms4QKjmjCQ2zoT7nBFZLfnwd59vYef97QN9jzzii5i"
    assert logredact.cid(CID) != logredact.cid(other)


def test_the_domain_separator_keeps_the_kinds_apart():
    value = "0x1a28b19f6d2ea1a05f9effbcccbf7e9571877981"
    assert logredact.addr(value)[5:] != logredact.cid(value)[4:]


def test_a_different_key_gives_different_tags(tmp_path, monkeypatch):
    first = logredact.cid(CID)
    monkeypatch.setenv("LOG_PEPPER_FILE", str(tmp_path / "other_pepper"))
    importlib.reload(logredact)
    assert logredact.cid(CID) != first, "the tag must depend on the key, not just the value"


def test_the_key_is_generated_once_and_reused(tmp_path, monkeypatch):
    path = tmp_path / "generated" / "log_pepper"
    monkeypatch.setenv("LOG_PEPPER_FILE", str(path))
    importlib.reload(logredact)

    first = logredact.cid(CID)
    assert path.exists(), "the key should be created beside the other secrets"
    importlib.reload(logredact)
    assert logredact.cid(CID) == first, "an existing key must be reused, not replaced"


# --- paths, where the identifiers are embedded rather than passed ---

def test_a_download_path_loses_both_the_cid_and_the_filename():
    out = logredact.path(f"/download/{CID}/usenixsecurity25-shafran.pdf")
    assert out.startswith("/download/")
    assert CID not in out
    assert "usenix" not in out


def test_a_query_string_loses_the_address():
    out = logredact.path(f"/?user_address={ADDR}")
    assert ADDR not in out
    assert logredact.addr(ADDR) in out


def test_a_path_with_nothing_sensitive_is_left_alone():
    assert logredact.path("/storage-stats") == "/storage-stats"
    assert logredact.path("/attestation") == "/attestation"


def test_a_cid_inside_a_path_tags_the_same_as_on_its_own():
    # a line about /thumbnail/<cid> and a line about that cid must join up
    assert logredact.cid(CID) in logredact.path(f"/thumbnail/{CID}")


def test_a_folder_path_loses_every_segment_that_names_a_file():
    out = logredact.path("/Papers/2026/usenixsecurity25-shafran.pdf")
    assert "usenix" not in out
    assert out.startswith("/Papers/2026/")


def test_missing_values_do_not_crash_the_logging_call():
    # verify_auth_token returns None for an unauthenticated request, and that
    # value goes straight into a log line
    assert logredact.addr(None) == "user#anon"
    assert logredact.cid(None) == "cid#-"
    assert logredact.fname(None) == "file#-"
    assert logredact.path(None) == ""
    assert logredact.email(None) == "email#-"


def test_a_name_without_an_extension_still_tags():
    out = logredact.fname("Makefile")
    assert re.fullmatch(r"file#[0-9a-f]{8}", out)
