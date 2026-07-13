# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/app/data

# Create non-root user upfront
RUN groupadd --system --gid 1001 app \
    && useradd  --system --uid 1001 --gid app \
                --home /app --shell /sbin/nologin app

WORKDIR /app

# Install dependencies separately for better layer caching
COPY --chown=app:app requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY --chown=app:app app ./app

# Pre-create writable data directories with correct ownership
RUN mkdir -p /app/data/uploads \
    && chown -R app:app /app

USER app

EXPOSE 8000

# Lightweight liveness probe
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

# Single worker: the in-process cleanup task and SQLite connection are not safe to share
# across multiple worker processes. For higher throughput put a reverse proxy in front and
# run multiple containers.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips=*", \
     "--log-level", "info"]
