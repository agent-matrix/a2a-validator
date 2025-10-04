# syntax=docker/dockerfile:1
FROM python:3.11-slim

# --- base env ---
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# --- system deps ---
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# --- app dir ---
WORKDIR /app

# --- python deps (cache friendly layer) ---
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# --- copy app ---
COPY . .

# Hugging Face sets $PORT at runtime; keep a sane default for local runs
ENV PORT=7860
EXPOSE 7860

# Optional: run as non-root
# RUN useradd -ms /bin/bash appuser && chown -R appuser:appuser /app
# USER appuser

# --- start (shell form so $PORT expands) ---
# --proxy-headers is helpful behind HF’s proxy
CMD uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers
