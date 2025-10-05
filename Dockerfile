# syntax=docker/dockerfile:1.4

# --- Stage 1: Builder ---------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps once and build wheels for a reproducible, cacheable layer
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /app/wheels -r requirements.txt


# --- Stage 2: Final Image -----------------------------------------------------
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Platforms like HF Spaces set PORT at runtime; default to 7860 for local
    PORT=7860

WORKDIR /app

# Minimal runtime deps (TLS certs for HTTPS calls, etc.)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Install prebuilt wheels
COPY --from=builder /app/wheels /wheels
COPY --from=builder /app/requirements.txt .
RUN pip install --no-cache-dir /wheels/*

# Copy app source
COPY . .

# Non-root for security
RUN useradd --create-home --shell /bin/bash appuser \
 && chown -R appuser:appuser /app
USER appuser

# --- Ports commonly used with A2A agents -------------------------------------
# NOTE: EXPOSE is documentation; publishing happens via `-p host:container`.
# 443   → Recommended prod HTTPS port (JSON-RPC /rpc and websockets on TLS)
# 80    → HTTP (typically only to redirect → 443 behind a reverse proxy)
# 8080  → Very common app / agent port for /rpc during dev/staging
# 8000  → Uvicorn/Gunicorn defaults (also used in many Python stacks)
# 7860  → Popular in ML tooling & Hugging Face Spaces (default UI port here)
# 5000  → Flask default (frequent in prototypes and simple agents)
# 3000  → Node dev servers / proxy frontends around agents
# 8443  → Alternate TLS port (used in some k8s/ingress setups)
EXPOSE 443
EXPOSE 80
EXPOSE 8080
EXPOSE 8000
EXPOSE 7860
EXPOSE 5000
EXPOSE 3000
EXPOSE 8443

# --- Start command ------------------------------------------------------------
# Use shell form so ${PORT} expands at runtime (important on HF Spaces).
# --host 0.0.0.0 allows external connections
# --proxy-headers plays nice behind reverse proxies
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860} --proxy-headers"]
