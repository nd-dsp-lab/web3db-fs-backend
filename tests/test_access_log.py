"""The access log: every request produces a line, and a request that blows up
produces one naming the route.

Uvicorn's own access log is switched off, so if the middleware stops emitting,
nothing else covers it.
"""
import logging

import pytest

CID = "QmRBa64fgpr5EmVyq5D78mbvPj2SwUfRwgEQYiv2VYy6a1"


@pytest.fixture
def access_lines(caplog):
    caplog.set_level(logging.INFO, logger="web3fs.access")
    return caplog


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "web3fs.access"]


def test_every_request_is_logged_with_its_outcome(client, access_lines):
    client.get("/storage-stats")

    lines = _lines(access_lines)
    assert len(lines) == 1
    assert lines[0].startswith("GET /storage-stats -> ")
    assert "ms" in lines[0], "timing is the point of an access log"


def test_a_failing_request_is_logged_too(client, access_lines):
    # no auth token, so the route refuses before doing any work
    client.get(f"/download/{CID}/paper.pdf")

    lines = _lines(access_lines)
    assert len(lines) == 1
    assert "-> 401" in lines[0]


def test_the_line_carries_the_full_path_for_debugging(client, access_lines):
    client.get(f"/download/{CID}/usenixsecurity25-shafran.pdf")

    line = _lines(access_lines)[0]
    assert CID in line
    assert "usenixsecurity25-shafran.pdf" in line


def test_a_5xx_is_logged_at_error_level(client, access_lines):
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
    assert "/boom-raise" in record.getMessage(), "the traceback alone does not say which route failed"
