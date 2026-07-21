import logging
import os

# One-time logging setup for the backend. Level is LOG_LEVEL from the
# environment (default INFO); set LOG_LEVEL=DEBUG for per-file upload detail.
# Modules obtain their logger with logging.getLogger(__name__).
_configured = False


def setup_logging():
    global _configured
    if _configured:
        return
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _configured = True
