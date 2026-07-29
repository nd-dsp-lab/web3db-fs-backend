"""The access log: every request produces a line, and no line names a file,
a CID or a wallet.

The log directory is a plain host mount, so what these lines contain is what
a host administrator can read.
"""
import logging

import pytest

import logredact

CID = "QmRBa64fgpr5EmVyq5D78mbvPj2SwUfRwgEQYiv2VYy6a1"
ADDR = "0x1A28b19f6d2ea1A05F9eFFbcCcbF7E9571877981"


@pytest.fixture
def access_lines(caplog):
    caplog.set_level(logging.INFO, logger="web3fs.access")
    return caplog


def test_every_request_is_logged_with_its_outcome(client, access_lines):
    client.get("/storage-stats")

    lines = [r.getMessage() for r in access_lines.records if r.name == "web3fs.access"]
    assert len(lines) == 1
    assert lines[0].startswith("GET /storage-stats -> ")
    assert "ms" in lines[0], "timing is the point of an access log"


def test_a_failing_request_is_logged_too(client, access_lines):
    # no auth token, so the route refuses before doing any work
    client.get(f"/download/{CID}/paper.pdf")

    lines = [r.getMessage() for r in access_lines.records if r.name == "web3fs.access"]
    assert len(lines) == 1
    assert "-> 401" in lines[0]


def test_the_request_line_names_no_file_and_no_cid(client, access_lines):
    client.get(f"/download/{CID}/usenixsecurity25-shafran.pdf")

    line = next(r.getMessage() for r in access_lines.records if r.name == "web3fs.access")
    assert CID not in line
    assert "usenix" not in line and "shafran" not in line
    # ...but the tag is there, so the line is still traceable
    assert logredact.cid(CID) in line


def test_the_request_line_names_no_wallet(client, access_lines):
    # any route will do — the redaction happens before dispatch, and a 404
    # keeps the test off the network
    client.get(f"/no-such-route/{ADDR}")

    line = next(r.getMessage() for r in access_lines.records if r.name == "web3fs.access")
    assert ADDR not in line and ADDR.lower() not in line.lower()
    assert logredact.addr(ADDR) in line


def test_a_5xx_is_logged_at_error_level(client, access_lines, monkeypatch):
    import server

    @server.app.get("/boom-500")
    def boom():
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={})

    client.get("/boom-500")

    record = next(r for r in access_lines.records if r.name == "web3fs.access")
    assert record.levelno == logging.ERROR, "server-side failures must stand out from routine traffic"


def test_an_unhandled_exception_is_logged_with_its_traceback(client, access_lines):
    import server

    @server.app.get("/boom-raise")
    def boom():
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError):
        client.get("/boom-raise")

    record = next(r for r in access_lines.records if r.name == "web3fs.access")
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None, "an unhandled error is only useful with its traceback"
    assert "unhandled exception" in record.getMessage()
