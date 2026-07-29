import logging
import os
import time

import uvicorn
from fastapi import FastAPI, Request

import logredact
from configure import configure_app
from routers.attestation import router as attestation_router
from routers.auth import router as auth_router
from routers.sharing import router as sharing_router
from routers.file_ops import router as file_ops_router
from routers.upload import router as upload_router
from routers.download import router as download_router
from routers.files import router as files_router
from routers.wallet import router as wallet_router

# This will be a simple fastAPI server that acts as an sgx node
app = FastAPI()
configure_app(app)  # CORS + other startup steps

access_logger = logging.getLogger("web3fs.access")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """One line per request, and the only place unhandled errors are recorded.

    This replaces uvicorn's access log, which prints raw paths — and those
    carry the filenames and CIDs that logredact exists to keep off the host's
    disk. Uvicorn's own access log is disabled where the server is launched.
    """
    started = time.perf_counter()
    where = logredact.path(request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        # An exception escaping the app would otherwise reach only uvicorn's
        # error logger; log it here so the failing request is identified, then
        # re-raise and let the framework return its 500.
        elapsed = (time.perf_counter() - started) * 1000
        access_logger.exception("%s %s -> unhandled exception in %.0fms",
                                request.method, where, elapsed)
        raise

    elapsed = (time.perf_counter() - started) * 1000
    # 5xx is ours, 4xx is usually the caller's; anything slow is worth seeing.
    level = logging.ERROR if response.status_code >= 500 else logging.INFO
    access_logger.log(level, "%s %s -> %d in %.0fms",
                      request.method, where, response.status_code, elapsed)
    return response

app.include_router(attestation_router)
app.include_router(auth_router)
app.include_router(sharing_router)
app.include_router(file_ops_router)
app.include_router(upload_router)
app.include_router(download_router)
app.include_router(files_router)
app.include_router(wallet_router)

if __name__ == "__main__":
    # Auto-reload is a development convenience and is off unless asked for:
    # it runs a supervisor that polls every .py file and restarts the worker
    # on any change, so a deploy that rewrites files one at a time can
    # restart the server mid-pull, dropping in-flight requests. Run
    # `UVICORN_RELOAD=1 python3 server.py` while developing.
    reload = os.getenv("UVICORN_RELOAD", "").strip().lower() in ("1", "true", "yes")

    # log_config=None: don't let uvicorn install its own handlers. Its error
    # logger then propagates to the root logger configured in logging_config,
    # so everything lands in logs/web3fs.log in one format.
    #
    # access_log=False: the log_requests middleware above covers every request
    # already, and uvicorn's version would print unredacted paths beside it.
    uvicorn.run("server:app", host="0.0.0.0", port=8090, reload=reload,
                log_config=None, access_log=False)
