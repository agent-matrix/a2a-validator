# app/core/inference/providers.py
from __future__ import annotations

"""
Provider layer for multi-backend LLM chat with a production-ready cascade:

GROQ → Gemini → Hugging Face Inference Router (Zephyr → Mistral)

- Each provider implements a common .chat(...) interface that returns either:
    * str (non-stream), or
    * Generator[str, None, None] (streaming text chunks)

- MultiProviderChat orchestrates providers in a user-configurable order (Settings.provider_order)
  and returns the first successful response.

- Robustness:
    * .env + logging are loaded via app.bootstrap import side-effect
    * Requests session has retries and timeouts
    * Provider initialization gracefully skips when keys/SDKs are missing
    * Streaming uses SSE for HF Router; Groq uses SDK streaming; Gemini yields one chunk
"""

from typing import Any, Dict, Generator, Iterable, List, Optional, Union
import json
import logging
import os
import time

# Ensure .env + logging configured even if imported directly
import app.bootstrap  # noqa: F401

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Optional SDKs; handled gracefully if absent
try:
    from groq import Groq
except Exception:  # pragma: no cover
    Groq = None  # type: ignore

try:
    from google import genai
except Exception:  # pragma: no cover
    genai = None  # type: ignore

from app.core.config import Settings

logger = logging.getLogger(__name__)

Message = Dict[str, str]  # {"role": "system|user|assistant", "content": "..."}


# ---------- Errors ----------
class ProviderError(RuntimeError):
    """Raised for provider-specific configuration/runtime errors."""


# ---------- Helpers ----------
def _ensure_messages(msgs: Iterable[Message]) -> List[Message]:
    """
    Normalize incoming messages to a strict [{"role": str, "content": str}, ...] list.
    """
    out: List[Message] = []
    for m in msgs:
        role = m.get("role", "user")
        content = m.get("content", "")
        out.append({"role": role, "content": content})
    return out


def _requests_session_with_retries(
    total: int = 3,
    backoff: float = 0.3,
    status_forcelist: Optional[List[int]] = None,
    timeout: float = 60.0,
) -> requests.Session:
    """
    Return a requests.Session configured with retries, connection pooling, and default timeouts.
    """
    status_forcelist = status_forcelist or [408, 429, 500, 502, 503, 504]
    retry = Retry(
        total=total,
        read=total,
        connect=total,
        backoff_factor=backoff,
        status_forcelist=status_forcelist,
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    # Store default timeout on session via a patched request method
    session.request = _patch_request_with_timeout(session.request, timeout)  # type: ignore
    return session


def _patch_request_with_timeout(fn, timeout: float):
    def wrapper(method, url, **kwargs):
        if "timeout" not in kwargs:
            kwargs["timeout"] = timeout
        return fn(method, url, **kwargs)

    return wrapper


# ---------- GROQ ----------
class GroqProvider:
    """
    Groq Chat Completions (OpenAI-compatible).
    Requires:
        - env: GROQ_API_KEY
        - package: groq
    """
    name = "groq"

    def __init__(self, model: str):
        self.model = model
        self.api_key = os.getenv("GROQ_API_KEY")
        if not self.api_key:
            raise ProviderError("GROQ_API_KEY is not set")
        if Groq is None:
            raise ProviderError("groq SDK not installed; add 'groq' to requirements.txt and pip install.")
        # SDK reads key from env
        self.client = Groq()

    def chat(
        self,
        messages: Iterable[Message],
        temperature: float,
        max_new_tokens: int,
        stream: bool,
    ) -> Union[str, Generator[str, None, None]]:
        msgs = _ensure_messages(messages)
        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=msgs,
                temperature=float(temperature),
                max_tokens=int(max_new_tokens),
                top_p=1,
                stream=bool(stream),
            )
            if stream:
                def gen():
                    for chunk in completion:
                        try:
                            delta = chunk.choices[0].delta
                            part = getattr(delta, "content", None)
                            if part:
                                yield part
                        except Exception:
                            continue
                return gen()
            else:
                # Non-streaming: return final message content
                return completion.choices[0].message.content or ""
        except Exception as e:
            raise ProviderError(f"GROQ error: {e}") from e


# ---------- GEMINI ----------
class GeminiProvider:
    """
    Google Gemini via google-genai.
    Requires:
        - env: GOOGLE_API_KEY
        - package: google-genai

    Role mapping:
        - system → system_instruction (joined)
        - user   → role 'user'
        - assistant → role 'model'
    """
    name = "gemini"

    def __init__(self, model: str):
        self.model = model
        self.api_key = os.getenv("GOOGLE_API_KEY")
        if not self.api_key:
            raise ProviderError("GOOGLE_API_KEY is not set")
        if genai is None:
            raise ProviderError("google-genai SDK not installed; add 'google-genai' to requirements.txt and pip install.")
        self.client = genai.Client(api_key=self.api_key)

    @staticmethod
    def _split_system_and_messages(msgs: List[Message]) -> tuple[str, List[dict]]:
        system_parts: List[str] = []
        contents: List[dict] = []
        for m in msgs:
            role = m.get("role", "user")
            text = m.get("content", "")
            if role == "system":
                system_parts.append(text)
            else:
                mapped = "user" if role == "user" else "model"
                contents.append({"role": mapped, "parts": [{"text": text}]})
        return ("\n".join(system_parts).strip(), contents)

    def chat(
        self,
        messages: Iterable[Message],
        temperature: float,
        max_new_tokens: int,
        stream: bool,
    ) -> Union[str, Generator[str, None, None]]:
        msgs = _ensure_messages(messages)
        system_instruction, contents = self._split_system_and_messages(msgs)
        try:
            # Some versions of google-genai expose system_instruction; if not, we prepend.
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "contents": contents,
                "generation_config": {
                    "temperature": float(temperature),
                    "max_output_tokens": int(max_new_tokens),
                },
            }
            try:
                resp = self.client.models.generate_content(system_instruction=system_instruction or None, **kwargs)
            except TypeError:
                # Fallback for older SDKs: inject system as first user turn
                if system_instruction:
                    contents = [{"role": "user", "parts": [{"text": f"System: {system_instruction}"}]}] + contents
                    kwargs["contents"] = contents
                resp = self.client.models.generate_content(**kwargs)

            text = getattr(resp, "text", "") or ""

            if stream:
                # Fake streaming for API parity: one chunk
                def gen():
                    yield text
                return gen()
            return text
        except Exception as e:
            raise ProviderError(f"Gemini error: {e}") from e


# ---------- HF Inference Router ----------
class HfRouterProvider:
    """
    Hugging Face Inference Router (OpenAI-like /v1/chat/completions).
    Tries primary -> fallback model (both can include optional provider tag, e.g., "model:featherless-ai").

    Requires:
        - env: HF_TOKEN
        - package: requests
    """
    name = "router"
    BASE_URL = "https://router.huggingface.co/v1/chat/completions"

    def __init__(self, primary_model: str, fallback_model: Optional[str], provider_tag: Optional[str]):
        self.primary = primary_model
        self.fallback = fallback_model
        self.provider_tag = provider_tag
        self.token = os.getenv("HF_TOKEN")
        if not self.token:
            raise ProviderError("HF_TOKEN is not set")
        self.session = _requests_session_with_retries(total=3, backoff=0.5, timeout=60.0)

    def _fmt_model(self, model: str) -> str:
        return model if not self.provider_tag else f"{model}:{self.provider_tag}"

    def _sse_stream(self, resp: requests.Response) -> Generator[str, None, None]:
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            if not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            try:
                delta = obj["choices"][0].get("delta", {})
                content = delta.get("content")
                if content:
                    yield content
            except Exception:
                continue

    def _call_router(
        self,
        model: str,
        messages: List[Message],
        temperature: float,
        max_new_tokens: int,
        stream: bool,
    ) -> Union[str, Generator[str, None, None]]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": self._fmt_model(model),
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": int(max_new_tokens),
            "stream": bool(stream),
        }
        if stream:
            with self.session.post(self.BASE_URL, headers=headers, json=payload, stream=True) as r:
                if r.status_code >= 400:
                    raise ProviderError(f"HF Router HTTP {r.status_code}: {r.text[:300]}")
                return self._sse_stream(r)
        else:
            r = self.session.post(self.BASE_URL, headers=headers, json=payload)
            if r.status_code >= 400:
                raise ProviderError(f"HF Router HTTP {r.status_code}: {r.text[:300]}")
            obj = r.json()
            try:
                return obj["choices"][0]["message"]["content"]
            except Exception as e:
                raise ProviderError(f"HF Router response parsing error: {e}") from e

    def chat(
        self,
        messages: Iterable[Message],
        temperature: float,
        max_new_tokens: int,
        stream: bool,
    ) -> Union[str, Generator[str, None, None]]:
        msgs = _ensure_messages(messages)
        try:
            return self._call_router(self.primary, msgs, temperature, max_new_tokens, stream)
        except Exception as e1:
            logger.warning("HF primary model failed (%s): %s", self.primary, e1)
            if self.fallback:
                return self._call_router(self.fallback, msgs, temperature, max_new_tokens, stream)
            raise


# ---------- Orchestrator ----------
class MultiProviderChat:
    """
    Tries providers in configured order. First success wins.
    Skips misconfigured providers (missing key or SDK).
    """
    def __init__(self, settings: Settings):
        m = settings.model
        order = [p.strip().lower() for p in settings.provider_order]
        self.providers: List[Any] = []

        for p in order:
            try:
                if p == "groq":
                    self.providers.append(GroqProvider(m.groq_model))
                elif p == "gemini":
                    self.providers.append(GeminiProvider(m.gemini_model))
                elif p == "router":
                    self.providers.append(HfRouterProvider(m.name, m.fallback, m.provider))
                else:
                    logger.warning("Unknown provider '%s' in provider_order; skipping.", p)
            except ProviderError as e:
                logger.warning("Provider '%s' not available: %s (will skip)", p, e)
                continue

        if not self.providers:
            raise ProviderError("No providers are configured/available")

        self.temperature = m.temperature
        self.max_new_tokens = m.max_new_tokens

    def chat(
        self,
        messages: Iterable[Message],
        temperature: Optional[float] = None,
        max_new_tokens: Optional[int] = None,
        stream: bool = True,
    ) -> Union[str, Generator[str, None, None]]:
        temp = float(self.temperature if temperature is None else temperature)
        mx = int(self.max_new_tokens if max_new_tokens is None else max_new_tokens)
        last_err: Optional[Exception] = None

        for provider in self.providers:
            pname = getattr(provider, "name", provider.__class__.__name__)
            t0 = time.time()
            try:
                result = provider.chat(messages, temp, mx, stream)
                logger.info("Provider '%s' succeeded in %.2fs", pname, time.time() - t0)
                return result
            except Exception as e:
                logger.warning("Provider '%s' failed: %s", pname, e)
                last_err = e
                continue

        raise ProviderError(f"All providers failed. Last error: {last_err}")


__all__ = [
    "ProviderError",
    "GroqProvider",
    "GeminiProvider",
    "HfRouterProvider",
    "MultiProviderChat",
]
