"""File listing and storage-stats endpoints."""
import routers.files as files_mod
from permissions import READ, DOWNLOAD

OWNER = "0x1A28b19f6d2ea1A05F9eFFbcCcbF7E9571877981"
OTHER = "0x3081Acc05169336e7875ad9f896bF6511397809a"
WALLET = "0x6e02F541dd762E5077e6d619AA2F2d81371AcABE"


def test_get_files_shapes_records(client, patch_contract, monkeypatch):
    from web3 import Web3
    owner_cs = Web3.to_checksum_address(OWNER)
    patch_contract(
        user_files=[
            ("cid1", "docs/a.txt", "txt", 111),   # owned, nested
            ("cid2", "b.txt", "txt", 222),         # shared to me (owned by OTHER), root
        ],
        owners={"cid1": owner_cs, "cid2": Web3.to_checksum_address(OTHER)},
        shared={"cid1": [OTHER]},
        permissions={("cid1", OTHER): READ, ("cid2", owner_cs): READ | DOWNLOAD},
    )
    monkeypatch.setattr(files_mod, "get_file_size", lambda cid: 42)

    r = client.get("/", params={"user_address": OWNER})
    assert r.status_code == 200
    recs = {f["cid"]: f for f in r.json()["user_files"]}

    assert recs["cid1"]["filename"] == "a.txt"
    assert recs["cid1"]["folder_path"] == "/docs"
    assert recs["cid1"]["is_owner"] is True
    assert recs["cid1"]["shared_with"] == [OTHER]
    assert recs["cid1"]["size"] == 42

    assert recs["cid2"]["filename"] == "b.txt"
    assert recs["cid2"]["folder_path"] == "/"
    assert recs["cid2"]["is_owner"] is False
    assert recs["cid2"]["shared_with"] == []       # only owners get a shared list
    assert recs["cid2"]["permissions"] == READ | DOWNLOAD


# getUserFiles keeps listing a shared cid forever -- it's a plain array, never
# touched by expiry (the contract's "filter, not cleanup" design: nothing ever
# runs at the expiry block to remove it). That used to make the listing drop
# such a file entirely, so an expiring share vanished with no explanation.
# It now stays, flagged, to be shown greyed out and asked about. A *revoked*
# share never reaches here at all -- _revoke pops the cid out of sharedFiles.
def test_get_files_keeps_an_expired_share_and_flags_it(client, patch_contract, monkeypatch):
    from web3 import Web3
    other_cs = Web3.to_checksum_address(OTHER)
    patch_contract(
        user_files=[("cidExpired", "shared.txt", "txt", 111)],
        owners={"cidExpired": other_cs},
        expires={("cidExpired", Web3.to_checksum_address(OWNER)): 400},
        # no permissions entry for (cidExpired, OWNER) -> getPermissions returns 0
    )
    monkeypatch.setattr(files_mod, "get_file_size", lambda cid: 1)

    r = client.get("/", params={"user_address": OWNER})
    assert r.status_code == 200
    rec = r.json()["user_files"][0]
    assert rec["cid"] == "cidExpired"
    assert rec["is_expired"] is True
    assert rec["permissions"] == 0
    assert rec["expires_at_block"] == 400


def test_get_files_does_not_flag_an_owned_file_as_expired(client, patch_contract, monkeypatch):
    from web3 import Web3
    owner_cs = Web3.to_checksum_address(OWNER)
    patch_contract(user_files=[("cid1", "a.txt", "txt", 111)], owners={"cid1": owner_cs})
    monkeypatch.setattr(files_mod, "get_file_size", lambda cid: 1)

    r = client.get("/", params={"user_address": OWNER})
    assert r.json()["user_files"][0]["is_expired"] is False


def test_get_files_reports_the_recipients_own_request_status(client, patch_contract, monkeypatch):
    # So the greyed-out tile can say "requested" instead of looking broken.
    from web3 import Web3
    owner_cs = Web3.to_checksum_address(OWNER)
    patch_contract(
        user_files=[("cidExpired", "shared.txt", "txt", 111)],
        owners={"cidExpired": Web3.to_checksum_address(OTHER)},
        expires={("cidExpired", owner_cs): 400},
        requests={("cidExpired", owner_cs): 1},   # Pending
    )
    monkeypatch.setattr(files_mod, "get_file_size", lambda cid: 1)

    r = client.get("/", params={"user_address": OWNER})
    assert r.json()["user_files"][0]["request_status"] == "pending"


def test_get_files_skips_the_request_read_for_live_files(client, patch_contract, monkeypatch):
    # Only expired files pay for that extra call.
    from web3 import Web3
    owner_cs = Web3.to_checksum_address(OWNER)
    patch_contract(
        user_files=[("cid1", "a.txt", "txt", 111)],
        owners={"cid1": Web3.to_checksum_address(OTHER)},
        permissions={("cid1", owner_cs): READ},
    )
    monkeypatch.setattr(files_mod, "get_file_size", lambda cid: 1)

    r = client.get("/", params={"user_address": OWNER})
    assert r.json()["user_files"][0]["request_status"] is None


def test_get_files_reports_expiry_to_the_recipient(client, patch_contract, monkeypatch):
    # A recipient needs to know their own access is time-limited too --
    # otherwise the file just vanishes later with no warning beforehand.
    from web3 import Web3
    other_cs = Web3.to_checksum_address(OTHER)
    patch_contract(
        user_files=[("cid1", "a.txt", "txt", 111)],
        owners={"cid1": other_cs},
        permissions={("cid1", Web3.to_checksum_address(OWNER)): READ},
        expires={("cid1", Web3.to_checksum_address(OWNER)): 500},
    )
    monkeypatch.setattr(files_mod, "get_file_size", lambda cid: 1)

    r = client.get("/", params={"user_address": OWNER})
    assert r.status_code == 200
    assert r.json()["user_files"][0]["expires_at_block"] == 500


def test_get_files_owner_never_shows_an_expiry_for_their_own_file(client, patch_contract, monkeypatch):
    from web3 import Web3
    owner_cs = Web3.to_checksum_address(OWNER)
    # Owner's own permissions are set directly in _uploadFile, never through
    # _grant, so getExpiresAtBlock is never even consulted for an owned file.
    patch_contract(user_files=[("cid1", "a.txt", "txt", 111)], owners={"cid1": owner_cs})
    monkeypatch.setattr(files_mod, "get_file_size", lambda cid: 1)

    r = client.get("/", params={"user_address": OWNER})
    assert r.status_code == 200
    assert r.json()["user_files"][0]["expires_at_block"] is None


def test_get_files_shared_with_excludes_expired_recipients(client, patch_contract, monkeypatch):
    from web3 import Web3
    owner_cs = Web3.to_checksum_address(OWNER)
    patch_contract(
        user_files=[("cid1", "a.txt", "txt", 111)],
        owners={"cid1": owner_cs},
        shared={"cid1": [OTHER]},  # no permissions entry -> getPermissions returns 0
    )
    monkeypatch.setattr(files_mod, "get_file_size", lambda cid: 1)

    r = client.get("/", params={"user_address": OWNER})
    assert r.status_code == 200
    assert r.json()["user_files"][0]["shared_with"] == []


def test_get_files_shared_with_detail_keeps_lapsed_recipients(client, patch_contract, monkeypatch):
    # The owner's "Shared with others" view needs people whose access ran out,
    # which shared_with deliberately omits. Anyone still in getSharedUsers with
    # zero permissions has lapsed -- revoke would have removed them outright.
    from web3 import Web3
    owner_cs = Web3.to_checksum_address(OWNER)
    patch_contract(
        user_files=[("cid1", "a.txt", "txt", 111)],
        owners={"cid1": owner_cs},
        shared={"cid1": [OTHER, WALLET]},
        permissions={("cid1", OTHER): READ},   # WALLET has lapsed
    )
    monkeypatch.setattr(files_mod, "get_file_size", lambda cid: 1)

    r = client.get("/", params={"user_address": OWNER})
    rec = r.json()["user_files"][0]
    assert rec["shared_with"] == [OTHER]            # unchanged shape, active only
    assert rec["shared_with_detail"] == [
        {"address": OTHER, "lapsed": False},
        {"address": WALLET, "lapsed": True},
    ]


def test_get_files_empty(client, patch_contract):
    patch_contract(user_files=[])
    r = client.get("/", params={"user_address": OWNER})
    assert r.status_code == 200
    assert r.json() == {"user_files": []}


class _Resp:
    def __init__(self, ok, payload):
        self.ok = ok
        self.status_code = 200 if ok else 500
        self._payload = payload

    def json(self):
        return self._payload


def test_storage_stats_reports_only_quota_fields(client, monkeypatch):
    monkeypatch.setattr(files_mod.requests, "post",
                        lambda *a, **k: _Resp(True, {"RepoSize": 100, "StorageMax": 1000}))
    r = client.get("/storage-stats")
    assert r.status_code == 200
    body = r.json()
    assert body["ipfs_storage_max"] == 1000
    assert body["disk_total"] > 0
    # Node-wide usage numbers stay private: they would reveal server capacity
    # and other users' aggregate usage.
    assert "ipfs_repo_size" not in body
    assert "disk_free" not in body


def test_storage_stats_survives_ipfs_failure(client, monkeypatch):
    def boom(*a, **k):
        raise Exception("ipfs down")
    monkeypatch.setattr(files_mod.requests, "post", boom)
    r = client.get("/storage-stats")
    assert r.status_code == 200
    # IPFS key absent, but the disk-size fallback still present
    assert "ipfs_storage_max" not in r.json()
    assert r.json()["disk_total"] > 0


def test_get_files_does_not_call_a_file_expired_when_the_owner_lookup_failed(client, patch_contract, monkeypatch):
    # Both getFileOwner attempts failing leaves owner unknown, and getPermissions
    # falls back to 0 on its own failure -- which together look exactly like an
    # expired share. Claiming "Access expired" on an RPC blip is a confident lie.
    from types import SimpleNamespace
    fake = patch_contract(user_files=[("cid1", "a.txt", "txt", 111)])

    def boom(cid):
        return SimpleNamespace(call=lambda: (_ for _ in ()).throw(Exception("rpc down")))
    monkeypatch.setattr(fake, "getFileOwner", boom)
    monkeypatch.setattr(files_mod, "get_file_size", lambda cid: 1)

    r = client.get("/", params={"user_address": OWNER})
    assert r.status_code == 200
    assert r.json()["user_files"][0]["is_expired"] is False
