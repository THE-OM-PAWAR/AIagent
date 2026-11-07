# Production Dockerfile for AIagent (Telegram AI Assistant)
FROM python:3.12-slim

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Create dedicated non-root user and group
RUN groupadd -r -g 10001 appgroup && \
    useradd -r -u 10001 -g appgroup -d /app -s /sbin/nologin appuser

# Install dependencies in a separate layer for optimal caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY --chown=appuser:appgroup src/ ./src/
COPY --chown=appuser:appgroup main.py ./

# Ensure correct permissions
RUN chown -R appuser:appgroup /app

# Switch to non-root user
USER appuser

# Use SIGTERM for graceful shutdown
STOPSIGNAL SIGTERM

# Container healthcheck: verifies application and MongoDB connectivity
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -m src.healthcheck --skip-telegram || exit 1

# Start application (exec form receives signals directly)
CMD ["python", "main.py"]

