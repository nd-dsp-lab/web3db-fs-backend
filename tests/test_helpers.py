"""Transaction building in helpers.py.

This is the layer closest to real funds and real on-chain state: a wrong nonce
offset, a dropped cid in a batch, or a cid/path pair that drifts out of
alignment all produce a transaction the user signs without seeing. The route
tests stub these functions out, so everything here is asserted directly on the
transaction dicts they return.
"""
import pytest
from web3 import Web3

import helpers
from permissions import READ, DOWNLOAD

OWNER = "0x1A28b19f6d2ea1A05F9eFFbcCcbF7E9571877981"
OWNER_LOWER = OWNER.lower()
OTHER = "0x3081Acc05169336e7875ad9f896bF6511397809a"
RECIPIENT = "0x6e02F541dd762E5077e6d619AA2F2d81371AcABE"
SEPOLIA = 11155111


# --- shared transaction scaffolding ---

def test_gas_price_carries_25_percent_margin(patch_helpers):
    # The node's quote lags the network, which left transactions stuck in the
    # mempool; the margin also lets a retry replace a stuck tx at the same nonce.
    patch_helpers(gas_price=1_000_000_000)
    assert helpers._gas_price() == 1_250_000_000


def test_base_tx_fields(patch_helpers):
    patch_helpers(nonce=7)
    tx = helpers._base_tx(OWNER_LOWER)
    assert tx["chainId"] == SEPOLIA
    assert tx["nonce"] == 7
    assert tx["from"] == OWNER, "address must be checksummed for web3"
    assert tx["gasPrice"] == 1_250_000_000


def test_base_tx_nonce_offset_stacks_unbroadcast_txs(patch_helpers):
    # The chain nonce does not advance until a tx is mined, so batch endpoints
    # have to offset manually or every tx in the group reuses one nonce.
    patch_helpers(nonce=7)
    assert helpers._base_tx(OWNER, nonce_offset=2)["nonce"] == 9


def test_base_tx_can_omit_gas_price(patch_helpers):
    patch_helpers()
    assert "gasPrice" not in helpers._base_tx(OWNER, with_gas_price=False)


def test_assert_owner_rejects_non_owner(patch_helpers):
    patch_helpers(owners={"cidA": OTHER})
    with pytest.raises(Exception, match="Only file owner can move file."):
        helpers._assert_owner("cidA", OWNER, "move")


def test_assert_owner_is_case_insensitive(patch_helpers):
    patch_helpers(owners={"cidA": OWNER})
    helpers._assert_owner("cidA", OWNER_LOWER, "move")  # must not raise


def test_owned_only_filters_to_caller(patch_helpers):
    patch_helpers(owners={"a": OWNER, "b": OTHER, "c": OWNER})
    assert helpers._owned_only(["a", "b", "c"], OWNER) == ["a", "c"]


# --- single-file transactions ---

def test_upload_tx_carries_cid_path_and_format(patch_helpers):
    patch_helpers()
    tx = helpers.prepare_upload_transaction("cidA", "docs/a.pdf", OWNER, "pdf")
    assert tx["_fn"] == "uploadFile"
    assert tx["_args"] == ("cidA", "docs/a.pdf", "pdf")
    assert tx["nonce"] == 7


def test_upload_tx_sends_empty_string_for_missing_format(patch_helpers):
    # The contract signature takes a string; None would raise on encoding.
    patch_helpers()
    tx = helpers.prepare_upload_transaction("cidA", "a", OWNER, None)
    assert tx["_args"] == ("cidA", "a", "")


def test_share_tx_grants_read_and_download_only(patch_helpers):
    patch_helpers(owners={"cidA": OWNER})
    tx = helpers.prepare_share_transaction("cidA", RECIPIENT, OWNER)
    assert tx["_fn"] == "grant"
    assert tx["_args"] == ("cidA", Web3.to_checksum_address(RECIPIENT), READ | DOWNLOAD)
    assert READ | DOWNLOAD == 5, "sharing must never hand out write/delete/share bits"


def test_share_tx_refuses_non_owner(patch_helpers):
    patch_helpers(owners={"cidA": OTHER})
    with pytest.raises(Exception, match="share"):
        helpers.prepare_share_transaction("cidA", RECIPIENT, OWNER)


def test_unshare_tx_revokes_the_same_mask_it_granted(patch_helpers):
    patch_helpers(owners={"cidA": OWNER})
    tx = helpers.prepare_unshare_transaction("cidA", RECIPIENT, OWNER)
    assert tx["_fn"] == "revoke"
    assert tx["_args"][2] == READ | DOWNLOAD


def test_move_tx_refuses_non_owner(patch_helpers):
    patch_helpers(owners={"cidA": OTHER})
    with pytest.raises(Exception, match="Only file owner can move file."):
        helpers.prepare_move_transaction("cidA", "/b/x", OWNER)


def test_delete_tx_targets_the_cid(patch_helpers):
    patch_helpers()
    tx = helpers.prepare_delete_transaction("cidA", OWNER)
    assert (tx["_fn"], tx["_args"]) == ("deleteFile", ("cidA",))


# --- batch transactions ---

def test_share_batch_filters_to_owned_and_adds_gas_margin(patch_helpers):
    contract, _ = patch_helpers(owners={"a": OWNER, "b": OTHER}, gas_estimate=100_000)
    tx, count = helpers.prepare_share_batch_transaction(["a", "b"], RECIPIENT, OWNER)
    assert count == 1
    assert tx["_args"][0] == ["a"], "must not try to grant a file the caller doesn't own"
    assert tx["gas"] == 110_000, "10% margin over the estimate"
    # gas must be estimated for exactly the call being built, not a stale one
    assert contract.estimates[0][0] == "grantFiles"


def test_share_batch_returns_none_when_nothing_owned(patch_helpers):
    contract, _ = patch_helpers(owners={"a": OTHER})
    assert helpers.prepare_share_batch_transaction(["a"], RECIPIENT, OWNER) == (None, 0)
    assert contract.built == [], "must not touch the chain when there is nothing to do"


def test_unshare_batch_filters_to_owned(patch_helpers):
    patch_helpers(owners={"a": OWNER, "b": OTHER})
    tx, count = helpers.prepare_unshare_batch_transaction(["a", "b"], RECIPIENT, OWNER)
    assert (tx["_fn"], tx["_args"][0], count) == ("revokeFiles", ["a"], 1)


def test_upload_batch_passes_all_three_lists_aligned(patch_helpers):
    patch_helpers()
    tx = helpers.prepare_upload_batch_transaction(
        ["c1", "c2"], ["docs/a", "docs/b"], ["pdf", "txt"], OWNER)
    assert tx["_fn"] == "uploadFiles"
    assert tx["_args"] == (["c1", "c2"], ["docs/a", "docs/b"], ["pdf", "txt"])
    assert tx["gas"] == 110_000


def test_move_batch_keeps_cid_and_path_pairs_aligned(patch_helpers):
    # Regression risk: filtering the two lists independently would silently
    # move a file to another file's destination path.
    patch_helpers(owners={"a": OWNER, "b": OTHER, "c": OWNER})
    tx, count = helpers.prepare_move_batch_transaction(
        ["a", "b", "c"], ["/x/a", "/x/b", "/x/c"], OWNER)
    assert count == 2
    assert tx["_args"] == (["a", "c"], ["/x/a", "/x/c"])


def test_move_batch_returns_none_when_nothing_owned(patch_helpers):
    patch_helpers(owners={"a": OTHER})
    assert helpers.prepare_move_batch_transaction(["a"], ["/x"], OWNER) == (None, 0)


def test_delete_folder_skips_the_chain_when_empty(patch_helpers):
    contract, eth = patch_helpers()
    assert helpers.prepare_delete_folder([], OWNER) is None
    assert eth.nonce_queries == [], "no RPC call at all for an empty folder"


def test_delete_folder_leaves_gas_price_to_web3(patch_helpers):
    # Intentional: gas is estimated here, and the manual 25% margin is skipped
    # so web3 fills gasPrice itself after estimation.
    patch_helpers()
    tx = helpers.prepare_delete_folder(["a", "b"], OWNER)
    assert tx["_fn"] == "cleanFolder"
    assert "gasPrice" not in tx
    assert tx["gas"] == 110_000


# --- inherited grants (nonce sequencing) ---

def test_inherited_grants_are_nonce_offset_behind_the_upload(patch_helpers):
    # The files don't exist on-chain yet, so each grant must mine *after* the
    # upload tx that creates them — enforced purely by nonce ordering.
    patch_helpers(nonce=7)
    txns = helpers.prepare_inherited_grant_transactions(["c1"], [RECIPIENT, OTHER], OWNER)
    assert [t["nonce"] for t in txns] == [8, 9]
    assert all(t["chainId"] == SEPOLIA for t in txns)
    assert all(t["gasPrice"] == 1_250_000_000 for t in txns)


def test_inherited_grants_one_tx_per_recipient_over_all_cids(patch_helpers):
    patch_helpers()
    txns = helpers.prepare_inherited_grant_transactions(["c1", "c2"], [RECIPIENT], OWNER)
    assert len(txns) == 1
    assert txns[0]["_args"][0] == ["c1", "c2"]
    assert txns[0]["_args"][2] == READ | DOWNLOAD


def test_inherited_grants_set_gas_manually(patch_helpers):
    # estimate_gas would revert here: the caller isn't the owner yet.
    contract, _ = patch_helpers()
    txns = helpers.prepare_inherited_grant_transactions(["c1", "c2"], [RECIPIENT], OWNER)
    assert txns[0]["gas"] == 150_000 * 2 + 100_000
    assert contract.estimates == [], "must never estimate gas for a file that doesn't exist yet"


def test_inherited_grants_empty_recipients_is_no_op(patch_helpers):
    patch_helpers()
    assert helpers.prepare_inherited_grant_transactions(["c1"], [], OWNER) == []


# --- folder share set (inheritance walk) ---

def test_folder_share_set_intersects_recipients_across_files(patch_helpers):
    # A folder is "shared with" only the users who can see *every* file in it.
    patch_helpers(
        owners={"c1": OWNER, "c2": OWNER},
        shared={"c1": [RECIPIENT, OTHER], "c2": [RECIPIENT]},
        user_files=[("c1", "docs/a", "txt", 1), ("c2", "docs/b", "txt", 2)])
    assert helpers.folder_share_set(OWNER, "docs") == [RECIPIENT]


def test_folder_share_set_walks_up_to_parent_when_folder_is_empty(patch_helpers):
    # A freshly created subfolder has no files, so it inherits the parent's set.
    patch_helpers(
        owners={"c1": OWNER},
        shared={"c1": [RECIPIENT]},
        user_files=[("c1", "docs/a", "txt", 1)])
    assert helpers.folder_share_set(OWNER, "docs/newsub") == [RECIPIENT]


def test_folder_share_set_root_inherits_nothing(patch_helpers):
    patch_helpers(
        owners={"c1": OWNER}, shared={"c1": [RECIPIENT]},
        user_files=[("c1", "a.txt", "txt", 1)])
    assert helpers.folder_share_set(OWNER, "/") == []


def test_folder_share_set_ignores_trash(patch_helpers):
    patch_helpers(
        owners={"c1": OWNER}, shared={"c1": [RECIPIENT]},
        user_files=[("c1", ".trash/docs/a", "txt", 1)])
    assert helpers.folder_share_set(OWNER, "docs") == []


def test_folder_share_set_ignores_files_shared_to_the_caller(patch_helpers):
    # getUserFiles also returns files shared *to* this user; those must not
    # define what the caller's own folder is shared with.
    patch_helpers(
        owners={"mine": OWNER, "theirs": OTHER},
        shared={"mine": [RECIPIENT], "theirs": [OTHER]},
        user_files=[("mine", "docs/a", "txt", 1), ("theirs", "docs/b", "txt", 2)])
    assert helpers.folder_share_set(OWNER, "docs") == [RECIPIENT]


def test_folder_share_set_unshared_folder_is_empty(patch_helpers):
    patch_helpers(owners={"c1": OWNER}, shared={"c1": []},
                  user_files=[("c1", "docs/a", "txt", 1)])
    assert helpers.folder_share_set(OWNER, "docs") == []


# --- unpin ---

def test_unpin_cid_reports_both_steps(patch_helpers, monkeypatch):
    calls = []

    class R:
        def __init__(self, ok=True):
            self.status_code = 200 if ok else 500
            self.text = "ok"
            self.ok = ok

    def fake_post(url, **kw):
        calls.append(url)
        return R()

    monkeypatch.setattr(helpers, "requests", type("M", (), {"post": staticmethod(fake_post)}))
    result = helpers.unpin_cid("cidA")

    assert result["unpin_ok"] and result["gc_ok"]
    assert any("/pin/rm" in u for u in calls) and any("/repo/gc" in u for u in calls)


def test_unpin_cid_swallows_errors(patch_helpers, monkeypatch):
    # Unpinning is best-effort cleanup after a confirmed on-chain delete; it
    # must never turn a successful delete into a failed request.
    def boom(url, **kw):
        raise RuntimeError("node down")

    monkeypatch.setattr(helpers, "requests", type("M", (), {"post": staticmethod(boom)}))
    result = helpers.unpin_cid("cidA")

    assert result["unpin_ok"] is False
    assert "node down" in result["error"]
