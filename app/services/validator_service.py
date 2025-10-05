# app/services/validator_service.py
"""
A2A Validator service.
- Provides /validator (UI) + /validator/agent-card (HTTP) routes.
- Defines all Socket.IO event handlers.
- Automatic localhost rewriting: if an Agent Card's "url" is localhost/127.0.0.1,
  we rewrite it to the origin that served the card (same scheme+host+port), then
  probe connectivity. If that fails, we try host.docker.internal:<same-port>.
"""
from __future__ import annotations

import logging
import socket
from typing import Any, Mapping, Tuple
from urllib.parse import urlparse, urlunparse, ParseResult
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

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

# ==============================================================================
# State Management
# ==============================================================================
# sid -> (httpx_client, a2a_client, card, origin_used_for_card_fetch)
clients: dict[str, tuple[httpx.AsyncClient, Any, Any, str]] = {}

# ==============================================================================
# URL helpers / rewriting
# ==============================================================================
def _parse(url: str) -> ParseResult:
    return urlparse(url)


def _build(pr: ParseResult) -> str:
    return urlunparse(pr)


def _origin_of(url: str) -> str:
    """
    Return scheme://netloc for a URL (no path/query/fragment).
    """
    pr = _parse(url)
    return f"{pr.scheme}://{pr.netloc}" if pr.scheme and pr.netloc else ""


def _looks_localhost(host: str | None) -> bool:
    return (host or "").lower() in LOCAL_HOSTS


def _docker_has_host_gateway() -> bool:
    try:
        socket.gethostbyname("host.docker.internal")
        return True
    except Exception:
        return False


def _rewrite_to_origin(card_url: ParseResult, card_origin: ParseResult) -> Tuple[ParseResult, str | None]:
    """
    If the card_url host is localhost/127.0.0.1 and we know the origin where the
    Agent Card was fetched from, rewrite to that origin (same scheme+host[:port]),
    preserving the /path?query.
    """
    if not _looks_localhost(card_url.hostname):
        return card_url, None
    if not (card_origin.scheme and card_origin.netloc):
        return card_url, None

    rewritten = ParseResult(
        scheme=card_origin.scheme,
        netloc=card_origin.netloc,
        path=card_url.path or "",
        params="",
        query=card_url.query or "",
        fragment="",
    )
    return rewritten, "rewritten to Agent Card origin"


def _rewrite_to_gateway(card_url: ParseResult) -> Tuple[ParseResult, str | None]:
    """
    Fallback: rewrite localhost to host.docker.internal:<same-port> if resolvable.
    """
    if not _looks_localhost(card_url.hostname):
        return card_url, None
    if not _docker_has_host_gateway():
        return card_url, None

    port = f":{card_url.port}" if card_url.port else ""
    rewritten = ParseResult(
        scheme=card_url.scheme or "http",
        netloc=f"host.docker.internal{port}",
        path=card_url.path or "",
        params="",
        query=card_url.query or "",
        fragment="",
    )
    return rewritten, "rewritten via host.docker.internal"


async def _probe_reachable(client: httpx.AsyncClient, url: str) -> Tuple[bool, str]:
    """
    Cheap reachability probe.
    - 2xx/3xx reachable
    - 405 counts as reachable (JSON-RPC endpoints often reject GET)
    """
    try:
        r = await client.get(url)
        if r.status_code == 405:
            return True, "reachable (405 on GET is OK for JSON-RPC)"
        if 200 <= r.status_code < 400:
            return True, f"reachable (HTTP {r.status_code})"
        return False, f"HTTP {r.status_code}"
    except httpx.ConnectError as e:
        return False, f"connect error: {e!s}"
    except httpx.RequestError as e:
        return False, f"request error: {e!s}"
    except Exception as e:
        return False, f"unexpected error: {e!s}"


def _card_copy_with_url(card: AgentCard, new_url: str) -> AgentCard:
    try:
        return card.model_copy(update={"url": new_url})  # type: ignore[attr-defined]
    except Exception:
        try:
            card.url = new_url  # type: ignore[attr-defined]
            return card
        except Exception:
            raise


# ==============================================================================
# Debug helpers
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

# Handle both /validator and /validator/
@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse)
async def validator_ui(request: Request) -> HTMLResponse:
    for name in ("validator.html", "validator.hml"):
        try:
            return templates.TemplateResponse(name, {"request": request})
        except TemplateNotFound:
            continue
    return HTMLResponse("<h3>Validator UI template not found.</h3>", status_code=500)


@router.post("/agent-card")
async def get_agent_card(request: Request) -> JSONResponse:
    """
    Fetch and validate an Agent Card from a URL.

    If A2A SDK is installed, use its resolver.
    Otherwise, be lenient: follow redirects and probe common well-known paths.
    Automatically rewrite localhost URLs in the card to the card's own origin.
    """
    # Parse request body
    try:
        request_data = await request.json()
        user_url = (request_data.get("url") or "").strip()
        sid = request_data.get("sid")
        if not user_url or not sid:
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
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers=custom_headers,
            follow_redirects=True,
            trust_env=True,
        ) as client:
            # We'll remember the ORIGIN we used to reach the card, for rewriting.
            card_fetch_origin = _origin_of(user_url)

            if HAS_A2A:
                resolver = get_card_resolver(client, user_url)
                card = await resolver.get_agent_card()  # type: ignore[assignment]
                card_data = card.model_dump(exclude_none=True)
                # Origin we used is the resolver's base (scheme+host[:port])
                card_fetch_origin = _origin_of(user_url)
            else:
                tried: list[str] = []

                async def _try(url: str) -> dict[str, Any]:
                    r = await client.get(url)
                    r.raise_for_status()
                    ctype = (r.headers.get("content-type") or "").lower()
                    if "application/json" in ctype or ctype.endswith("+json"):
                        return r.json()
                    raise ValueError(f"Non-JSON response (content-type={ctype or 'unknown'}) at {url}")

                # Try the user URL first; otherwise probe well-knowns at that origin
                try:
                    card_data = await _try(user_url)
                except Exception:
                    pr = _parse(user_url)
                    base = f"{pr.scheme}://{pr.netloc}" if pr.scheme and pr.netloc else ""
                    candidates = [
                        user_url,
                        f"{base}/.well-known/agent.json",
                        f"{base}/.well-known/ai-agent.json",
                        f"{base}/agent-card",
                        f"{base}/agent.json",
                    ]
                    last_err: Exception | None = None
                    card_data = None  # type: ignore[assignment]
                    for u in candidates:
                        if u in tried or not u.startswith("http"):
                            continue
                        tried.append(u)
                        try:
                            card_data = await _try(u)
                            card_fetch_origin = _origin_of(u)
                            break
                        except Exception as e:
                            last_err = e
                    if card_data is None:  # type: ignore[truthy-bool]
                        raise RuntimeError(
                            f"Could not find a JSON Agent Card at {user_url} (last error: {last_err})"
                        )

        # Validate locally
        validation_errors = validators.validate_agent_card(card_data)  # type: ignore[arg-type]

        # --- Automatic localhost rewrite of the card's own URL ---
        rewrite_note = None
        resolved_card_url = None
        try:
            card_origin_pr = _parse(card_fetch_origin) if card_fetch_origin else None
            raw_card_url = (card_data.get("url") if isinstance(card_data, Mapping) else None) or ""
            card_url_pr = _parse(raw_card_url)

            if _looks_localhost(card_url_pr.hostname):
                # 1) Prefer rewrite to the origin that served the Agent Card
                if card_origin_pr and (card_origin_pr.scheme and card_origin_pr.netloc):
                    new_pr, note = _rewrite_to_origin(card_url_pr, card_origin_pr)
                    if note:
                        resolved_card_url = _build(new_pr)
                        card_data = {**card_data, "url": resolved_card_url}  # type: ignore[operator]
                        rewrite_note = note
                # 2) Fallback to host.docker.internal if available
                if not resolved_card_url:
                    new_pr, note = _rewrite_to_gateway(card_url_pr)
                    if note:
                        resolved_card_url = _build(new_pr)
                        card_data = {**card_data, "url": resolved_card_url}  # type: ignore[operator]
                        rewrite_note = note
        except Exception as e:
            # Do not fail the card response if rewriting fails
            rewrite_note = f"rewrite failed: {e}"

        response = {
            "card": card_data,
            "validation_errors": validation_errors,
            "resolved_url": (card_data.get("url") if isinstance(card_data, Mapping) else None),  # type: ignore[union-attr]
            "rewrite_note": rewrite_note,
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
        httpx_client, _, _, _ = clients.pop(sid)
        await httpx_client.aclose()
        logger.info(f"Cleaned up client for {sid}")


@sio.on("initialize_client")
async def handle_initialize_client(sid: str, data: dict[str, Any]) -> None:  # type: ignore[misc]
    """
    Prepare an A2A client for chat/streaming.
    Automatically rewrites localhost card URLs to the card origin or host.docker.internal.
    """
    if not HAS_A2A:
        await sio.emit(
            "client_initialized",
            {"status": "warning", "message": "A2A SDK not installed; chat/streaming disabled."},
            to=sid,
        )
        return

    user_url = (data.get("url") or "").strip()
    custom_headers = data.get("customHeaders", {}) or {}
    if not user_url:
        await sio.emit("client_initialized", {"status": "error", "message": "Agent URL is required."}, to=sid)
        return

    httpx_client = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=15.0),
        headers=custom_headers,
        follow_redirects=True,
        trust_env=True,
    )

    try:
        # Resolve card (this base will be our "origin" candidate)
        resolver = get_card_resolver(httpx_client, user_url)
        card: AgentCard = await resolver.get_agent_card()  # type: ignore[assignment]
        card_fetch_origin = _origin_of(user_url)
        origin_pr = _parse(card_fetch_origin) if card_fetch_origin else None

        # Rewrite card.url if it's localhost to the card origin first
        try:
            card_url_pr = _parse(getattr(card, "url", "") or "")
            if _looks_localhost(card_url_pr.hostname):
                if origin_pr and (origin_pr.scheme and origin_pr.netloc):
                    new_pr, note = _rewrite_to_origin(card_url_pr, origin_pr)
                    if note:
                        new_url = _build(new_pr)
                        card = _card_copy_with_url(card, new_url)
                        await _emit_debug_log(
                            sid,
                            "client-init-rewrite",
                            "info",
                            {"original": card_url_pr.geturl(), "rewritten": new_url, "note": note},
                        )
                # Fallback to host.docker.internal if still localhost
                card_url_pr2 = _parse(getattr(card, "url", "") or "")
                if _looks_localhost(card_url_pr2.hostname):
                    new_pr, note = _rewrite_to_gateway(card_url_pr2)
                    if note:
                        new_url = _build(new_pr)
                        card = _card_copy_with_url(card, new_url)
                        await _emit_debug_log(
                            sid,
                            "client-init-gateway",
                            "info",
                            {"original": card_url_pr2.geturl(), "rewritten": new_url, "note": note},
                        )
        except Exception as e:
            await _emit_debug_log(sid, "client-init-rewrite", "warn", {"error": str(e)})

        # Connectivity probe before enabling chat
        ok, detail = await _probe_reachable(httpx_client, getattr(card, "url", ""))
        if not ok:
            await sio.emit(
                "client_initialized",
                {
                    "status": "error",
                    "message": f"Agent endpoint unreachable: {detail}. "
                               f"Ensure your Agent Card URL points to a network-reachable host.",
                },
                to=sid,
            )
            await httpx_client.aclose()
            return

        # Create A2A client and store
        a2a_client = A2AClient(httpx_client, agent_card=card)
        clients[sid] = (httpx_client, a2a_client, card, card_fetch_origin)
        await sio.emit("client_initialized", {"status": "success"}, to=sid)

    except Exception as e:
        await httpx_client.aclose()
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

    httpx_client, a2a_client, card, _origin = clients[sid]

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
    supports_streaming = hasattr(card.capabilities, "streaming") and getattr(card.capabilities, "streaming") is True

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
