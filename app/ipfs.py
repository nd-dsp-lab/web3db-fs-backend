"""The node calls the upload paths share.

Both /upload and /upload-folder add unpinned, check for a duplicate, then pin —
but they used to do it with their own copies of the HTTP calls, which drifted:
one had a timeout and checked the pin result, the other did neither. One
implementation each, so they cannot drift again.
"""
import json
import logging

import requests

from configure import IPFS_API_URL

logger = logging.getLogger(__name__)

# Every call to the node is bounded: an unbounded one holds the request open
# for as long as the node is wedged. Pinning is the slower of the two — it can
# wait on the DHT — so it gets the longer budget.
ADD_TIMEOUT = 10
PIN_TIMEOUT = 30


def add_unpinned(filename: str, data: bytes) -> str:
    """Add bytes to IPFS without pinning and return the CID.

    Adding under a name containing a slash makes kubo build a wrapper directory
    and emit its entry *last*, so callers must pass a leaf name — the wrapper's
    CID resolves to a directory listing, not the file.
    """
    resp = requests.post(
        f"{IPFS_API_URL}/add?pin=false",
        files={"file": (filename, data)},
        timeout=ADD_TIMEOUT,
    )
    resp.raise_for_status()
    # One line per added object; a leaf name yields exactly one.
    first = resp.text.strip().splitlines()[0]
    return json.loads(first)["Hash"]


def pin(cid: str):
    """Pin a CID, raising if the node refuses.

    Callers must not register an unpinned CID on-chain: the entry would outlive
    the bytes, resolving to nothing after the next garbage collection.
    """
    resp = requests.post(f"{IPFS_API_URL}/pin/add?arg={cid}", timeout=PIN_TIMEOUT)
    resp.raise_for_status()
    return resp
