"""The Besu QBFT chain is POA: its block headers carry a validator set and
round seals in extraData, hundreds of bytes past Ethereum's 32-byte limit.

web3's default block formatter rejects that outright, so every block read
raises. Most write paths never read a block -- they set gasPrice themselves --
but prepare_delete_folder leaves fees to web3, which fetches the latest block
for baseFeePerGas. That asymmetry is why uploads and moves worked on Besu
while delete-folder and empty-trash returned 500 in production.

These tests pin the middleware that fixes it, against the real configured w3.
"""
import pytest
from web3.providers.base import BaseProvider

# A real Besu QBFT header: 437 bytes of extraData, far past the 32 the
# default formatter allows. Trimmed to the fields the formatter touches.
POA_EXTRA_DATA = "0x" + "ab" * 437

BLOCK = {
    "number": "0xdf8a8",
    "hash": "0x" + "11" * 32,
    "parentHash": "0x" + "22" * 32,
    "nonce": "0x0000000000000000",
    "sha3Uncles": "0x" + "33" * 32,
    "logsBloom": "0x" + "00" * 256,
    "transactionsRoot": "0x" + "44" * 32,
    "stateRoot": "0x" + "55" * 32,
    "receiptsRoot": "0x" + "66" * 32,
    "miner": "0x" + "77" * 20,
    "difficulty": "0x1",
    "totalDifficulty": "0x1",
    "extraData": POA_EXTRA_DATA,
    "size": "0x3e8",
    "gasLimit": "0x1c9c380",
    "gasUsed": "0x0",
    "timestamp": "0x6756f000",
    "transactions": [],
    "uncles": [],
    "baseFeePerGas": "0x0",
}


class POAProvider(BaseProvider):
    """Answers eth_getBlockByNumber with a POA header, like Besu does.

    Subclasses BaseProvider so web3's manager gets the request_func it uses
    to run the middleware stack -- which is the thing under test.
    """

    def __init__(self):
        super().__init__()
        self.requests = []

    def make_request(self, method, params):
        self.requests.append(method)
        if method == "eth_getBlockByNumber":
            return {"jsonrpc": "2.0", "id": 1, "result": BLOCK}
        if method == "eth_chainId":
            return {"jsonrpc": "2.0", "id": 1, "result": hex(20260928)}
        raise AssertionError(f"unexpected RPC call: {method}")

    def is_connected(self, show_traceback=False):
        return True


@pytest.fixture
def poa_w3(monkeypatch):
    """The real configured w3, talking to a POA node."""
    import configure
    monkeypatch.setattr(configure.w3, "provider", POAProvider())
    return configure.w3


def test_poa_block_header_parses(poa_w3):
    """Without the middleware this raises ExtraDataLengthError."""
    block = poa_w3.eth.get_block("latest")
    assert block.number == 0xDF8A8


def test_oversized_extra_data_is_preserved_not_truncated(poa_w3):
    """The middleware relocates extraData rather than discarding it, so the
    validator set stays inspectable under its POA name."""
    block = poa_w3.eth.get_block("latest")
    assert "extraData" not in block
    assert len(block.proofOfAuthorityData) == 437


def test_base_fee_readable(poa_w3):
    """prepare_delete_folder's path: web3 reads baseFeePerGas off the latest
    block to fill in fees. zeroBaseFee makes it 0, but it must be readable."""
    assert poa_w3.eth.get_block("latest")["baseFeePerGas"] == 0


def test_middleware_is_actually_installed():
    """Guards against the injection being dropped in a future refactor --
    the symptom in production was a 500 only on delete, easy to miss."""
    import configure
    assert "poa" in configure.w3.middleware_onion
