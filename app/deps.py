from functools import lru_cache
from .core.config import Settings

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """FastAPI dependency to get application settings."""
    return Settings.load()
