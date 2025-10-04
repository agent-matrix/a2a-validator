# app/middleware.py
from __future__ import annotations

import time
import logging
import json
import asyncio  
from typing import Callable, Optional

from anyio import EndOfStream
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response, JSONResponse
from starlette.middleware.gzip import GZipMiddleware
from starlette.exceptions import ClientDisconnect 

# Optional: python-json-logger for structured logs; fallback to a minimal JSON formatter.
try:
    from pythonjsonlogger import jsonlogger  # type: ignore
    _HAS_PY_JSON_LOGGER = True
except Exception:
    _HAS_PY_JSON_LOGGER = False

from .deps import get_settings
from .core.rate_limit import RateLimiter
from .core.logging import add_trace_id

class _SimpleJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "asctime": self.formatTime(record, "%Y-%m-%d %H:%M:%S"),
            "name": record.name,
            "levelname": record.levelname,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", None),
        }
        try:
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return (
                f'{payload["asctime"]} {payload["name"]} {payload["levelname"]} '
                f'{payload["message"]} trace_id={payload["trace_id"]}'
            )

_logger = logging.getLogger("matrix-ai")
if not _logger.handlers:
    _logger.setLevel(logging.INFO)
    _handler = logging.StreamHandler()
    if _HAS_PY_JSON_LOGGER:
        _formatter = jsonlogger.JsonFormatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s %(trace_id)s"
        )
    else:
        _formatter = _SimpleJsonFormatter()
        logging.getLogger("uvicorn.error").warning(
            "python-json-logger not found; using a minimal JSON formatter."
        )
    _handler.setFormatter(_formatter)
    _logger.addHandler(_handler)

_rate_limiter = RateLimiter()
_SSE_PATH_SUFFIXES = ("/chat/stream", "/v1/chat/stream")
_HEALTH_PATHS = ("/health", "/livez", "/readyz")

def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def _is_sse(request: Request, response: Optional[Response] = None) -> bool:
    path = request.url.path
    if path.endswith(_SSE_PATH_SUFFIXES):
        return True
    if response is not None:
        ctype = response.headers.get("content-type", "")
        if ctype.startswith("text/event-stream"):
            return True
    accept = request.headers.get("accept", "")
    return "text/event-stream" in accept

def attach_middlewares(app: FastAPI) -> None:
    app.add_middleware(GZipMiddleware, minimum_size=512)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Trace-Id", "X-Process-Time-Ms", "Server-Timing"],
    )

    @app.middleware("http")
    async def rate_limit_and_log_middleware(request: Request, call_next: Callable):
        add_trace_id(request)
        trace_id = getattr(request.state, "trace_id", "N/A")

        path = request.url.path
        method = request.method
        ua = request.headers.get("user-agent", "-")
        ip = _client_ip(request)

        if path in _HEALTH_PATHS:
            try:
                response = await call_next(request)
            except Exception:
                return JSONResponse({"status": "unhealthy"}, status_code=500)
            response.headers.setdefault("X-Trace-Id", str(trace_id))
            return response

        settings = get_settings()
        if not _rate_limiter.allow(ip, path, settings.limits.rate_per_min):
            _logger.warning(
                "429 Too Many Requests from %s on %s",
                ip, path, extra={"trace_id": trace_id},
            )
            return JSONResponse({"detail": "Too Many Requests"}, status_code=429,
                                headers={"X-Trace-Id": str(trace_id)})

        t0 = time.time()
        try:
            response = await call_next(request)

        # --- NEW: treat disconnects as benign (return 204) ---
        except (EndOfStream, ClientDisconnect, asyncio.CancelledError):
            _logger.info(
                "Client disconnected from stream. Path: %s, IP: %s",
                path, ip, extra={"trace_id": trace_id},
            )
            resp = Response(status_code=204)
            resp.headers.setdefault("X-Trace-Id", str(trace_id))
            return resp

        except RuntimeError as e:
            # Starlette sometimes wraps EndOfStream as this RuntimeError
            if str(e) == "No response returned.":
                _logger.info(
                    "Downstream produced no response (likely streaming disconnect). "
                    "Path: %s, IP: %s",
                    path, ip, extra={"trace_id": trace_id},
                )
                resp = Response(status_code=204)
                resp.headers.setdefault("X-Trace-Id", str(trace_id))
                return resp
            # not a disconnect case → re-raise to be handled below
            raise

        except Exception as e:
            _logger.exception(
                "Unhandled error while processing %s %s: %s",
                method, path, e, extra={"trace_id": trace_id},
            )
            dur_ms = (time.time() - t0) * 1000.0
            return JSONResponse(
                {"detail": "Internal Server Error"},
                status_code=500,
                headers={
                    "X-Trace-Id": str(trace_id),
                    "X-Process-Time-Ms": f"{dur_ms:.2f}",
                    "Server-Timing": f"app;dur={dur_ms:.2f}",
                },
            )

        if not isinstance(response, Response):
            _logger.error("Downstream returned no Response object for %s",
                          path, extra={"trace_id": trace_id})
            return JSONResponse({"detail": "Internal Server Error"},
                                status_code=500,
                                headers={"X-Trace-Id": str(trace_id)})

        sse = _is_sse(request, response)
        dur_ms = (time.time() - t0) * 1000.0
        response.headers.setdefault("X-Trace-Id", str(trace_id))
        response.headers.setdefault("X-Process-Time-Ms", f"{dur_ms:.2f}")
        response.headers.setdefault("Server-Timing", f"app;dur={dur_ms:.2f}")

        if sse:
            response.headers.setdefault("Cache-Control", "no-cache")
            _logger.info(
                '"%s %s" %s (SSE) ip=%s ua="%s" %.2fms',
                method, path, response.status_code, ip, ua, dur_ms,
                extra={"trace_id": trace_id},
            )
            return response

        _logger.info(
            '"%s %s" %s ip=%s ua="%s" %.2fms',
            method, path, response.status_code, ip, ua, dur_ms,
            extra={"trace_id": trace_id},
        )
        return response
