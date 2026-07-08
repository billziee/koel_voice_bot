# syntax=docker/dockerfile:1
# Multi-stage build (P): deps compiled in a builder, only the venv + app ship in
# the runtime layer, which runs as a non-root user with a container HEALTHCHECK.

# ---- builder ----------------------------------------------------------------
FROM python:3.11-slim AS builder
WORKDIR /build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# Build into an isolated virtualenv we can copy wholesale into the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml ./
COPY koel ./koel
COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini ./
# `smart-turn` extra pulls onnxruntime for the semantic end-of-turn gate
# (koel/voicebot/pipeline/endpointing.py); the model itself is fetched below —
# it's not on PyPI, so it can't be a pip dependency.
RUN pip install --upgrade pip && pip install ".[smart-turn]"
RUN bash scripts/fetch_smart_turn_model.sh models/smart-turn-v3.2-cpu.onnx

# ---- runtime ----------------------------------------------------------------
FROM python:3.11-slim AS runtime
WORKDIR /app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# curl for the HEALTHCHECK probe; then a non-root user owns the app.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 koel

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=koel:koel /build/models ./models
COPY --chown=koel:koel koel ./koel
COPY --chown=koel:koel scripts ./scripts
COPY --chown=koel:koel migrations ./migrations
COPY --chown=koel:koel alembic.ini ./

USER koel
EXPOSE 8100 8101 8102 8103 8104

# Liveness probe (P): the process is up. Compose/K8s use /health/ready for routing.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8100/health/live || exit 1

# Default command is the orchestrator; compose overrides port/module per service.
CMD ["uvicorn", "koel.voicebot.main:app", "--host", "0.0.0.0", "--port", "8100"]
