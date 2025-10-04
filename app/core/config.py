from __future__ import annotations
import os, yaml
from pydantic import BaseModel, AnyHttpUrl
from typing import Optional, List

class ModelCfg(BaseModel):
    # HF Router defaults (used when we reach the router)
    name: str = "HuggingFaceH4/zephyr-7b-beta"
    fallback: str = "mistralai/Mistral-7B-Instruct-v0.2"
    max_new_tokens: int = 256
    temperature: float = 0.2
    provider: Optional[str] = None  # HF Router provider tag (e.g., "featherless-ai")

    # New: provider-specific default models
    groq_model: str = "llama-3.1-8b-instant"
    gemini_model: str = "gemini-2.5-flash"

class LimitsCfg(BaseModel):
    rate_per_min: int = 60
    cache_size: int = 256

class RagCfg(BaseModel):
    index_dataset: Optional[str] = None
    top_k: int = 4

class MatrixHubCfg(BaseModel):
    base_url: AnyHttpUrl = "https://api.matrixhub.io"

class SecurityCfg(BaseModel):
    admin_token: Optional[str] = None

class Settings(BaseModel):
    model: ModelCfg = ModelCfg()
    limits: LimitsCfg = LimitsCfg()
    rag: RagCfg = RagCfg()
    matrixhub: MatrixHubCfg = MatrixHubCfg()
    security: SecurityCfg = SecurityCfg()

    # New
    provider_order: List[str] = ["groq", "gemini", "router"]  # cascade order
    chat_backend: str = "multi"   # was "router"; "multi" enables cascade
    chat_stream: bool = True

    @staticmethod
    def load() -> "Settings":
        path = os.getenv("SETTINGS_FILE", "configs/settings.yaml")
        data = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

        settings = Settings.model_validate(data)

        # Existing env overrides
        if "MODEL_NAME" in os.environ:
            settings.model.name = os.environ["MODEL_NAME"]
        if "MODEL_FALLBACK" in os.environ:
            settings.model.fallback = os.environ["MODEL_FALLBACK"]
        if "MODEL_PROVIDER" in os.environ:
            settings.model.provider = os.environ["MODEL_PROVIDER"]
        if "ADMIN_TOKEN" in os.environ:
            settings.security.admin_token = os.environ["ADMIN_TOKEN"]
        if "RATE_LIMITS" in os.environ:
            settings.limits.rate_per_min = int(os.environ["RATE_LIMITS"])
        if "HF_CHAT_BACKEND" in os.environ:
            settings.chat_backend = os.environ["HF_CHAT_BACKEND"].strip().lower()
        if "CHAT_STREAM" in os.environ:
            settings.chat_stream = os.environ["CHAT_STREAM"].lower() in ("1","true","yes","on")

        # New env overrides
        if "GROQ_MODEL" in os.environ:
            settings.model.groq_model = os.environ["GROQ_MODEL"]
        if "GEMINI_MODEL" in os.environ:
            settings.model.gemini_model = os.environ["GEMINI_MODEL"]
        if "PROVIDER_ORDER" in os.environ:
            settings.provider_order = [p.strip().lower() for p in os.environ["PROVIDER_ORDER"].split(",") if p.strip()]

        # Default to cascade
        if settings.chat_backend not in ("multi", "router"):
            settings.chat_backend = "multi"

        return settings
