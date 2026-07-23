"""CORS policy: only the configured frontend origins may call the API from a
browser, and credentials mode stays off (auth is a header, not a cookie)."""


def _preflight(client, origin):
    return client.options(
        "/storage-stats",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-auth-token",
        },
    )


def test_allowed_origin_gets_cors_headers(client):
    res = _preflight(client, "https://fs.web3db.org")
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == "https://fs.web3db.org"


def test_localhost_dev_origin_allowed(client):
    res = _preflight(client, "http://localhost:3000")
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_unknown_origin_is_refused(client):
    res = _preflight(client, "https://evil.example.com")
    assert "access-control-allow-origin" not in res.headers


def test_credentials_mode_is_off(client):
    res = _preflight(client, "https://fs.web3db.org")
    # Absent header means the browser will not send cookies; a wildcard origin
    # combined with credentials would be rejected outright.
    assert "access-control-allow-credentials" not in res.headers
