"""Guard against blocking the event loop.

FastAPI runs a `def` handler in a threadpool but an `async def` handler on
the single event-loop thread. Every blocking call this app makes — the IPFS
API via requests, contract reads via web3, wait_for_transaction_receipt — is
synchronous, so putting one inside an `async def` stalls every other request
for its whole duration. verify_upload used to hold the loop for up to 120
seconds after every single transaction.

An `async def` handler is only correct here if it actually awaits something.
"""
import inspect

from server import app


def _handlers():
    """Our own route handlers only — FastAPI's built-in /docs and
    /openapi.json endpoints are async without awaiting and are not ours."""
    for route in app.routes:
        fn = getattr(route, "endpoint", None)
        if fn is not None and getattr(fn, "__module__", "").startswith("routers"):
            yield route.path, fn


def test_every_async_handler_actually_awaits():
    offenders = []
    for path, fn in _handlers():
        if not inspect.iscoroutinefunction(fn):
            continue
        source = inspect.getsource(fn)
        if "await " not in source:
            offenders.append(f"{fn.__name__} ({path})")

    assert offenders == [], (
        "these handlers are async but never await, so they block the event "
        f"loop for every user: {offenders}. Drop the `async` keyword and "
        "FastAPI will run them in a threadpool instead."
    )


def test_blocking_handlers_are_synchronous():
    """The specific handlers whose blocking work is longest — a Sepolia
    confirmation, or one network round trip per file in the listing."""
    from routers.upload import verify_upload
    from routers.wallet import fund_wallet
    from routers.files import get_files

    for fn in (verify_upload, fund_wallet, get_files):
        assert not inspect.iscoroutinefunction(fn), (
            f"{fn.__name__} blocks on network I/O; as `async def` it freezes "
            "the whole server while it waits"
        )
