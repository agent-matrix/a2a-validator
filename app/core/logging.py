# app/core/logging.py
from __future__ import annotations

import logging
import os
import uuid
from typing import Optional

_DEF_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DEF_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


def setup_logging(level: Optional[str] = None) -> None:
    """
    Idempotent logging setup.
    - Honors LOG_LEVEL env (default INFO) unless an explicit level is passed.
    - Avoids duplicate handlers if called multiple times.
    - Tames noisy third-party loggers by default.
    """
    root = logging.getLogger()
    if root.handlers:
        return  # already configured

    log_level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    try:
        parsed_level = getattr(logging, log_level)
    except AttributeError:
        parsed_level = logging.INFO

    handler = logging.StreamHandler()
    formatter = logging.Formatter(_DEF_FORMAT, datefmt=_DEF_DATEFMT)
    handler.setFormatter(formatter)

    root.setLevel(parsed_level)
    root.addHandler(handler)

    # Quiet noisy libs by default; adjust if you need more/less detail.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def add_trace_id(request) -> None:
    """
    Injects a unique `trace_id` into request.state (works with FastAPI-style objects).
    Duck-typed to avoid importing FastAPI here.
    """
    try:
        state = getattr(request, "state", None)
        if state is None:
            # Some frameworks may not have .state; just skip silently.
            return
        if not hasattr(state, "trace_id"):
            state.trace_id = str(uuid.uuid4())
    except Exception:
        # Never let logging helpers break the app.
        return
