FROM python:3.11-slim

WORKDIR /app

# System deps kept minimal; asyncpg/cryptography ship wheels for slim.
RUN pip install --no-cache-dir --upgrade pip

COPY pyproject.toml ./
COPY koel ./koel
COPY scripts ./scripts

RUN pip install --no-cache-dir .

EXPOSE 8100 8101 8102 8103 8104

# Default command is the orchestrator; compose overrides per service.
CMD ["uvicorn", "koel.voicebot.main:app", "--host", "0.0.0.0", "--port", "8100"]
