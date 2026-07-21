import os
import logging
from logging.handlers import RotatingFileHandler

# One-time logging setup for the backend. Emits to the console (stderr) and,
# unless disabled, to a size-capped rotating file so logs survive however the
# server is launched (nohup, systemd, container). Modules obtain their logger
# with logging.getLogger(__name__).
#
# Environment:
#   LOG_LEVEL  logging level (default INFO); set DEBUG for per-file upload detail
#   LOG_DIR    directory for the log file, relative to the backend root unless
#              absolute (default "logs"); set empty to disable the file handler
#   LOG_FILE   log file name within LOG_DIR (default "web3fs.log")
_configured = False

# backend root = parent of the app/ package this file lives in
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
BACKUP_COUNT = 5             # keep 5 rotations -> ~25 MB ceiling


def setup_logging():
    global _configured
    if _configured:
        return

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    log_dir = os.getenv("LOG_DIR", "logs")
    if log_dir:  # empty LOG_DIR -> console only
        if not os.path.isabs(log_dir):
            log_dir = os.path.join(_BACKEND_ROOT, log_dir)
        try:
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, os.getenv("LOG_FILE", "web3fs.log"))
            file_handler = RotatingFileHandler(
                log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as e:
            # A read-only or missing volume shouldn't take the server down;
            # console logging still works.
            root.warning("File logging disabled (%s): %s", log_dir, e)

    _configured = True
