"""Token helpers and on-chain download authorization."""
import time

from permissions import DOWNLOAD, READ
from security import (
    auth_message, _token_signature, verify_auth_token, can_download,
    require_download_access,
)

ADDR = "0x1a28b19f6d2ea1a05f9effbcccbf7e9571877981"
OTHER = "0x3081acc05169336e7875ad9f896bf6511397809a"


def _token(address, expiry):
    payload = f"{address}.{expiry}"
    return f"{payload}.{_token_signature(payload)}"


def test_auth_message_is_stable_and_lowercased():
    assert auth_message("0xABC", 123) == "Web3FS sign-in\nAddress: 0xabc\nTimestamp: 123"


def test_valid_token_round_trips():
    tok = _token(ADDR, int(time.time()) + 100)
    assert verify_auth_token(tok) == ADDR


def test_expired_token_rejected():
    assert verify_auth_token(_token(ADDR, int(time.time()) - 1)) is None


def test_tampered_token_rejected():
    tok = _token(ADDR, int(time.time()) + 100)
    assert verify_auth_token(tok[:-1] + ("0" if tok[-1] != "0" else "1")) is None


def test_malformed_token_rejected():
    assert verify_auth_token("garbage") is None
    assert verify_auth_token("") is None
    assert verify_auth_token(None) is None


def test_can_download_owner(patch_contract):
    patch_contract(owners={"cidA": ADDR})
    assert can_download("cidA", ADDR) is True


def test_can_download_with_permission_bit(patch_contract):
    from web3 import Web3
    checksum = Web3.to_checksum_address(OTHER)
    patch_contract(owners={"cidA": ADDR}, permissions={("cidA", checksum): READ | DOWNLOAD})
    assert can_download("cidA", OTHER) is True


def test_can_download_denied_without_bit(patch_contract):
    from web3 import Web3
    checksum = Web3.to_checksum_address(OTHER)
    patch_contract(owners={"cidA": ADDR}, permissions={("cidA", checksum): READ})
    assert can_download("cidA", OTHER) is False


def _valid_token(address):
    payload = f"{address}.{int(time.time()) + 100}"
    return f"{payload}.{_token_signature(payload)}"


def test_require_download_access_allows_owner(patch_contract):
    patch_contract(owners={"cidA": ADDR})
    assert require_download_access("cidA", _valid_token(ADDR)) is None


def test_require_download_access_401_without_token(patch_contract):
    patch_contract(owners={"cidA": ADDR})
    resp = require_download_access("cidA", None)
    assert resp.status_code == 401


def test_require_download_access_403_without_permission(patch_contract):
    from web3 import Web3
    checksum = Web3.to_checksum_address(OTHER)
    patch_contract(owners={"cidA": ADDR}, permissions={("cidA", checksum): READ})
    resp = require_download_access("cidA", _valid_token(OTHER))
    assert resp.status_code == 403
