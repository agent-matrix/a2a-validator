import re

SECRET_PATTERN = re.compile(r"(bearer\s+[a-z0-9\-.~+/]+=*|sk-[a-z0-9]{20,})", re.IGNORECASE)
EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", re.IGNORECASE)

def redact(text: str) -> str:
    """Redacts sensitive information like API keys and emails from a string."""
    text = SECRET_PATTERN.sub("[REDACTED_TOKEN]", text)
    text = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
    return text
