FROM python:3.12-slim

RUN apt-get update && apt-get install -y ca-certificates curl git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/
# --retries/--timeout harden the build against transient PyPI read-timeouts on
# large wheels (e.g. livekit-*). A slow chunk should retry, not kill the build.
RUN pip install --no-cache-dir --retries 5 --timeout 120 .

# Pre-download ML models (turn detector, VAD)
RUN python -m src.agent download-files || true

HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -f http://localhost:8080/health/db || exit 1

CMD ["python", "-m", "src.agent", "start"]
