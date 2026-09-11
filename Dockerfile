FROM python:3.11-slim

# System deps: ffmpeg for yt-dlp audio extract; curl for healthcheck.
# PO Token generation runs in a separate Coolify sidecar
# (brainicism/bgutil-ytdlp-pot-provider). This image only needs the
# yt-dlp Python plugin (bgutil-ytdlp-pot-provider from PyPI).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Default POT sidecar URL (Coolify shared network hostname).
# Override with POT_PROVIDER_URL if the sidecar name/port differs.
ENV WHISPER_MODEL=small \
    DOWNLOAD_TIMEOUT=120 \
    TRANSCRIBE_TIMEOUT=600 \
    METADATA_TIMEOUT=30 \
    POT_PROVIDER_URL=http://bgutil-pot:4416 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# STT_WORKER_SECRET must be provided at runtime (Coolify env)
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
