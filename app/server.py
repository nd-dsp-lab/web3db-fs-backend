import os

import uvicorn
from fastapi import FastAPI

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

    # log_config=None: don't let uvicorn install its own handlers. Its
    # access/error loggers then propagate to the root logger configured in
    # logging_config, so request lines land in logs/web3fs.log in our format
    # instead of a separate stream.
    uvicorn.run("server:app", host="0.0.0.0", port=8090, reload=reload, log_config=None)
