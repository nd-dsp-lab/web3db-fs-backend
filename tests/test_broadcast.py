"""The relay that stands in for a browser-reachable RPC.

The wallet signs; the enclave forwards. These cover what the relay must
refuse as much as what it forwards, because the endpoint is the only public
write path to a chain that is otherwise reachable only on loopback.
"""
import sys
from types import SimpleNamespace

import pytest
from eth_account import Account
from hexbytes import HexBytes
from web3 import Web3

import routers.broadcast as broadcast


def _signed_tx(acct=None, nonce=0):
    """A real signed transaction, so recover_transaction has something to recover."""
    acct = acct or Account.create()
    tx = {
        "to": "0x" + "11" * 20,
        "value": 0,
        "gas": 21000,
        "gasPrice": 0,
        "nonce": nonce,
        "chainId": 20260928,
        "data": b"",
    }
    signed = Account.sign_transaction(tx, acct.key)
    return acct, Web3.to_hex(signed.rawTransaction)


@pytest.fixture
def node(monkeypatch):
    """Stand-in for the chain. Records what it was asked to broadcast."""
    sent = []

    class FakeEth:
        def send_raw_transaction(self, raw):
            sent.append(raw)
            return HexBytes(Web3.keccak(raw))

    fake = SimpleNamespace(eth=FakeEth(), sent=sent)
    monkeypatch.setattr(broadcast, "w3", fake)
    return fake


def test_relays_a_transaction_signed_by_the_token_holder(client, auth_token, node):
    acct, raw = _signed_tx()
    res = client.post("/broadcast", json={"raw_transaction": raw},
                      headers={"X-Auth-Token": auth_token(acct.address)})
    assert res.status_code == 200
    assert res.json()["tx_hash"] == Web3.keccak(hexstr=raw).hex()
    assert node.sent == [Web3.to_bytes(hexstr=raw)], "the bytes must reach the node unaltered"


def test_without_a_token_nothing_is_broadcast(client, node):
    _, raw = _signed_tx()
    res = client.post("/broadcast", json={"raw_transaction": raw})
    assert res.status_code == 401
    assert node.sent == []


def test_a_token_cannot_relay_someone_elses_transaction(client, auth_token, node):
    # Gas is free on this chain, so an open relay is free spam. The signer
    # must be the wallet that proved ownership.
    _, raw = _signed_tx()
    other = Account.create()
    res = client.post("/broadcast", json={"raw_transaction": raw},
                      headers={"X-Auth-Token": auth_token(other.address)})
    assert res.status_code == 403
    assert node.sent == []


def test_garbage_is_refused_before_the_node_is_asked(client, auth_token, node):
    acct = Account.create()
    res = client.post("/broadcast", json={"raw_transaction": "0xdeadbeef"},
                      headers={"X-Auth-Token": auth_token(acct.address)})
    assert res.status_code == 400
    assert node.sent == []


def test_non_hex_is_refused(client, auth_token, node):
    acct = Account.create()
    res = client.post("/broadcast", json={"raw_transaction": "not hex at all"},
                      headers={"X-Auth-Token": auth_token(acct.address)})
    assert res.status_code == 400
    assert node.sent == []


def test_a_resend_reports_the_same_hash_rather_than_failing(client, auth_token, monkeypatch):
    # A retry after a dropped response must not look like a new failure: the
    # hash is a function of the signed bytes, so it is the one the first
    # attempt produced.
    acct, raw = _signed_tx()

    class Rejecting:
        def send_raw_transaction(self, raw):
            raise ValueError("already known")

    monkeypatch.setattr(broadcast, "w3", SimpleNamespace(eth=Rejecting()))
    res = client.post("/broadcast", json={"raw_transaction": raw},
                      headers={"X-Auth-Token": auth_token(acct.address)})
    assert res.status_code == 200
    assert res.json()["tx_hash"] == Web3.keccak(hexstr=raw).hex()


def test_a_rejected_transaction_surfaces_as_a_gateway_error(client, auth_token, monkeypatch):
    acct, raw = _signed_tx()

    class Rejecting:
        def send_raw_transaction(self, raw):
            raise ValueError("nonce too low")

    monkeypatch.setattr(broadcast, "w3", SimpleNamespace(eth=Rejecting()))
    res = client.post("/broadcast", json={"raw_transaction": raw},
                      headers={"X-Auth-Token": auth_token(acct.address)})
    assert res.status_code == 502
    assert "nonce too low" in res.json()["error"]
