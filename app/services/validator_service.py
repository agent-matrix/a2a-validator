# app/services/validator_service.py
"""
A2A Validator service.
- Provides /validator (UI) + /validator/agent-card (HTTP) routes.
- Defines all Socket.IO event handlers.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import bleach
import httpx

# Socket.IO is optional; create shims when missing
try:
    import socketio  # type: ignore
    HAS_SOCKETIO = True
except Exception:  # pragma: no cover
    socketio = None  # type: ignore
    HAS_SOCKETIO = False

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from jinja2 import TemplateNotFound

# Conditional import for A2A SDK (optional)
try:
    from a2a.client import A2ACardResolver, A2AClient
    from a2a.types import (
        AgentCard,
        JSONRPCErrorResponse,
        Message,
        MessageSendConfiguration,
        MessageSendParams,
        Role,
        SendMessageRequest,
        SendMessageResponse,
        SendStreamingMessageRequest,
        SendStreamingMessageResponse,
        TextPart,
    )
    HAS_A2A = True
except Exception:
    HAS_A2A = False
    # Dummy stand-ins so type hints won’t explode
    AgentCard = JSONRPCErrorResponse = Message = MessageSendConfiguration = object  # type: ignore
    MessageSendParams = Role = SendMessageRequest = SendMessageResponse = object  # type: ignore
    SendStreamingMessageRequest = SendStreamingMessageResponse = TextPart = object  # type: ignore
    A2ACardResolver = A2AClient = object  # type: ignore

from app import validators  # local validators.py

# ==============================================================================
# Setup
# ==============================================================================
logger = logging.getLogger("uvicorn.error")

if HAS_SOCKETIO:
    sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
    socketio_app = socketio.ASGIApp(sio)
else:
    class _SioShim:
        async def emit(self, *a, **k):  # no-op
            pass

        def on(self, *a, **k):
            def _wrap(f):
                return f
            return _wrap

        event = on

    sio = _SioShim()
    socketio_app = None

router = APIRouter(prefix="/validator", tags=["Validator"])
templates = Jinja2Templates(directory="app/templates")

STANDARD_HEADERS = {
    "host",
    "user-agent",
    "accept",
    "content-type",
    "content-length",
    "connection",
    "accept-encoding",
}

# ==============================================================================
# State Management
# ==============================================================================
clients: dict[str, tuple[httpx.AsyncClient, Any, Any]] = {}

# ==============================================================================
# Helpers
# ==============================================================================
async def _emit_debug_log(sid: str, event_id: str, log_type: str, data: Any) -> None:
    await sio.emit("debug_log", {"type": log_type, "data": data, "id": event_id}, to=sid)


async def _process_a2a_response(result: Any, sid: str, request_id: str) -> None:
    if not HAS_A2A:
        return

    if isinstance(result.root, JSONRPCErrorResponse):
        error_data = result.root.error.model_dump(exclude_none=True)
        await _emit_debug_log(sid, request_id, "error", error_data)
        await sio.emit(
            "agent_response",
            {"error": error_data.get("message", "Unknown error"), "id": request_id},
            to=sid,
        )
        return

    event = result.root.result
    response_id = getattr(event, "id", request_id)
    response_data = event.model_dump(exclude_none=True)
    response_data["id"] = response_id
    response_data["validation_errors"] = validators.validate_message(response_data)

    await _emit_debug_log(sid, response_id, "response", response_data)
    await sio.emit("agent_response", response_data, to=sid)


def get_card_resolver(client: httpx.AsyncClient, agent_card_url: str) -> Any:
    if not HAS_A2A:
        return None
    parsed_url = urlparse(agent_card_url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    path_with_query = urlunparse(("", "", parsed_url.path, "", parsed_url.query, ""))
    card_path = path_with_query.lstrip("/")
    if card_path:
        return A2ACardResolver(client, base_url, agent_card_path=card_path)
    return A2ACardResolver(client, base_url)

# ==============================================================================
# FastAPI Routes
# ==============================================================================
@router.get("/", response_class=HTMLResponse)
async def validator_ui(request: Request) -> HTMLResponse:
    # Prefer validator.hml (your current file), fallback to validator.html
    for name in ("validator.hml", "validator.html"):
        try:
            return templates.TemplateResponse(name, {"request": request})
        except TemplateNotFound:
            continue
    # If neither exists, return a minimal message
    return HTMLResponse("<h3>Validator UI template not found.</h3>", status_code=500)


@router.post("/agent-card")
async def get_agent_card(request: Request) -> JSONResponse:
    """
    Fetch and validate an Agent Card from a URL.

    If A2A SDK is installed, use its resolver.
    Otherwise, be lenient: follow redirects and probe common well-known paths.
    """
    # Parse request body
    try:
        request_data = await request.json()
        agent_url = (request_data.get("url") or "").strip()
        sid = request_data.get("sid")
        if not agent_url or not sid:
            return JSONResponse({"error": "Agent URL and SID are required."}, status_code=400)
    except Exception:
        return JSONResponse({"error": "Invalid request body."}, status_code=400)

    # Collect custom headers (forwarded to the target)
    custom_headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in STANDARD_HEADERS
    }

    await _emit_debug_log(
        sid,
        "http-agent-card",
        "request",
        {"endpoint": "/agent-card", "payload": request_data, "custom_headers": custom_headers},
    )

    # Fetch the agent card
    try:
        async with httpx.AsyncClient(
            timeout=30.0,
            headers=custom_headers,
            follow_redirects=True,  # <<< important for 3xx like 307 to /docs
        ) as client:
            if HAS_A2A:
                # Preferred path: let the resolver figure out the right card location
                card_resolver = get_card_resolver(client, agent_url)
                card = await card_resolver.get_agent_card()
                card_data = card.model_dump(exclude_none=True)
            else:
                # Fallback: try what the user typed first; if non-JSON, probe common paths
                tried: list[str] = []

                async def _try(url: str) -> dict[str, Any]:
                    r = await client.get(url)
                    r.raise_for_status()
                    ctype = (r.headers.get("content-type") or "").lower()
                    if "application/json" in ctype or ctype.endswith("+json"):
                        return r.json()
                    # If we got HTML or something else, raise to trigger probing
                    raise ValueError(f"Non-JSON response (content-type={ctype or 'unknown'}) at {url}")

                try:
                    card_data = await _try(agent_url)
                except Exception:
                    # If the user pasted a base/root URL, probe common Agent Card paths on same host
                    parsed = urlparse(agent_url)
                    base = f"{parsed.scheme}://{parsed.netloc}"
                    candidates = [
                        agent_url,  # original again (in case it became JSON after redirect)
                        f"{base}/.well-known/agent.json",
                        f"{base}/.well-known/ai-agent.json",
                        f"{base}/agent-card",
                        f"{base}/agent.json",
                    ]
                    err: Exception | None = None
                    card_data = None
                    for u in candidates:
                        if u in tried:
                            continue
                        tried.append(u)
                        try:
                            card_data = await _try(u)
                            agent_url = u  # record the working URL
                            break
                        except Exception as e:
                            err = e
                    if card_data is None:
                        raise RuntimeError(
                            f"Could not find a JSON Agent Card at {agent_url} (last error: {err})"
                        )

        # Validate locally
        validation_errors = validators.validate_agent_card(card_data)  # type: ignore[arg-type]
        response = {
            "card": card_data,
            "validation_errors": validation_errors,
            "resolved_url": agent_url,
        }
        status = 200

    except httpx.RequestError as e:
        response = {"error": f"Failed to connect to agent: {e}"}
        status = 502
    except Exception as e:
        response = {"error": f"An internal server error occurred: {e}"}
        status = 500

    await _emit_debug_log(sid, "http-agent-card", "response", {"status": status, "payload": response})
    return JSONResponse(content=response, status_code=status)

# ==============================================================================
# Socket.IO Event Handlers
# ==============================================================================
@sio.on("connect")
async def handle_connect(sid: str, environ: dict[str, Any]) -> None:  # type: ignore[misc]
    logger.info(f"Client connected: {sid}")


@sio.on("disconnect")
async def handle_disconnect(sid: str) -> None:  # type: ignore[misc]
    logger.info(f"Client disconnected: {sid}")
    if sid in clients:
        httpx_client, _, _ = clients.pop(sid)
        await httpx_client.aclose()
        logger.info(f"Cleaned up client for {sid}")


@sio.on("initialize_client")
async def handle_initialize_client(sid: str, data: dict[str, Any]) -> None:  # type: ignore[misc]
    """
    Prepare an A2A client for chat/streaming. If a2a is not installed, reply with a warning
    so the UI still proceeds (card viewing still works via HTTP).
    """
    if not HAS_A2A:
        await sio.emit(
            "client_initialized",
            {"status": "warning", "message": "A2A SDK not installed; chat/streaming disabled."},
            to=sid,
        )
        return

    agent_card_url = data.get("url")
    custom_headers = data.get("customHeaders", {})
    if not agent_card_url:
        await sio.emit("client_initialized", {"status": "error", "message": "Agent URL is required."}, to=sid)
        return

    try:
        httpx_client = httpx.AsyncClient(timeout=600.0, headers=custom_headers)
        card_resolver = get_card_resolver(httpx_client, agent_card_url)
        card = await card_resolver.get_agent_card()
        a2a_client = A2AClient(httpx_client, agent_card=card)
        clients[sid] = (httpx_client, a2a_client, card)
        await sio.emit("client_initialized", {"status": "success"}, to=sid)
    except Exception as e:
        await sio.emit("client_initialized", {"status": "error", "message": str(e)}, to=sid)


@sio.on("send_message")
async def handle_send_message(sid: str, json_data: dict[str, Any]) -> None:  # type: ignore[misc]
    if not HAS_A2A:
        await sio.emit("agent_response", {"error": "A2A SDK not installed", "id": json_data.get("id")}, to=sid)
        return

    message_text = bleach.clean(json_data.get("message", ""))
    message_id = json_data.get("id", str(uuid4()))
    context_id = json_data.get("contextId")
    metadata = json_data.get("metadata", {})

    if sid not in clients:
        await sio.emit("agent_response", {"error": "Client not initialized.", "id": message_id}, to=sid)
        return

    _, a2a_client, card = clients[sid]

    message = Message(
        role=Role.user,
        parts=[TextPart(text=str(message_text))],  # type: ignore[list-item]
        message_id=message_id,
        context_id=context_id,
        metadata=metadata,
    )
    payload = MessageSendParams(
        message=message,
        configuration=MessageSendConfiguration(accepted_output_modes=["text/plain", "video/mp4"]),
    )
    supports_streaming = hasattr(card.capabilities, "streaming") and card.capabilities.streaming is True

    try:
        if supports_streaming:
            stream_request = SendStreamingMessageRequest(
                id=message_id, method="message/stream", jsonrpc="2.0", params=payload
            )
            await _emit_debug_log(sid, message_id, "request", stream_request.model_dump(exclude_none=True))
            response_stream = a2a_client.send_message_streaming(stream_request)
            async for stream_result in response_stream:
                await _process_a2a_response(stream_result, sid, message_id)
        else:
            send_message_request = SendMessageRequest(
                id=message_id, method="message/send", jsonrpc="2.0", params=payload
            )
            await _emit_debug_log(sid, message_id, "request", send_message_request.model_dump(exclude_none=True))
            send_result = await a2a_client.send_message(send_message_request)
            await _process_a2a_response(send_result, sid, message_id)
    except Exception as e:
        await sio.emit("agent_response", {"error": f"Failed to send message: {e}", "id": message_id}, to=sid)

__all__ = ["router", "socketio_app", "HAS_SOCKETIO"]
