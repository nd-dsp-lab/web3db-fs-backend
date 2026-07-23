"""Shared test fixtures.

Env vars are set BEFORE any app module is imported so configure.py builds a
contract from a dummy address (no network) and security.py uses a known
AUTH_SECRET. load_dotenv(override=False) in configure leaves these intact, so
the suite is hermetic and never touches the real .env, IPFS, or Sepolia.
"""
import json
import os
import sys
import time
from types import SimpleNamespace

# --- hermetic environment (must precede app imports) ---
os.environ.setdefault("AUTH_SECRET", "00" * 32)
os.environ.setdefault("CONTRACT_ADDRESS", "0x463FA1e9cF1f7f8b1b450708773aBa8BaBBe86AF")
os.environ.setdefault("INFURA_URL", "http://localhost:9")  # unreachable on purpose
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("LOG_DIR", "")  # console only; don't write a log file during tests

APP_DIR = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, os.path.abspath(APP_DIR))

import pytest  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.messages import encode_defunct  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ZERO_ADDR = "0x0000000000000000000000000000000000000000"


@pytest.fixture
def client():
    from server import app
    return TestClient(app)


class FakeContract:
    """Stands in for a web3 contract. Each on-chain function is fed from a dict
    or callable, and X(*args).call() looks the answer up by its first arg
    (the cid), matching how the app calls getFileOwner / getPermissions / etc.
    """

    def __init__(self, owners=None, shared=None, permissions=None, user_files=None):
        self._owners = owners or {}
        self._shared = shared or {}
        self._permissions = permissions or {}
        self._user_files = user_files or []
        self.functions = self  # app calls contract.functions.X(...)

    def getFileOwner(self, cid):
        return SimpleNamespace(call=lambda: self._owners.get(cid, ZERO_ADDR))

    def getSharedUsers(self, cid):
        return SimpleNamespace(call=lambda: list(self._shared.get(cid, [])))

    def getPermissions(self, cid, user):
        return SimpleNamespace(call=lambda: self._permissions.get((cid, user), 0))

    def getUserFiles(self, user):
        return SimpleNamespace(call=lambda: list(self._user_files))


@pytest.fixture
def patch_contract(monkeypatch):
    """Install a FakeContract into every module that imported the real one.
    Returns the installer so a test can shape the on-chain state it needs.
    """
    def _install(**kwargs):
        fake = FakeContract(**kwargs)
        for mod in ["configure", "security", "helpers",
                    "routers.files", "routers.download", "routers.upload",
                    "routers.sharing", "routers.file_ops"]:
            __import__(mod)
            monkeypatch.setattr(sys.modules[mod], "contract", fake, raising=False)
        return fake
    return _install


class FakeFn:
    """One contract function call. Reads answer via .call(); writes record the
    base transaction they were handed and echo the function name and args back
    inside the built transaction so tests can assert on both."""

    def __init__(self, name, args, contract):
        self.name = name
        self.args = args
        self.contract = contract

    def call(self):
        return self.contract.answer(self.name, self.args)

    def estimate_gas(self, base_tx):
        self.contract.estimates.append((self.name, self.args, dict(base_tx)))
        return self.contract.gas_estimate

    def build_transaction(self, base_tx):
        self.contract.built.append((self.name, self.args, dict(base_tx)))
        return {**base_tx, "_fn": self.name, "_args": self.args}


class FakeTxContract:
    """Contract stand-in for the helpers layer, where the interesting output is
    the *transaction dict* rather than a route's JSON body."""

    def __init__(self, owners=None, shared=None, user_files=None, gas_estimate=100_000):
        self._owners = owners or {}
        self._shared = shared or {}
        self._user_files = user_files or []
        self.gas_estimate = gas_estimate
        self.built = []      # [(fn_name, args, base_tx)] for build_transaction
        self.estimates = []  # [(fn_name, args, base_tx)] for estimate_gas
        self.functions = self

    def answer(self, name, args):
        if name == "getFileOwner":
            return self._owners.get(args[0], ZERO_ADDR)
        if name == "getSharedUsers":
            return list(self._shared.get(args[0], []))
        if name == "getUserFiles":
            return list(self._user_files)
        raise AssertionError(f"unexpected read call: {name}")

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args: FakeFn(name, args, self)


class FakeEth:
    def __init__(self, nonce=7, gas_price=1_000_000_000):
        self.gas_price = gas_price
        self._nonce = nonce
        self.nonce_queries = []

    def get_transaction_count(self, address):
        self.nonce_queries.append(address)
        return self._nonce


@pytest.fixture
def patch_helpers(monkeypatch):
    """Install a transaction-capturing contract and a fake w3 into helpers.

    Returns (contract, eth) so a test can shape on-chain state and then assert
    on what was built.
    """
    import helpers

    def _install(nonce=7, gas_price=1_000_000_000, **contract_kwargs):
        fake = FakeTxContract(**contract_kwargs)
        eth = FakeEth(nonce=nonce, gas_price=gas_price)
        monkeypatch.setattr(helpers, "contract", fake)
        monkeypatch.setattr(helpers, "w3", SimpleNamespace(eth=eth))
        return fake, eth

    return _install


@pytest.fixture
def auth_token():
    """Mint a valid download token for an address, as /auth/token would."""
    from security import _token_signature

    def _make(address, ttl=100):
        payload = f"{address.lower()}.{int(time.time()) + ttl}"
        return f"{payload}.{_token_signature(payload)}"

    return _make


class FakeIPFSResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code
        self.content = text.encode()
        self.raw = SimpleNamespace(readline=lambda: text.splitlines()[0].encode())

    def raise_for_status(self):
        if self.status_code != 200:
            raise Exception(f"HTTP {self.status_code}")

    def close(self):
        pass


class FakeIPFS:
    """Emulates the kubo /add and /pin/add HTTP API.

    Faithful on the one behaviour that has bitten us: adding under a name
    containing a slash makes kubo build a wrapper directory and emit an extra
    line for it *last*, so a caller that keeps the last line stores the
    directory CID instead of the file's.
    """

    def __init__(self):
        self.added_names = []   # names as handed to /add
        self.pinned = []

    def cid_for(self, name):
        return "Qmfile" + name.split("/")[-1].replace(".", "")

    def post(self, url, files=None, **kwargs):
        if "/pin/add" in url:  # must precede the /add check — it contains it
            self.pinned.append(url.split("arg=")[-1])
            return FakeIPFSResponse("{}")
        if "/add" in url:
            name, _data = files["file"]
            self.added_names.append(name)
            lines = [json.dumps({"Name": name, "Hash": self.cid_for(name), "Size": "10"})]
            if "/" in name:
                wrapper = name.split("/")[0]
                lines.append(json.dumps({"Name": wrapper, "Hash": "Qmdir" + wrapper, "Size": "20"}))
            return FakeIPFSResponse("\n".join(lines))
        return FakeIPFSResponse("{}", status_code=404)


@pytest.fixture
def fake_ipfs(monkeypatch):
    """Swap the `requests` module used by the upload router for a fake node."""
    import routers.upload as upload

    node = FakeIPFS()
    monkeypatch.setattr(upload, "requests", node)
    return node


@pytest.fixture
def fake_gateway(monkeypatch):
    """Swap httpx.AsyncClient in the download router for a scripted gateway.

    Call the returned installer with {cid: bytes} plus an optional status for
    misses; it records which CIDs were fetched.
    """
    import routers.download as download

    def _install(content_by_cid, miss_status=404):
        fetched = []

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url):
                cid = url.rstrip("/").split("/")[-1]
                fetched.append(cid)
                if cid in content_by_cid:
                    return SimpleNamespace(status_code=200, content=content_by_cid[cid])
                return SimpleNamespace(status_code=miss_status, content=b"")

        monkeypatch.setattr(download.httpx, "AsyncClient", lambda **kw: FakeClient())
        return fetched

    return _install


@pytest.fixture
def sign_login():
    """Produce (address, timestamp, signature) for the /auth/token flow using
    a throwaway key, signing the exact message security.auth_message builds.
    """
    from security import auth_message

    def _sign(timestamp=None):
        ts = int(time.time()) if timestamp is None else timestamp
        acct = Account.create()
        msg = auth_message(acct.address, ts)
        sig = Account.sign_message(encode_defunct(text=msg), acct.key).signature.hex()
        return acct.address, ts, sig

    return _sign
