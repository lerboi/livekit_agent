FROM python:3.12-slim

RUN apt-get update && apt-get install -y ca-certificates curl git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/
# --retries/--timeout harden the build against transient PyPI read-timeouts on
# large wheels (e.g. livekit-*). A slow chunk should retry, not kill the build.
RUN pip install --no-cache-dir --retries 5 --timeout 120 .

# Pre-download ML models (turn detector, VAD) into the image layer so the worker
# starts fast and never fetches at call time. NO `|| true`: a failed download must
# fail the build LOUDLY rather than silently ship an image that crashes on every
# call at MultilingualModel() ("Could not find file languages.json"). The runtime
# key preflight in src/agent.py __main__ is gated to the start/dev subcommands, so
# this build-time step reaches the download-files CLI without needing any secrets.
RUN python -m src.agent download-files

HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -f http://localhost:8080/health/db || exit 1

CMD ["python", "-m", "src.agent", "start"]
