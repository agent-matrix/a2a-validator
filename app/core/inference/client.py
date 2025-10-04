# app/core/inference/client.py
from __future__ import annotations

"""
Unified chat client module.

- Exposes a production-ready MultiProvider cascade client (GROQ → Gemini → HF Router),
  via ChatClient / chat(...).
- Keeps the legacy RouterRequestsClient for direct access to the HF Router compatible
  /v1/chat/completions endpoint, preserving backward compatibility.

This file assumes:
  - app/bootstrap.py exists and loads configs/.env + sets up logging.
  - app/core/config.py provides Settings (with provider_order, etc.).
  - app/core/inference/providers.py implements MultiProviderChat orchestrator.
"""

import os
import json
import time
import logging
from typing import Dict, List, Optional, Iterator, Tuple, Iterable, Union, Generator

# Ensure .env & logging before we load settings/providers
import app.bootstrap  # noqa: F401

import requests

from app.core.config import Settings
from app.core.inference.providers import MultiProviderChat

logger = logging.getLogger(__name__)

# -----------------------------
# Multi-provider cascade client
# -----------------------------

Message = Dict[str, str]

class ChatClient:
    """
    Unified chat client that executes the configured provider cascade.
    Providers are tried in order (settings.provider_order). First success wins.
    """
    def __init__(self, settings: Settings | None = None):
        self._settings = settings or Settings.load()
        self._chain = MultiProviderChat(self._settings)

    def chat(
        self,
        messages: Iterable[Message],
        temperature: Optional[float] = None,
        max_new_tokens: Optional[int] = None,
        stream: Optional[bool] = None,
    ) -> Union[str, Generator[str, None, None]]:
        """
        Execute a chat completion using the provider cascade.

        Args:
            messages: Iterable of {"role": "system|user|assistant", "content": "..."}
            temperature: Optional override for sampling temperature.
            max_new_tokens: Optional override for max tokens.
            stream: If None, uses settings.chat_stream. If True, returns a generator of text chunks.

        Returns:
            str (non-stream) or generator[str] (stream)
        """
        use_stream = self._settings.chat_stream if stream is None else bool(stream)
        return self._chain.chat(
            messages,
            temperature=temperature,
            max_new_tokens=max_new_tokens,
            stream=use_stream,
        )

# Backward-compatible helpers
_default_client: ChatClient | None = None

def _get_default() -> ChatClient:
    global _default_client
    if _default_client is None:
        _default_client = ChatClient()
    return _default_client

def chat(
    messages: Iterable[Message],
    temperature: Optional[float] = None,
    max_new_tokens: Optional[int] = None,
    stream: Optional[bool] = None,
) -> Union[str, Generator[str, None, None]]:
    """
    Convenience function using a process-wide default ChatClient.
    """
    return _get_default().chat(messages, temperature=temperature, max_new_tokens=max_new_tokens, stream=stream)

def get_client(settings: Settings | None = None) -> ChatClient:
    """
    Factory for an explicit ChatClient bound to provided settings.
    """
    return ChatClient(settings)


# ------------------------------------------------------
# Legacy HF Router client (kept for backward compatibility)
# ------------------------------------------------------

ROUTER_URL = "https://router.huggingface.co/v1/chat/completions"

def _require_token() -> str:
    tok = os.getenv("HF_TOKEN")
    if not tok:
        raise ValueError("HF_TOKEN is not set. Put it in .env or export it before starting.")
    return tok

def _model_with_provider(model: str, provider: Optional[str]) -> str:
    if provider and ":" not in model:
        return f"{model}:{provider}"
    return model

def _mk_messages(system_prompt: Optional[str], user_text: str) -> List[Dict[str, str]]:
    msgs: List[Dict[str, str]] = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    msgs.append({"role": "user", "content": user_text})
    return msgs

def _timeout_tuple(connect: float = 10.0, read: float = 60.0) -> Tuple[float, float]:
    return (connect, read)

class RouterRequestsClient:
    """
    Simple requests-only client for HF Router Chat Completions.
    Supports non-streaming (returns str) and streaming (yields token strings).

    NOTE: New code should prefer ChatClient above. This class is preserved for any
    legacy call sites that rely on direct HF Router access.
    """
    def __init__(
        self,
        model: str,
        fallback: Optional[str] = None,
        provider: Optional[str] = None,
        max_retries: int = 2,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0
    ):
        self.model = model
        self.fallback = fallback if fallback != model else None
        self.provider = provider
        self.headers = {"Authorization": f"Bearer {_require_token()}"}
        self.max_retries = max(0, int(max_retries))
        self.timeout = _timeout_tuple(connect_timeout, read_timeout)

    # -------- Non-stream (single text) --------
    def chat_nonstream(
        self,
        system_prompt: Optional[str],
        user_text: str,
        max_tokens: int,
        temperature: float,
        stop: Optional[List[str]] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
    ) -> str:
        payload = {
            "model": _model_with_provider(self.model, self.provider),
            "messages": _mk_messages(system_prompt, user_text),
            "temperature": float(max(0.0, temperature)),
            "max_tokens": int(max_tokens),
            "stream": False,
        }
        if stop:
            payload["stop"] = stop
        if frequency_penalty is not None:
            payload["frequency_penalty"] = float(frequency_penalty)
        if presence_penalty is not None:
            payload["presence_penalty"] = float(presence_penalty)

        text, ok = self._try_once(payload)
        if ok:
            return text

        # fallback (if configured)
        if self.fallback:
            payload["model"] = _model_with_provider(self.fallback, self.provider)
            text, ok = self._try_once(payload)
            if ok:
                return text

        raise RuntimeError(f"Chat non-stream failed: model={self.model} fallback={self.fallback}")

    def _try_once(self, payload: dict) -> Tuple[str, bool]:
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                r = requests.post(ROUTER_URL, headers=self.headers, json=payload, timeout=self.timeout)
                if r.status_code >= 400:
                    logger.error("Router error %s: %s", r.status_code, r.text)
                    last_err = RuntimeError(f"{r.status_code}: {r.text}")
                    time.sleep(min(1.5 * (attempt + 1), 3.0))
                    continue
                data = r.json()
                return data["choices"][0]["message"]["content"], True
            except Exception as e:
                logger.error("Router request failure: %s", e)
                last_err = e
                time.sleep(min(1.5 * (attempt + 1), 3.0))
        if last_err:
            logger.error("Router exhausted retries: %s", last_err)
        return "", False

    # -------- Streaming (yield token deltas) --------
    def chat_stream(
        self,
        system_prompt: Optional[str],
        user_text: str,
        max_tokens: int,
        temperature: float,
        stop: Optional[List[str]] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
    ) -> Iterator[str]:
        payload = {
            "model": _model_with_provider(self.model, self.provider),
            "messages": _mk_messages(system_prompt, user_text),
            "temperature": float(max(0.0, temperature)),
            "max_tokens": int(max_tokens),
            "stream": True,
        }
        if stop:
            payload["stop"] = stop
        if frequency_penalty is not None:
            payload["frequency_penalty"] = float(frequency_penalty)
        if presence_penalty is not None:
            payload["presence_penalty"] = float(presence_penalty)

        # primary
        ok = False
        for token in self._stream_once(payload):
            ok = True
            yield token
        if ok:
            return
        # fallback stream if primary produced nothing (or died immediately)
        if self.fallback:
            payload["model"] = _model_with_provider(self.fallback, self.provider)
            for token in self._stream_once(payload):
                yield token

    def _stream_once(self, payload: dict) -> Iterator[str]:
        try:
            with requests.post(ROUTER_URL, headers=self.headers, json=payload, stream=True, timeout=self.timeout) as r:
                if r.status_code >= 400:
                    logger.error("Router stream error %s: %s", r.status_code, r.text)
                    return
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        return
                    try:
                        obj = json.loads(data)
                        delta = obj["choices"][0]["delta"].get("content", "")
                        if delta:
                            yield delta
                    except Exception as e:
                        logger.warning("Stream JSON parse issue: %s | line=%r", e, line)
                        continue
        except Exception as e:
            logger.error("Stream request failure: %s", e)
            return

    # -------- Planning (non-stream) --------
    def plan_nonstream(self, system_prompt: str, user_text: str,
                       max_tokens: int, temperature: float) -> str:
        return self.chat_nonstream(system_prompt, user_text, max_tokens, temperature)


__all__ = [
    "ChatClient",
    "chat",
    "get_client",
    "RouterRequestsClient",
]
