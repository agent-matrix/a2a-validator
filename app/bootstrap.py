# app/bootstrap.py
"""
App bootstrap: load .env and configure logging as early as possible.
This module should be imported once at process start (import side-effects).
"""
from __future__ import annotations

import os
from dotenv import load_dotenv

# Load environment from configs/.env if present (non-fatal if missing)
load_dotenv(dotenv_path=os.path.join("configs", ".env"))

# Configure logging after env is loaded so LOG_LEVEL is respected
try:
    from app.core.logging import setup_logging  # noqa: E402
    setup_logging()
except Exception as e:
    # Fallback to a minimal logger if our setup helper isn't available for any reason
    import logging as _logging
    _logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    _logging.getLogger(__name__).warning("Fallback logging configured: %s", e)
