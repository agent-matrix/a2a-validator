# app/main.py
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, APIRouter, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ---- Early env load (HF_TOKEN, ADMIN_TOKEN, GITHUB_TOKEN, etc.) ----
def _load_env_file(paths: list[str]) -> None:
    logger = logging.getLogger("uvicorn.error")
    try:
        from dotenv import load_dotenv  # type: ignore
        for p in paths:
            if os.path.exists(p):
                load_dotenv(dotenv_path=p, override=False)
                logger.info("Loaded environment from %s", p)
                return
        logger.info("No .env file found in %s (skipping)", paths)
    except Exception:
        # Fallback, very small .env parser
        for p in paths:
            if not os.path.exists(p):
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for raw in f:
                        line = raw.strip()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("export "):
                            line = line[len("export "):].strip()
                        if "=" not in line:
                            continue
                        key, val = line.split("=", 1)
                        key, val = key.strip(), val.strip()
                        if (val.startswith('"') and val.endswith('"')) or (
                            val.startswith("'") and val.endswith("'")
                        ):
                            val = val[1:-1]
                        os.environ.setdefault(key, val)
                logger.info("Loaded environment from %s (fallback parser)", p)
                return
            except Exception as e:
                logger.warning("Failed loading env from %s: %s", p, e)
        logger.info("No .env loaded (none found / parsers failed)")

_load_env_file([".env", "configs/.env", ".env.local", "configs/.env.local"])

# ---- RAG DISABLED (commented out while debugging) ----
# from .deps import get_settings
# from .services.chat_service import get_retriever
# from .core.rag.build import ensure_kb

# ---- Middlewares ----
try:
    from .middleware import attach_middlewares  # type: ignore
except Exception:
    try:
        from .middlewares import attach_middlewares  # type: ignore
    except Exception:
        def attach_middlewares(app: FastAPI) -> None:
            logging.getLogger("uvicorn.error").warning(
                "attach_middlewares not found; continuing without custom middlewares."
            )

# ---- Routers enabled ----
from .routers import health
from .ui import router as ui_router  # <-- mount UI so /home works

# ---- Validator service integration ----
VALIDATOR_TAG = {"name": "Validator", "description": "A2A Validator UI and endpoints (/validator)."}

HAS_VALIDATOR = False
HAS_SOCKETIO = False
socketio_app = None  # type: ignore[assignment]

try:
    # Primary validator router + optional Socket.IO app
    from .services.validator_service import router as validator_router  # type: ignore
    HAS_VALIDATOR = True
    try:
        from .services.validator_service import socketio_app as _socketio_app  # type: ignore
        socketio_app = _socketio_app
        HAS_SOCKETIO = socketio_app is not None
    except Exception:
        socketio_app = None
        HAS_SOCKETIO = False
except Exception as e:
    logging.getLogger("uvicorn.error").warning("validator_service import failed: %s", e)
    # Fallback validator router if import fails
    _templates = Jinja2Templates(directory="app/templates")
    validator_router = APIRouter(prefix="/validator", tags=["Validator"])

    @validator_router.get("", response_class=HTMLResponse)
    @validator_router.get("/", response_class=HTMLResponse)
    async def _validator_fallback_ui(request: Request) -> HTMLResponse:
        # Try validator.hml first (project used this name), then validator.html
        try:
            return _templates.TemplateResponse("validator.hml", {"request": request})
        except Exception:
            return _templates.TemplateResponse(
                "validator.html",
                {"request": request, "warning": "validator service running in fallback mode"},
            )

TAGS_METADATA = [
    {"name": "Health", "description": "Liveness / readiness probes and basic service metadata."},
    VALIDATOR_TAG,
    # UI tag is implicit; only /home (Info) and /validator are exposed
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.started_at = time.time()
    app.state.version = os.getenv("APP_VERSION", "1.0.0")
    logger = logging.getLogger("uvicorn.error")

    # ---- RAG INIT DISABLED ----
    # try:
    #     if ensure_kb(out_jsonl="data/kb.jsonl", config_path="configs/rag_sources.yaml", skip_if_exists=True):
    #         logger.info("KB ready at data/kb.jsonl")
    #     else:
    #         logger.warning("KB build produced no records; running LLM-only.")
    # except Exception as e:
    #     logger.warning("KB build failed (%s); running LLM-only.", e)
    # logger.info("Warming up RAG retriever...")
    # get_retriever(get_settings())
    # logger.info("RAG retriever is ready.")

    hf_token_present = bool(os.getenv("HF_TOKEN"))
    logger.info(
        "matrix-ai starting (version=%s, port=%s, hf_token_present=%s)",
        app.state.version,
        os.getenv("PORT", "7860"),
        "yes" if hf_token_present else "no",
    )
    try:
        yield
    finally:
        uptime = time.time() - getattr(app.state, "started_at", time.time())
        logger.info("matrix-ai shutting down (uptime=%.2fs)", uptime)

def create_app() -> FastAPI:
    app = FastAPI(
        title="matrix-ai",
        version=os.getenv("APP_VERSION", "1.0.0"),
        description="Minimal service with A2A Validator and health endpoints",
        openapi_tags=TAGS_METADATA,
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    # Static files (for validator UI assets, etc.)
    try:
        app.mount("/static", StaticFiles(directory="app/static"), name="static")
    except Exception:
        pass

    # Middlewares (gzip, CORS, rate-limit, req-logs, etc.)
    attach_middlewares(app)

    # Core info/router pages
    app.include_router(health.router, tags=["Health"])

    # Validator router
    app.include_router(validator_router, tags=["Validator"])

    # UI router (enables /home "Info" page and "/" redirect defined in ui.py)
    app.include_router(ui_router)

    # Alias so the frontend can POST /agent-card (script.js default target)
    try:
        from .services.validator_service import get_agent_card as _get_agent_card  # type: ignore
        app.add_api_route(
            "/agent-card",
            _get_agent_card,
            methods=["POST"],
            tags=["Validator"],
            name="agent_card_alias",
        )
        logging.getLogger("uvicorn.error").info(
            "Added alias: POST /agent-card → /validator/agent-card"
        )
    except Exception as e:
        logging.getLogger("uvicorn.error").warning(
            f"Failed to add /agent-card alias: {e}"
        )

    # Mount Socket.IO if available
    if HAS_SOCKETIO and socketio_app is not None:
        app.mount("/socket.io", socketio_app)
        logging.getLogger("uvicorn.error").info("Mounted Socket.IO at /socket.io")

    # IMPORTANT:
    # Do NOT define extra "/" or "/home" handlers here.
    # ui.py already defines:
    #   - GET "/"  -> Redirect to /validator
    #   - GET "/home" -> Render home.html (Info tab)
    # Keeping only one definition avoids duplicate-route conflicts.

    return app

app = create_app()
